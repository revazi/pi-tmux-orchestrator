// fallow-ignore-file unused-file -- loaded explicitly by Python worker launch commands
import net from "node:net";
import { randomBytes } from "node:crypto";

const VERSION = 1;
const MAX_FRAME_BYTES = 256 * 1024;
const DELIVERY_ENTRY = "pi-tmux-orchestrator-delivery-v1";
const MESSAGE_TYPE = "pi-tmux-orchestrator-message-v1";
const ROLE = process.env.PI_TMUX_ORCHESTRATOR_ROLE;
const TOKEN = process.env.PI_TMUX_ORCHESTRATOR_TOKEN;
const SOCKET_PATH = process.env.PI_TMUX_ORCHESTRATOR_SOCKET;
const ROLES = new Set(["implementer", "reviewer", "probe", "playwright", "django"]);

function id() {
  return randomBytes(16).toString("hex");
}

function validEnvironment() {
  return ROLES.has(ROLE) && /^[a-f0-9]{32}$/.test(TOKEN || "") && Boolean(SOCKET_PATH);
}

function frame(value) {
  const payload = Buffer.from(JSON.stringify(value), "utf8");
  if (!payload.length || payload.length > MAX_FRAME_BYTES) throw new Error("broker_frame_too_large");
  const prefix = Buffer.allocUnsafe(4);
  prefix.writeUInt32BE(payload.length);
  return Buffer.concat([prefix, payload]);
}

function message(type, extra = {}) {
  return { version: VERSION, type, role: ROLE, token: TOKEN, id: id(), ...extra };
}

function text(value, limit) {
  if (typeof value !== "string" || !value.trim() || value.length > limit || value.includes("\0")) {
    throw new Error("invalid_report_text");
  }
  return value;
}

function textArray(value, label) {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > 50) throw new Error(`invalid_${label}`);
  return value.map((item) => text(item, 500));
}

function normalizeReport(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("invalid_report");
  const expectedKind = {
    implementer: "implementation",
    reviewer: "review",
    probe: "probe",
    playwright: "playwright",
    django: "django",
  }[ROLE];
  if (input.kind !== expectedKind) throw new Error("invalid_report_kind");
  const changedPaths = textArray(input.changed_paths, "changed_paths");
  if (ROLE !== "implementer" && changedPaths.length) throw new Error("read_only_role_changed_paths");
  const checks = input.checks ?? [];
  if (!Array.isArray(checks) || checks.length > 50) throw new Error("invalid_checks");
  const normalizedChecks = checks.map((check) => {
    if (!check || typeof check !== "object" || !["passed", "failed", "skipped", "unknown"].includes(check.status)) {
      throw new Error("invalid_check");
    }
    return { name: text(check.name, 500), status: check.status };
  });
  const findings = input.findings ?? [];
  if (!Array.isArray(findings) || findings.length > 50) throw new Error("invalid_findings");
  const normalizedFindings = findings.map((finding) => {
    if (!finding || typeof finding !== "object" || !["critical", "high", "medium", "low", "info"].includes(finding.severity)) {
      throw new Error("invalid_finding");
    }
    const line = finding.line ?? null;
    if (line !== null && (!Number.isInteger(line) || line <= 0)) throw new Error("invalid_finding_line");
    return {
      severity: finding.severity,
      path: finding.path == null ? null : text(finding.path, 500),
      line,
      summary: text(finding.summary, 500),
      acceptance: finding.acceptance == null ? null : text(finding.acceptance, 500),
    };
  });
  const verdicts = {
    reviewer: ["approved", "changes_requested"],
    playwright: ["pass", "fail"],
    django: ["advisory_approved", "issues_found"],
  }[ROLE];
  const verdict = input.verdict ?? null;
  if (verdicts ? !verdicts.includes(verdict) : verdict !== null) throw new Error("invalid_report_verdict");
  return {
    kind: input.kind,
    summary: text(input.summary, 2000),
    changed_paths: changedPaths,
    checks: normalizedChecks,
    findings: normalizedFindings,
    risks: textArray(input.risks, "risks"),
    limitations: textArray(input.limitations, "limitations"),
    verdict,
  };
}

function totalUsage(ctx) {
  const totals = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, reasoning: null, cost: { total: 0 } };
  let hasReasoning = false;
  for (const entry of ctx.sessionManager.getEntries()) {
    if (entry.type !== "message" || entry.message?.role !== "assistant" || !entry.message.usage) continue;
    const usage = entry.message.usage;
    for (const key of ["input", "output", "cacheRead", "cacheWrite"]) {
      if (Number.isFinite(usage[key]) && usage[key] >= 0) totals[key] += usage[key];
    }
    if (Number.isFinite(usage.reasoning) && usage.reasoning >= 0) {
      totals.reasoning = (totals.reasoning || 0) + usage.reasoning;
      hasReasoning = true;
    }
    if (Number.isFinite(usage.cost?.total) && usage.cost.total >= 0) totals.cost.total += usage.cost.total;
  }
  if (!hasReasoning) delete totals.reasoning;
  const context = ctx.getContextUsage();
  if (context) {
    totals.contextTokens = context.tokens;
    totals.contextWindow = context.contextWindow;
    totals.contextPercent = context.percent;
  }
  return totals;
}

