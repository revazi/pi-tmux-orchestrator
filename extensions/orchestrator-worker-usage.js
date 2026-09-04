import { GUARDRAIL_ENTRY } from "./orchestrator-worker-protocol.js";

const GUARDRAIL_LEVELS = ["warning", "hard"];
const GUARDRAIL_METRICS = ["context_percent", "context_tokens", "provider_calls"];
const INTEGER_GUARDRAIL_METRICS = new Set(["context_tokens", "provider_calls"]);
const TOKEN_USAGE_KEYS = ["input", "output", "cacheRead", "cacheWrite"];
const CONTEXT_USAGE_KEYS = ["contextTokens", "contextWindow", "contextPercent"];

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

export function parseGuardrailPolicy(raw) {
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

export function emptyGuardrailState() {
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

export function restoreGuardrailState(entries, assignmentId) {
  const state = emptyGuardrailState();
  if (!assignmentId) return state;
  for (const entry of entries) {
    applyRestoredGuardrail(state, guardrailEntryData(entry), assignmentId);
  }
  return state;
}

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

export function validUsageBaseline(value) {
  if (!baselineObject(value)) return undefined;
  const checks = [
    baselineIntegersAreValid(value),
    baselineCostIsValid(value),
    baselineReasoningIsValid(value),
  ];
  return checks.every(Boolean) ? value : undefined;
}

function emptyUsageAccumulator() {
  return {
    totals: {
      providerCalls: 0,
      input: 0,
      output: 0,
      cacheRead: 0,
      cacheWrite: 0,
      cost: { total: 0 },
    },
    reasoning: 0,
    hasReasoning: false,
  };
}

function addTokenUsage(totals, usage) {
  for (const key of TOKEN_USAGE_KEYS) {
    if (Number.isFinite(usage[key]) && usage[key] >= 0) totals[key] += usage[key];
  }
}

function addReasoningUsage(accumulator, usage) {
  if (!Number.isFinite(usage.reasoning) || usage.reasoning < 0) return;
  accumulator.reasoning += usage.reasoning;
  accumulator.hasReasoning = true;
}

function addCostUsage(totals, usage) {
  if (Number.isFinite(usage.cost?.total) && usage.cost.total >= 0) {
    totals.cost.total += usage.cost.total;
  }
}

function addAssistantUsage(accumulator, entry) {
  accumulator.totals.providerCalls += 1;
  const usage = entry.message.usage;
  if (!usage) return;
  addTokenUsage(accumulator.totals, usage);
  addReasoningUsage(accumulator, usage);
  addCostUsage(accumulator.totals, usage);
}

function accumulatedUsage(entries) {
  const accumulator = emptyUsageAccumulator();
  for (const entry of assistantEntries(entries)) addAssistantUsage(accumulator, entry);
  if (accumulator.hasReasoning) accumulator.totals.reasoning = accumulator.reasoning;
  return accumulator.totals;
}

function addCurrentContext(totals, current) {
  if (!current) return;
  totals.contextTokens = current.tokens;
  totals.contextWindow = current.contextWindow;
  totals.contextPercent = current.percent;
}

export function totalUsage(ctx) {
  const totals = accumulatedUsage(ctx.sessionManager.getEntries());
  addCurrentContext(totals, ctx.getContextUsage());
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

export function reportUsage(ctx, baseline) {
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

export function nextAssignmentGuardrailState(state, activeAssignment, assignmentId) {
  return activeAssignment?.id === assignmentId ? state : emptyGuardrailState();
}

export function guardrailObservations(ctx, baseline) {
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

export function firstGuardrailFinding(thresholds, observations) {
  for (const metric of Object.keys(thresholds).sort()) {
    const observed = observations[metric];
    const threshold = thresholds[metric];
    if (guardrailThresholdCrossed(observed, threshold)) {
      return { metric, observed, threshold };
    }
  }
  return undefined;
}

export function pendingGuardrailFinding(thresholds, existing, observations) {
  return existing ? undefined : firstGuardrailFinding(thresholds, observations);
}

export function hardGuardrailThresholds(policy) {
  return policy.hard;
}

function guardrailWarningText(finding) {
  return `Assignment guardrail warning: ${finding.metric} is ${finding.observed} `
    + `(configured warning ${finding.threshold}). Keep any remaining tool use essential and `
    + "submit orchestrator_report before usage grows further.";
}

export function appendGuardrailWarning(content, finding) {
  const warning = { type: "text", text: guardrailWarningText(finding) };
  return Array.isArray(content) ? [...content, warning] : [warning];
}

export function observationalGuardrailDecision() {
  return undefined;
}

export function shouldDeliverGuardrailWarning(activeAssignment, toolName, state) {
  return [
    Boolean(activeAssignment),
    toolName !== "orchestrator_report",
    Boolean(state.warning),
    state.warningDelivered === false,
  ].every(Boolean);
}
