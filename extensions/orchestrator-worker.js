import net from "node:net";
import { randomBytes } from "node:crypto";

const VERSION = 1;
const MAX_FRAME_BYTES = 256 * 1024;
const DELIVERY_ENTRY = "pi-tmux-orchestrator-delivery-v1";
const BOUNDARY_ENTRY = "pi-tmux-orchestrator-context-boundary-v1";
const GUARDRAIL_ENTRY = "pi-tmux-orchestrator-guardrail-v1";
const MESSAGE_TYPE = "pi-tmux-orchestrator-message-v1";
const ROLE = process.env.PI_TMUX_ORCHESTRATOR_ROLE;
const TOKEN = process.env.PI_TMUX_ORCHESTRATOR_TOKEN;
const SOCKET_PATH = process.env.PI_TMUX_ORCHESTRATOR_SOCKET;
const GENERATION = Number(process.env.PI_TMUX_ORCHESTRATOR_GENERATION);
const ROLES = new Set(["implementer", "reviewer", "probe", "playwright", "django"]);
const ORCHESTRATION_MESSAGE_KEY = `custom:${MESSAGE_TYPE}`;
const PRUNABLE_PROVIDER_ROLES = new Set(["assistant", "toolResult"]);
const REPLACEABLE_CONTEXT_KINDS = new Set(["baseline", "run_state"]);
const GUARDRAIL_LEVELS = ["warning", "hard"];
const GUARDRAIL_METRICS = ["context_percent", "context_tokens", "provider_calls"];
const INTEGER_GUARDRAIL_METRICS = new Set(["context_tokens", "provider_calls"]);

function id() {
  return randomBytes(16).toString("hex");
}

function validEnvironment(guardrailPolicy) {
  const checks = [
    ROLES.has(ROLE),
    /^[a-f0-9]{32}$/.test(TOKEN || ""),
    Boolean(SOCKET_PATH),
    Number.isInteger(GENERATION) && GENERATION > 0,
    guardrailPolicy !== undefined,
  ];
  return checks.every(Boolean);
}

function exactObjectKeys(value, expected) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).sort().join("\0") === [...expected].sort().join("\0");
}

function positiveSafeInteger(value) {
  return [Number.isSafeInteger(value), value > 0].every(Boolean);
}

function validContextPercent(value) {
  return [
    typeof value === "number",
    Number.isFinite(value),
    value > 0,
    value <= 100,
  ].every(Boolean);
}

function validGuardrailThreshold(metric, value) {
  const validator = INTEGER_GUARDRAIL_METRICS.has(metric)
    ? positiveSafeInteger
    : validContextPercent;
  return validator(value);
}

function validGuardrailLevel(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  return Object.entries(value).every(([metric, threshold]) => (
    GUARDRAIL_METRICS.includes(metric) && validGuardrailThreshold(metric, threshold)
  ));
}

function parsedJson(raw) {
  try {
    return JSON.parse(raw || "");
  } catch {
    return undefined;
  }
}

function guardrailOrderIsValid(policy) {
  return GUARDRAIL_METRICS.every((metric) => {
    const warning = policy.warning[metric];
    const hard = policy.hard[metric];
    return warning === undefined || hard === undefined || warning <= hard;
  });
}

