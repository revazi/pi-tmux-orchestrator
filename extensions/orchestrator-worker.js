import net from "node:net";

import {
  BOUNDARY_ENTRY,
  createWorkerMessage,
  DELIVERY_ENTRY,
  deliveryOptions,
  GUARDRAIL_ENTRY,
  MAX_WORKER_FRAME_BYTES as MAX_FRAME_BYTES,
  RESULT_VOLUME_ENTRY,
  validWorkerEnvironment,
  workerFrame,
  WORKER_MESSAGE_TYPE as MESSAGE_TYPE,
  WORKER_PROTOCOL_VERSION as VERSION,
} from "./orchestrator-worker-protocol.js";
import {
  assignmentUsageBaseline,
  filterWorkerContext,
  restoreWorkerState,
} from "./orchestrator-worker-context.js";
import {
  acceptedReportResult,
  assignmentToolNames,
  normalizeReport,
  planToolDecision,
  prepareReportArguments,
  progressShouldEmit,
  reportedAssignmentRemainsActive,
  reportParameters,
} from "./orchestrator-worker-reporting.js";
import {
  appendGuardrailWarning,
  emptyGuardrailState,
  firstGuardrailFinding,
  guardrailObservations,
  hardGuardrailThresholds,
  nextAssignmentGuardrailState,
  observationalGuardrailDecision,
  parseGuardrailPolicy,
  pendingGuardrailFinding,
  reportUsage,
  restoreGuardrailState,
  shouldDeliverGuardrailWarning,
  totalUsage,
} from "./orchestrator-worker-usage.js";
import {
  applyToolInputPolicy,
  applyToolResultPolicy,
  immediateFollowupObservation,
} from "./orchestrator-result-policy.js";

const ROLE = process.env.PI_TMUX_ORCHESTRATOR_ROLE;
const TOKEN = process.env.PI_TMUX_ORCHESTRATOR_TOKEN;
const SOCKET_PATH = process.env.PI_TMUX_ORCHESTRATOR_SOCKET;
const GENERATION = Number(process.env.PI_TMUX_ORCHESTRATOR_GENERATION);
const message = createWorkerMessage(ROLE, TOKEN);

