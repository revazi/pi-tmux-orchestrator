import net from "node:net";
import { randomBytes } from "node:crypto";

const VERSION = 1;
const MAX_FRAME_BYTES = 256 * 1024;
const DELIVERY_ENTRY = "pi-tmux-orchestrator-delivery-v1";
const BOUNDARY_ENTRY = "pi-tmux-orchestrator-context-boundary-v1";
const MESSAGE_TYPE = "pi-tmux-orchestrator-message-v1";
const ROLE = process.env.PI_TMUX_ORCHESTRATOR_ROLE;
const TOKEN = process.env.PI_TMUX_ORCHESTRATOR_TOKEN;
const SOCKET_PATH = process.env.PI_TMUX_ORCHESTRATOR_SOCKET;
const GENERATION = Number(process.env.PI_TMUX_ORCHESTRATOR_GENERATION);
const ROLES = new Set(["implementer", "reviewer", "probe", "playwright", "django"]);
const ORCHESTRATION_MESSAGE_KEY = `custom:${MESSAGE_TYPE}`;
const PRUNABLE_PROVIDER_ROLES = new Set(["assistant", "toolResult"]);
const REPLACEABLE_CONTEXT_KINDS = new Set(["baseline", "run_state"]);

function id() {
  return randomBytes(16).toString("hex");
}