function parseGuardrailPolicy(raw) {
  const value = Object(parsedJson(raw));
  const validShape = [
    exactObjectKeys(value, ["enforcement", ...GUARDRAIL_LEVELS]),
    ["warn-only", "hard"].includes(value.enforcement),
    validGuardrailLevel(value.warning),
    validGuardrailLevel(value.hard),
  ].every(Boolean);
  if (!validShape) return undefined;
  const policy = {
    enforcement: value.enforcement,
    warning: Object.fromEntries(Object.entries(value.warning)),
    hard: Object.fromEntries(Object.entries(value.hard)),
  };
  if (!guardrailOrderIsValid(policy)) return undefined;
  return policy;
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

function boundaryData(entry) {
  if (`${entry.type}:${entry.customType}` !== `custom:${BOUNDARY_ENTRY}`) return undefined;
  const data = Object(entry.data);
  if (!/^[a-f0-9]{32}$/.test(data.assignment_id)) return undefined;
  return { assignmentId: data.assignment_id, usageBaseline: validUsageBaseline(data.usage_baseline) };
}

function validGuardrailEntryEnvelope(entry, data) {
  return [
    `${entry.type}:${entry.customType}` === `custom:${GUARDRAIL_ENTRY}`,
    /^[a-f0-9]{32}$/.test(data.assignment_id),
    ["triggered", "delivered"].includes(data.status),
    GUARDRAIL_LEVELS.includes(data.level),
  ].every(Boolean);
}

function validTriggeredGuardrail(data) {
  return [
    GUARDRAIL_METRICS.includes(data.metric),
    validGuardrailThreshold(data.metric, data.threshold),
    typeof data.observed === "number",
    Number.isFinite(data.observed),
    data.observed >= data.threshold,
  ].every(Boolean);
}

function deliveredGuardrailData(data) {
  if (data.level !== "warning") return undefined;
  return { assignmentId: data.assignment_id, level: data.level, status: data.status };
}

function triggeredGuardrailData(data) {
  if (!validTriggeredGuardrail(data)) return undefined;
  return {
    assignmentId: data.assignment_id,
    level: data.level,
    status: data.status,
    finding: {
      metric: data.metric,
      observed: data.observed,
      threshold: data.threshold,
    },
  };
}

function guardrailEntryData(entry) {
  const data = Object(entry.data);
  if (!validGuardrailEntryEnvelope(entry, data)) return undefined;
  return data.status === "delivered"
    ? deliveredGuardrailData(data)
    : triggeredGuardrailData(data);
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

function applyRestoredBoundary(state, baselines, entry) {
  const boundary = boundaryData(entry);
  if (!boundary) return;
  state.assignmentIds.add(boundary.assignmentId);
  if (boundary.usageBaseline) baselines.set(boundary.assignmentId, boundary.usageBaseline);
}

function attachRestoredBaseline(state, baselines) {
  const baseline = baselines.get(state.activeAssignment?.id);
  if (baseline) state.activeAssignment.usageBaseline = baseline;
}

function restoreWorkerState(entries) {
  const state = {
    activeAssignment: undefined,
    assignmentIds: new Set(),
    delivered: new Set(),
  };
  const baselines = new Map();
  for (const entry of entries) {
    applyRestoredBoundary(state, baselines, entry);
    const data = deliveryEntryData(entry);
    if (data) applyRestoredDelivery(state, data);
  }
  attachRestoredBaseline(state, baselines);
  return state;
}

function emptyGuardrailState() {
  return { warning: undefined, hard: undefined, warningDelivered: false };
}

function applyRestoredGuardrail(state, data, assignmentId) {
  const value = Object(data);
  if (value.assignmentId !== assignmentId) return;
  if (value.status === "delivered") {
    state.warningDelivered = Boolean(state.warning);
    return;
  }
  if (state[value.level] === undefined) state[value.level] = value.finding;
}

function restoreGuardrailState(entries, assignmentId) {
  const state = emptyGuardrailState();
  if (!assignmentId) return state;
  for (const entry of entries) {
    applyRestoredGuardrail(state, guardrailEntryData(entry), assignmentId);
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

const TOKEN_USAGE_KEYS = ["input", "output", "cacheRead", "cacheWrite"];
const CONTEXT_USAGE_KEYS = ["contextTokens", "contextWindow", "contextPercent"];

function assistantEntries(entries) {
  return entries.filter((entry) => entry.type === "message" && entry.message?.role === "assistant");
}

function nonnegativeInteger(value) {
  return Number.isInteger(value) && value >= 0;
}

function baselineIntegersAreValid(value) {
  return ["providerCalls", ...TOKEN_USAGE_KEYS].every((key) => nonnegativeInteger(value[key]));
}

function baselineCostIsValid(value) {
  return Number.isFinite(value.cost?.total) && value.cost.total >= 0;
}

function baselineReasoningIsValid(value) {
  return value.reasoning === undefined || nonnegativeInteger(value.reasoning);
}

function baselineObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function validUsageBaseline(value) {
  if (!baselineObject(value)) return undefined;
  const checks = [
    baselineIntegersAreValid(value),
    baselineCostIsValid(value),
    baselineReasoningIsValid(value),
  ];
  return checks.every(Boolean) ? value : undefined;
}

function totalUsage(ctx) {
  const totals = {
    providerCalls: 0,
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
    cost: { total: 0 },
  };
  let reasoning = 0;
  let hasReasoning = false;
  for (const entry of assistantEntries(ctx.sessionManager.getEntries())) {
    totals.providerCalls += 1;
    const usage = entry.message.usage;
    if (!usage) continue;
    for (const key of TOKEN_USAGE_KEYS) {
      if (Number.isFinite(usage[key]) && usage[key] >= 0) totals[key] += usage[key];
    }
    if (Number.isFinite(usage.reasoning) && usage.reasoning >= 0) {
      reasoning += usage.reasoning;
      hasReasoning = true;
    }
    if (Number.isFinite(usage.cost?.total) && usage.cost.total >= 0) totals.cost.total += usage.cost.total;
  }
  if (hasReasoning) totals.reasoning = reasoning;
  const current = ctx.getContextUsage();
  if (current) {
    totals.contextTokens = current.tokens;
    totals.contextWindow = current.contextWindow;
    totals.contextPercent = current.percent;
  }
  return totals;
}

function availableContextUsage(usage) {
  return Object.fromEntries(
    CONTEXT_USAGE_KEYS.filter((key) => usage[key] !== undefined).map((key) => [key, usage[key]]),
  );
}

function optionalReasoningDelta(current, baseline) {
  if (current.reasoning === undefined) return {};
  return { reasoning: current.reasoning - (baseline.reasoning || 0) };
}

function usageDelta(current, baseline) {
  return {
    providerCalls: current.providerCalls - baseline.providerCalls,
    input: current.input - baseline.input,
    output: current.output - baseline.output,
    cacheRead: current.cacheRead - baseline.cacheRead,
    cacheWrite: current.cacheWrite - baseline.cacheWrite,
    cost: {
      total: Number((current.cost.total - baseline.cost.total).toFixed(12)),
    },
    ...optionalReasoningDelta(current, baseline),
    ...availableContextUsage(current),
  };
}

function assignmentPeakContext(entries, baselineCalls) {
  const tokens = assistantEntries(entries)
    .slice(baselineCalls)
    .map((entry) => entry.message.usage?.totalTokens)
    .filter(nonnegativeInteger);
  return tokens.length ? Math.max(...tokens) : undefined;
}

function addPeakContext(assignment, observedPeak) {
  const candidates = [observedPeak, assignment.contextTokens].filter(nonnegativeInteger);
  if (candidates.length) assignment.peakContextTokens = Math.max(...candidates);
}

function reportUsage(ctx, baseline) {
  const acceptedBaseline = validUsageBaseline(baseline);
  if (!acceptedBaseline) return null;
  const cumulative = totalUsage(ctx);
  const assignment = usageDelta(cumulative, acceptedBaseline);
  const peak = assignmentPeakContext(
    ctx.sessionManager.getEntries(), acceptedBaseline.providerCalls,
  );
  addPeakContext(assignment, peak);
  return { cumulative, assignment };
}

function assignmentUsageBaseline(isAssignment, value, activeAssignment, ctx) {
  if (!isAssignment) return undefined;
  if (activeAssignment?.id === value.assignment_id) return activeAssignment.usageBaseline;
  return totalUsage(ctx);
}

function nextAssignmentGuardrailState(state, activeAssignment, assignmentId) {
  return activeAssignment?.id === assignmentId ? state : emptyGuardrailState();
}

function guardrailObservations(ctx, baseline) {
  const cumulative = totalUsage(ctx);
  const acceptedBaseline = validUsageBaseline(baseline);
  return {
    provider_calls: acceptedBaseline
      ? cumulative.providerCalls - acceptedBaseline.providerCalls
      : undefined,
    context_tokens: cumulative.contextTokens,
    context_percent: cumulative.contextPercent,
  };
}

function guardrailThresholdCrossed(observed, threshold) {
  return [
    typeof observed === "number",
    Number.isFinite(observed),
    observed >= threshold,
  ].every(Boolean);
}

function firstGuardrailFinding(thresholds, observations) {
  for (const metric of Object.keys(thresholds).sort()) {
    const observed = observations[metric];
    const threshold = thresholds[metric];
    if (guardrailThresholdCrossed(observed, threshold)) {
      return { metric, observed, threshold };
    }
  }
  return undefined;
}

function pendingGuardrailFinding(thresholds, existing, observations) {
  return existing ? undefined : firstGuardrailFinding(thresholds, observations);
}

function hardGuardrailThresholds(policy) {
  return policy.hard;
}

function guardrailWarningText(finding) {
  return `Assignment guardrail warning: ${finding.metric} is ${finding.observed} `
    + `(configured warning ${finding.threshold}). Keep any remaining tool use essential and `
    + "submit orchestrator_report before usage grows further.";
}

function appendGuardrailWarning(content, finding) {
  const warning = { type: "text", text: guardrailWarningText(finding) };
  return Array.isArray(content) ? [...content, warning] : [warning];
}

function observationalGuardrailDecision() {
  return undefined;
}

function shouldDeliverGuardrailWarning(activeAssignment, toolName, state) {
  return [
    Boolean(activeAssignment),
    toolName !== "orchestrator_report",
    Boolean(state.warning),
    state.warningDelivered === false,
  ].every(Boolean);
}

// fallow-ignore-next-line unused-export -- loaded explicitly by Python worker launch commands
export default function orchestratorWorker(pi) {
  const guardrailPolicy = parseGuardrailPolicy(
    process.env.PI_TMUX_ORCHESTRATOR_GUARDRAILS,
  );
  if (!validEnvironment(guardrailPolicy)) throw new Error("Pi Tmux Orchestrator worker environment is invalid");

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
    }
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
    async execute(_toolCallId, input, _signal, _onUpdate, ctx) {
      if (!activeAssignment) throw new Error("no_active_orchestration_assignment");
      const report = normalizeReport(input);
      const assignment = activeAssignment;
      const usage = reportUsage(ctx, assignment.usageBaseline);
      const response = await brokerRequest(message("report", {
        assignment_id: assignment.id,
        report,
        usage,
      }));
      if (!response.success) throw new Error("orchestration_report_rejected");
      pi.appendEntry(DELIVERY_ENTRY, { kind: "report", assignment_id: assignment.id, report_id: response.id });
      activeAssignment = undefined;
      guardrailState = emptyGuardrailState();
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
  pi.on("tool_call", (_event, ctx) => {
    if (!activeAssignment) return undefined;
    evaluateAssignmentGuardrails(ctx);
    return observationalGuardrailDecision();
  });
  pi.on("tool_result", (event) => {
    if (!shouldDeliverGuardrailWarning(activeAssignment, event.toolName, guardrailState)) {
      return undefined;
    }
    pi.appendEntry(GUARDRAIL_ENTRY, {
      assignment_id: activeAssignment.id,
      level: "warning",
      status: "delivered",
    });
    guardrailState.warningDelivered = true;
    return { content: appendGuardrailWarning(event.content, guardrailState.warning) };
  });
  pi.on("agent_start", (_event, ctx) => lifecycle("active", ctx));
  pi.on("agent_settled", (_event, ctx) => lifecycle(activeAssignment ? "waiting" : "idle", ctx, true));
  pi.on("session_shutdown", () => {
    stopping = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    if (socket) socket.destroy();
  });
}

export const testHooks = {
  appendGuardrailWarning,
  deliveryOptions,
  filterWorkerContext,
  firstGuardrailFinding,
  guardrailObservations,
  hardGuardrailThresholds,
  observationalGuardrailDecision,
  parseGuardrailPolicy,
  reportParameters,
  reportUsage,
  restoreGuardrailState,
  restoreWorkerState,
  totalUsage,
};