export default function orchestratorWorker(pi) {
  if (!validEnvironment()) throw new Error("Pi Tmux Orchestrator worker environment is invalid");

  let socket;
  let buffer = Buffer.alloc(0);
  let stopping = false;
  let reconnectTimer;
  let reconnectDelay = 100;
  let activeAssignment;
  let context;
  const pending = new Map();
  const delivered = new Set();

  function restore(ctx) {
    delivered.clear();
    activeAssignment = undefined;
    for (const entry of ctx.sessionManager.getEntries()) {
      if (entry.type !== "custom" || entry.customType !== DELIVERY_ENTRY || !entry.data) continue;
      if (typeof entry.data.delivery_id === "string") delivered.add(entry.data.delivery_id);
      if (entry.data.kind === "assignment" && typeof entry.data.assignment_id === "string") {
        activeAssignment = {
          id: entry.data.assignment_id,
          round: entry.data.round,
          kind: entry.data.assignment_kind,
        };
      }
      if (entry.data.kind === "report" && activeAssignment?.id === entry.data.assignment_id) {
        activeAssignment = undefined;
      }
    }
  }

  function send(value) {
    if (!socket || socket.destroyed || !socket.writable) throw new Error("coordination_broker_disconnected");
    socket.write(frame(value));
  }

  function brokerRequest(value, timeout = 10_000) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(value.id);
        reject(new Error("coordination_response_timeout"));
      }, timeout);
      pending.set(value.id, { resolve, reject, timer });
      try {
        send(value);
      } catch (error) {
        clearTimeout(timer);
        pending.delete(value.id);
        reject(error);
      }
    });
  }

  function lifecycle(state, ctx, includeUsage = false) {
    const value = message("lifecycle", { state, usage: includeUsage ? totalUsage(ctx) : null });
    brokerRequest(value).catch(() => {});
  }

  function acknowledge(deliveryId, status) {
    brokerRequest(message("ack", { delivery_id: deliveryId, status })).catch(() => {});
  }

  function acceptDelivery(value) {
    if (
      typeof value.id !== "string" ||
      !/^[a-f0-9]{32}$/.test(value.id) ||
      typeof value.content !== "string" ||
      !value.content.trim() ||
      value.content.length > 32_768 ||
      !Number.isInteger(value.round) ||
      value.round <= 0 ||
      typeof value.trigger !== "boolean" ||
      (value.type === "assignment" &&
        (typeof value.assignment_id !== "string" ||
          !/^[a-f0-9]{32}$/.test(value.assignment_id) ||
          typeof value.kind !== "string"))
    ) {
      socket.destroy(new Error("invalid_broker_delivery"));
      return;
    }
    const duplicate = delivered.has(value.id);
    if (duplicate) {
      acknowledge(value.id, "duplicate");
      return;
    }
    const isAssignment = value.type === "assignment";
    const details = {
      delivery_id: value.id,
      kind: isAssignment ? "assignment" : "context",
      delivery_kind: value.kind,
      assignment_id: isAssignment ? value.assignment_id : null,
      assignment_kind: isAssignment ? value.kind : null,
      round: value.round,
    };
    pi.sendMessage(
      { customType: MESSAGE_TYPE, content: value.content, display: true, details },
      {
        triggerTurn: value.trigger === true,
        deliverAs: value.trigger === true ? "followUp" : "nextTurn",
      },
    );
    pi.appendEntry(DELIVERY_ENTRY, details);
    delivered.add(value.id);
    if (isAssignment) activeAssignment = { id: value.assignment_id, round: value.round, kind: value.kind };
    acknowledge(value.id, "accepted");
  }

  function handle(value) {
    if (!value || value.version !== VERSION || typeof value.type !== "string") return;
    if (value.type === "response" && typeof value.id === "string") {
      const waiter = pending.get(value.id);
      if (!waiter) return;
      pending.delete(value.id);
      clearTimeout(waiter.timer);
      if (value.success) waiter.resolve(value);
      else waiter.reject(new Error(value.error || value.status || "coordination_rejected"));
      return;
    }
    if (value.type === "assignment" || value.type === "context") acceptDelivery(value);
    if (value.type === "abort") {
      if (typeof value.id !== "string" || !/^[a-f0-9]{32}$/.test(value.id)) {
        socket.destroy(new Error("invalid_broker_abort"));
        return;
      }
      context?.abort();
      acknowledge(value.id, "accepted");
    }
  }

  function consume(chunk) {
    buffer = Buffer.concat([buffer, chunk]);
    while (buffer.length >= 4) {
      const size = buffer.readUInt32BE(0);
      if (!size || size > MAX_FRAME_BYTES) {
        socket.destroy(new Error("invalid_broker_frame"));
        return;
      }
      if (buffer.length < size + 4) return;
      const payload = buffer.subarray(4, size + 4);
      buffer = buffer.subarray(size + 4);
      try {
        handle(JSON.parse(payload.toString("utf8")));
      } catch {
        socket.destroy(new Error("invalid_broker_json"));
        return;
      }
    }
  }

  function connect() {
    if (stopping) return;
    buffer = Buffer.alloc(0);
    socket = net.createConnection({ path: SOCKET_PATH });
    socket.on("connect", async () => {
      reconnectDelay = 100;
      try {
        await brokerRequest(message("hello"));
        lifecycle(context?.isIdle() ? "idle" : "active", context, true);
      } catch {
        socket.destroy();
      }
    });
    socket.on("data", consume);
    socket.on("error", () => {});
    socket.on("close", () => {
      for (const waiter of pending.values()) {
        clearTimeout(waiter.timer);
        waiter.reject(new Error("coordination_delivery_uncertain"));
      }
      pending.clear();
      if (stopping) return;
      reconnectTimer = setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 5000);
    });
  }

  pi.registerTool({
    name: "orchestrator_report",
    label: "Orchestration Report",
    description: "Submit the final bounded structured result for the active assignment. This must be the final action of the assignment and ends the turn.",
    promptSnippet: "Submit a final structured orchestration result and end the assignment",
    promptGuidelines: [
      "Use orchestrator_report exactly once as the final action for every active orchestration assignment.",
      "Report concise summaries, paths, checks, findings, risks, and limitations; never copy diffs, logs, prompts, credentials, provider bodies, or private payloads.",
      "After reporting, end the turn. Never wait, sleep, or poll for coordination work.",
    ],
    parameters: {
      type: "object",
      additionalProperties: false,
      required: ["kind", "summary"],
      properties: {
        kind: { type: "string", enum: ["implementation", "review", "probe", "playwright", "django"] },
        summary: { type: "string", maxLength: 2000 },
        changed_paths: { type: "array", maxItems: 50, items: { type: "string", maxLength: 500 } },
        checks: {
          type: "array", maxItems: 50,
          items: {
            type: "object", additionalProperties: false, required: ["name", "status"],
            properties: { name: { type: "string", maxLength: 500 }, status: { type: "string", enum: ["passed", "failed", "skipped", "unknown"] } },
          },
        },
        findings: {
          type: "array", maxItems: 50,
          items: {
            type: "object", additionalProperties: false, required: ["severity", "summary"],
            properties: {
              severity: { type: "string", enum: ["critical", "high", "medium", "low", "info"] },
              path: { type: "string", maxLength: 500 }, line: { type: "integer", minimum: 1 },
              summary: { type: "string", maxLength: 500 }, acceptance: { type: "string", maxLength: 500 },
            },
          },
        },
        risks: { type: "array", maxItems: 50, items: { type: "string", maxLength: 500 } },
        limitations: { type: "array", maxItems: 50, items: { type: "string", maxLength: 500 } },
        verdict: { type: "string", enum: ["approved", "changes_requested", "pass", "fail", "advisory_approved", "issues_found"] },
      },
    },
    async execute(_toolCallId, input) {
      if (!activeAssignment) throw new Error("no_active_orchestration_assignment");
      const report = normalizeReport(input);
      const assignment = activeAssignment;
      const response = await brokerRequest(message("report", { assignment_id: assignment.id, report }));
      if (!response.success) throw new Error("orchestration_report_rejected");
      pi.appendEntry(DELIVERY_ENTRY, { kind: "report", assignment_id: assignment.id, report_id: response.id });
      activeAssignment = undefined;
      return {
        content: [{ type: "text", text: `Structured ${report.kind} report accepted for round ${assignment.round}. End this turn; do not wait or poll.` }],
        details: { protocol_version: VERSION, assignment_id: assignment.id, round: assignment.round, role: ROLE, report },
        terminate: true,
      };
    },
  });

  pi.on("session_start", (_event, ctx) => {
    context = ctx;
    restore(ctx);
    connect();
  });
  pi.on("context", (event) => {
    let latestBaseline = -1;
    const latestRoundKind = new Map();
    event.messages.forEach((item, index) => {
      if (item?.role !== "custom" || item.customType !== MESSAGE_TYPE) return;
      const details = item.details || {};
      if (details.kind === "context" && details.delivery_kind === "baseline") {
        latestBaseline = index;
      }
      const round = details.round;
      const kind = details.assignment_kind || details.delivery_kind || "context";
      if (Number.isInteger(round)) latestRoundKind.set(`${round}:${kind}`, index);
    });
    return {
      messages: event.messages.filter((item, index) => {
        if (item?.role !== "custom" || item.customType !== MESSAGE_TYPE) return true;
        const details = item.details || {};
        if (details.kind === "context" && details.delivery_kind === "baseline") {
          return index === latestBaseline;
        }
        const round = details.round;
        const kind = details.assignment_kind || details.delivery_kind || "context";
        return !Number.isInteger(round) || latestRoundKind.get(`${round}:${kind}`) === index;
      }),
    };
  });
  pi.on("agent_start", (_event, ctx) => lifecycle("active", ctx));
  pi.on("agent_settled", (_event, ctx) => lifecycle(activeAssignment ? "waiting" : "idle", ctx, true));
  pi.on("session_shutdown", () => {
    stopping = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    if (socket) socket.destroy();
  });
}