function validEnvironment() {
  const checks = [
    ROLES.has(ROLE),
    /^[a-f0-9]{32}$/.test(TOKEN || ""),
    Boolean(SOCKET_PATH),
    Number.isInteger(GENERATION) && GENERATION > 0,
  ];
  return checks.every(Boolean);
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

function deliveryOptions(trigger) {
  return { triggerTurn: trigger, deliverAs: "followUp" };
}

function orchestrationDetails(item = {}) {
  if (`${item.role}:${item.customType}` !== ORCHESTRATION_MESSAGE_KEY) return undefined;
  return Object(item.details);
}

function contextKey(details = {}) {
  if (details.kind === "assignment") return "assignment";
  if (details.kind !== "context") return undefined;
  return REPLACEABLE_CONTEXT_KINDS.has(details.delivery_kind)
    ? `context:${details.delivery_kind}`
    : undefined;
}

function latestContextIndexes(messages) {
  const latest = new Map();
  for (const [index, item] of messages.entries()) {
    const key = contextKey(orchestrationDetails(item));
    if (key) latest.set(key, index);
  }
  return latest;
}

function assignmentBoundaryIndex(messages, latest) {
  const current = messages[latest.get("assignment")];
  const assignmentId = orchestrationDetails(current)?.assignment_id;
  return messages.findIndex((item) => {
    const details = orchestrationDetails(item);
    return details?.kind === "assignment" && details.assignment_id === assignmentId;
  });
}

function contextSelection(messages) {
  const latest = latestContextIndexes(messages);
  return { latest, assignmentBoundary: assignmentBoundaryIndex(messages, latest) };
}

function isCompletedProviderMessage(item, index, boundary) {
  return boundary >= 0
    && index < boundary
    && PRUNABLE_PROVIDER_ROLES.has(Object(item).role);
}

function keepWorkerContextMessage(item, index, selection) {
  const details = orchestrationDetails(item);
  const key = contextKey(details);
  if (key) return selection.latest.get(key) === index;
  if (details) return true;
  return !isCompletedProviderMessage(item, index, selection.assignmentBoundary);
}

function filterWorkerContext(messages) {
  const selection = contextSelection(messages);
  return messages.filter((item, index) => keepWorkerContextMessage(item, index, selection));
}

function deliveryEntryData(entry) {
  if (entry.type !== "custom") return undefined;
  if (entry.customType !== DELIVERY_ENTRY) return undefined;
  if (!entry.data) return undefined;
  return entry.data;
}

function boundaryAssignmentId(entry) {
  if (`${entry.type}:${entry.customType}` !== `custom:${BOUNDARY_ENTRY}`) return undefined;
  const assignmentId = Object(entry.data).assignment_id;
  return /^[a-f0-9]{32}$/.test(assignmentId) ? assignmentId : undefined;
}

function assignmentFromDelivery(data) {
  if (data.kind !== "assignment") return undefined;
  if (typeof data.assignment_id !== "string") return undefined;
  return {
    id: data.assignment_id,
    round: data.round,
    kind: data.assignment_kind,
  };
}

function deliveryCompletesAssignment(data, activeAssignment) {
  return data.kind === "report" && activeAssignment?.id === data.assignment_id;
}

function applyRestoredDelivery(state, data) {
  if (typeof data.delivery_id === "string") state.delivered.add(data.delivery_id);
  const assignment = assignmentFromDelivery(data);
  if (assignment) {
    state.assignmentIds.add(assignment.id);
    state.activeAssignment = assignment;
    return;
  }
  if (deliveryCompletesAssignment(data, state.activeAssignment)) {
    state.activeAssignment = undefined;
  }
}

function restoreWorkerState(entries) {
  const state = {
    activeAssignment: undefined,
    assignmentIds: new Set(),
    delivered: new Set(),
  };
  for (const entry of entries) {
    const boundaryId = boundaryAssignmentId(entry);
    if (boundaryId) state.assignmentIds.add(boundaryId);
    const data = deliveryEntryData(entry);
    if (data) applyRestoredDelivery(state, data);
  }
  return state;
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

function reportParameters(role) {
  const kind = {
    implementer: "implementation",
    reviewer: "review",
    probe: "probe",
    playwright: "playwright",
    django: "django",
  }[role];
  const verdicts = {
    reviewer: ["approved", "changes_requested"],
    playwright: ["pass", "fail"],
    django: ["advisory_approved", "issues_found"],
  }[role];
  const properties = {
    kind: { type: "string", enum: [kind] },
    summary: { type: "string", maxLength: 2000 },
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
  };
  const required = ["kind", "summary"];
  if (role === "implementer") {
    properties.changed_paths = { type: "array", maxItems: 50, items: { type: "string", maxLength: 500 } };
  }
  if (verdicts) {
    properties.verdict = { type: "string", enum: verdicts };
    required.push("verdict");
  }
  return { type: "object", additionalProperties: false, required, properties };
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

// fallow-ignore-next-line unused-export -- loaded explicitly by Python worker launch commands
export default function orchestratorWorker(pi) {
  if (!validEnvironment()) throw new Error("Pi Tmux Orchestrator worker environment is invalid");

  let socket;
  let buffer = Buffer.alloc(0);
  let stopping = false;
  let reconnectTimer;
  let reconnectDelay = 100;
  let activeAssignment;
  let context;
  const assignmentIds = new Set();
  const pending = new Map();
  const delivered = new Set();

  function restore(ctx) {
    const restored = restoreWorkerState(ctx.sessionManager.getEntries());
    delivered.clear();
    for (const deliveryId of restored.delivered) delivered.add(deliveryId);
    assignmentIds.clear();
    for (const assignmentId of restored.assignmentIds) assignmentIds.add(assignmentId);
    activeAssignment = restored.activeAssignment;
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
      deliveryOptions(value.trigger === true),
    );
    if (isAssignment && !assignmentIds.has(value.assignment_id)) {
      pi.appendEntry(BOUNDARY_ENTRY, {
        assignment_id: value.assignment_id,
        assignment_kind: value.kind,
        generation: GENERATION,
        round: value.round,
      });
      assignmentIds.add(value.assignment_id);
    }
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
        await brokerRequest(message("hello", { generation: GENERATION }));
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
    parameters: reportParameters(ROLE),
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
  pi.on("context", (event) => ({
    messages: filterWorkerContext(event.messages),
  }));
  pi.on("agent_start", (_event, ctx) => lifecycle("active", ctx));
  pi.on("agent_settled", (_event, ctx) => lifecycle(activeAssignment ? "waiting" : "idle", ctx, true));
  pi.on("session_shutdown", () => {
    stopping = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    if (socket) socket.destroy();
  });
}

export const testHooks = {
  deliveryOptions,
  filterWorkerContext,
  reportParameters,
  restoreWorkerState,
  totalUsage,
};