export default function orchestratorWorker(pi) {
  const guardrailPolicy = parseGuardrailPolicy(
    process.env.PI_TMUX_ORCHESTRATOR_GUARDRAILS,
  );
  if (!validWorkerEnvironment({
    role: ROLE,
    token: TOKEN,
    socketPath: SOCKET_PATH,
    generation: GENERATION,
    guardrailPolicy,
  })) throw new Error("Pi Tmux Orchestrator worker environment is invalid");

  let socket;
  let buffer = Buffer.alloc(0);
  let stopping = false;
  let reconnectTimer;
  let reconnectDelay = 100;
  let activeAssignment;
  let guardrailState = emptyGuardrailState();
  let context;
  const assignmentIds = new Set();
  const pending = new Map();
  const delivered = new Set();
  const toolInputPolicies = new Map();
  let pendingLimitedResults = [];
  let normalTools = [];
  let lastProgressAt = 0;
  let lastProgressPhase;

  function applyActiveToolPolicy() {
    pi.setActiveTools(assignmentToolNames(normalTools, ROLE, activeAssignment?.kind));
  }

  function restore(ctx) {
    const restored = restoreWorkerState(ctx.sessionManager.getEntries());
    delivered.clear();
    for (const deliveryId of restored.delivered) delivered.add(deliveryId);
    assignmentIds.clear();
    for (const assignmentId of restored.assignmentIds) assignmentIds.add(assignmentId);
    activeAssignment = restored.activeAssignment;
    guardrailState = restoreGuardrailState(
      ctx.sessionManager.getEntries(), activeAssignment?.id,
    );
  }

  function send(value) {
    if (!socket || socket.destroyed || !socket.writable) throw new Error("coordination_broker_disconnected");
    socket.write(workerFrame(value));
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

  function recordGuardrail(level, finding) {
    if (!activeAssignment || guardrailState[level] !== undefined) return;
    guardrailState[level] = finding;
    pi.appendEntry(GUARDRAIL_ENTRY, {
      assignment_id: activeAssignment.id,
      level,
      status: "triggered",
      ...finding,
    });
    brokerRequest(message("guardrail", {
      assignment_id: activeAssignment.id,
      level,
      ...finding,
    })).catch(() => {});
  }

  function recordPendingGuardrail(level, thresholds, observations) {
    const finding = pendingGuardrailFinding(
      thresholds, guardrailState[level], observations,
    );
    if (finding) recordGuardrail(level, finding);
  }

  function evaluateAssignmentGuardrails(ctx) {
    if (!activeAssignment) return;
    const observations = guardrailObservations(ctx, activeAssignment.usageBaseline);
    recordPendingGuardrail("warning", guardrailPolicy.warning, observations);
    recordPendingGuardrail(
      "hard", hardGuardrailThresholds(guardrailPolicy), observations,
    );
  }

  function lifecycle(state, ctx, includeUsage = false) {
    const value = message("lifecycle", { state, usage: includeUsage ? totalUsage(ctx) : null });
    brokerRequest(value).catch(() => {});
  }

  function progress(phase, ctx, { includeUsage = false, force = false } = {}) {
    if (!activeAssignment) return;
    const now = Date.now();
    if (!progressShouldEmit(
      phase, lastProgressPhase, now, lastProgressAt, force,
    )) return;
    lastProgressAt = now;
    lastProgressPhase = phase;
    brokerRequest(message("progress", {
      assignment_id: activeAssignment.id,
      phase,
      usage: includeUsage ? totalUsage(ctx) : null,
    })).catch(() => {});
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
    const usageBaseline = assignmentUsageBaseline(
      isAssignment, value, activeAssignment, context,
    );
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
        usage_baseline: usageBaseline,
      });
      assignmentIds.add(value.assignment_id);
    }
    pi.appendEntry(DELIVERY_ENTRY, details);
    delivered.add(value.id);
    if (isAssignment) {
      guardrailState = nextAssignmentGuardrailState(
        guardrailState, activeAssignment, value.assignment_id,
      );
      activeAssignment = {
        id: value.assignment_id,
        round: value.round,
        kind: value.kind,
        usageBaseline,
      };
      lastProgressAt = 0;
      lastProgressPhase = undefined;
      applyActiveToolPolicy();
    }
    acknowledge(value.id, "accepted");
  }

  function acceptResponse(value) {
    if (typeof value.id !== "string") return;
    const waiter = pending.get(value.id);
    if (!waiter) return;
    pending.delete(value.id);
    clearTimeout(waiter.timer);
    if (value.success) waiter.resolve(value);
    else waiter.reject(new Error(value.error || value.status || "coordination_rejected"));
  }

  function acceptAbort(value) {
    if (typeof value.id !== "string" || !/^[a-f0-9]{32}$/.test(value.id)) {
      socket.destroy(new Error("invalid_broker_abort"));
      return;
    }
    context?.abort();
    acknowledge(value.id, "accepted");
  }

  const inboundHandlers = new Map([
    ["response", acceptResponse],
    ["assignment", acceptDelivery],
    ["context", acceptDelivery],
    ["abort", acceptAbort],
  ]);

  function handle(value) {
    if (!value || value.version !== VERSION || typeof value.type !== "string") return;
    inboundHandlers.get(value.type)?.(value);
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
      ...(ROLE === "implementer" ? ["For a plan assignment, report only relevant paths/symbols, intended changes, required checks, risks, and open questions; never claim changes, executed checks, findings, approval, or a verdict."] : []),
      "Report concise summaries, paths, checks, findings, risks, and limitations; never copy diffs, logs, prompts, credentials, provider bodies, or private payloads.",
      "After reporting, end the turn. Never wait, sleep, or poll for coordination work.",
    ],
    parameters: reportParameters(ROLE),
    prepareArguments: prepareReportArguments,
    async execute(_toolCallId, input, _signal, _onUpdate, ctx) {
      if (!activeAssignment) throw new Error("no_active_orchestration_assignment");
      const report = normalizeReport(input, activeAssignment.kind, ROLE);
      const assignment = activeAssignment;
      const usage = reportUsage(ctx, assignment.usageBaseline);
      const response = await brokerRequest(message("report", {
        assignment_id: assignment.id,
        report,
        usage,
      }));
      if (!response.success) throw new Error("orchestration_report_rejected");
      pi.appendEntry(DELIVERY_ENTRY, { kind: "report", assignment_id: assignment.id, report_id: response.id });
      if (reportedAssignmentRemainsActive(activeAssignment, assignment)) {
        activeAssignment = undefined;
        guardrailState = emptyGuardrailState();
        applyActiveToolPolicy();
      }
      return acceptedReportResult(report, assignment, ROLE);
    },
  });

  function assignmentIdOrNull() {
    return activeAssignment ? activeAssignment.id : null;
  }

  function recordImmediateFollowup(event) {
    const followups = pendingLimitedResults.map(
      (previous) => immediateFollowupObservation(previous, event),
    );
    pendingLimitedResults = [];
    for (const followup of followups) {
      pi.appendEntry(RESULT_VOLUME_ENTRY, {
        assignment_id: assignmentIdOrNull(),
        ...followup,
      });
    }
  }

  function prepareToolInput(event) {
    const inputPolicy = applyToolInputPolicy(event);
    if (inputPolicy) toolInputPolicies.set(event.toolCallId, inputPolicy);
  }

  function onToolCall(event, ctx) {
    recordImmediateFollowup(event);
    progress(
      event.toolName === "orchestrator_report" ? "reporting" : "tool",
      ctx,
      { includeUsage: true, force: true },
    );
    const planDecision = planToolDecision(activeAssignment, ROLE, event.toolName);
    if (planDecision) return planDecision;
    prepareToolInput(event);
    if (!activeAssignment) return undefined;
    evaluateAssignmentGuardrails(ctx);
    return observationalGuardrailDecision();
  }

  function recordResultVolume(limited) {
    if (!limited) return;
    pi.appendEntry(RESULT_VOLUME_ENTRY, {
      assignment_id: assignmentIdOrNull(),
      ...limited.observation,
    });
    if (limited.pending) pendingLimitedResults.push(limited.pending);
  }

  function deliverGuardrailWarning(event, content) {
    if (!shouldDeliverGuardrailWarning(
      activeAssignment, event.toolName, guardrailState,
    )) return { content, delivered: false };
    pi.appendEntry(GUARDRAIL_ENTRY, {
      assignment_id: activeAssignment.id,
      level: "warning",
      status: "delivered",
    });
    guardrailState.warningDelivered = true;
    return {
      content: appendGuardrailWarning(content, guardrailState.warning),
      delivered: true,
    };
  }

  function optionalResultDetails(limited) {
    return limited && limited.details !== undefined
      ? { details: limited.details }
      : {};
  }

  function limitedContent(event, limited) {
    return limited && limited.content ? limited.content : event.content;
  }

  function resultWasModified(limited, warning) {
    return [Boolean(limited && limited.content), warning.delivered].some(Boolean);
  }

  function toolResultPatch(event, limited) {
    const warning = deliverGuardrailWarning(event, limitedContent(event, limited));
    if (!resultWasModified(limited, warning)) return undefined;
    return { content: warning.content, ...optionalResultDetails(limited) };
  }

  async function onToolResult(event) {
    const inputPolicy = toolInputPolicies.get(event.toolCallId);
    toolInputPolicies.delete(event.toolCallId);
    const limited = await applyToolResultPolicy(event, inputPolicy);
    recordResultVolume(limited);
    return toolResultPatch(event, limited);
  }

  pi.on("session_start", (_event, ctx) => {
    context = ctx;
    normalTools = pi.getActiveTools();
    restore(ctx);
    applyActiveToolPolicy();
    connect();
  });
  pi.on("context", (event) => ({
    messages: filterWorkerContext(event.messages),
  }));
  pi.on("tool_call", onToolCall);
  pi.on("tool_result", onToolResult);
  pi.on("turn_start", (_event, ctx) => progress("thinking", ctx, { force: true }));
  pi.on("message_update", (event, ctx) => {
    if (event.message?.role === "assistant") progress("streaming", ctx);
  });
  pi.on("message_end", (event, ctx) => {
    if (event.message?.role === "assistant") {
      progress("streaming", ctx, { includeUsage: true, force: true });
    }
  });
  pi.on("agent_start", (_event, ctx) => lifecycle("active", ctx, true));
  pi.on("agent_settled", (_event, ctx) => lifecycle(activeAssignment ? "waiting" : "idle", ctx, true));
  pi.on("session_shutdown", () => {
    stopping = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    if (socket) socket.destroy();
  });
}

export const testHooks = {
  acceptedReportResult,
  appendGuardrailWarning,
  assignmentToolNames,
  deliveryOptions,
  filterWorkerContext,
  firstGuardrailFinding,
  guardrailObservations,
  hardGuardrailThresholds,
  observationalGuardrailDecision,
  normalizeReport: (value, assignmentKind, role = ROLE) => (
    normalizeReport(value, assignmentKind, role)
  ),
  parseGuardrailPolicy,
  planToolDecision,
  prepareReportArguments,
  progressShouldEmit,
  reportParameters,
  reportUsage,
  reportedAssignmentRemainsActive,
  restoreGuardrailState,
  restoreWorkerState,
  totalUsage,
};
