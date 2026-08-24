const LEVELS = ["warning", "hard"];
const SCOPES = ["run", "role", "assignment"];
const INTEGER_METRICS = [
  "provider_calls",
  "input_tokens",
  "output_tokens",
  "cache_read_tokens",
  "cache_write_tokens",
  "reasoning_tokens",
  "operational_tokens",
  "context_tokens",
];
const NUMBER_METRICS = ["cost_total", "context_percent"];
const METRICS = [...INTEGER_METRICS, ...NUMBER_METRICS];
const MAX_INTEGER = 1_000_000_000_000;
const MAX_COST = 1_000_000_000;
const METRIC_TYPES = Object.fromEntries([
  ...INTEGER_METRICS.map((metric) => [metric, "integer"]),
  ...NUMBER_METRICS.map((metric) => [metric, "number"]),
]);
const METRIC_MAXIMUMS = {
  ...Object.fromEntries(INTEGER_METRICS.map((metric) => [metric, MAX_INTEGER])),
  cost_total: MAX_COST,
  context_percent: 100,
};

function metricSchema(metric) {
  return {
    type: [METRIC_TYPES[metric], "null"],
    exclusiveMinimum: 0,
    maximum: METRIC_MAXIMUMS[metric],
    description: "Numeric threshold, or null to disable the configured threshold for this run",
  };
}

const thresholdParameters = {
  type: "object",
  additionalProperties: false,
  properties: Object.fromEntries(METRICS.map((metric) => [metric, metricSchema(metric)])),
};

const levelParameters = {
  type: "object",
  additionalProperties: false,
  properties: Object.fromEntries(SCOPES.map((scope) => [scope, thresholdParameters])),
};

export const budgetOverrideParameters = {
  type: "object",
  additionalProperties: false,
  description: "Explicit per-run budget overrides; user-global policy supplies omitted values",
  properties: {
    enforcement: { type: "string", enum: ["warn-only", "hard"] },
    warning: levelParameters,
    hard: levelParameters,
  },
};

function positiveBounded(value, maximum) {
  return value > 0 && value <= maximum;
}

function validInteger(value, maximum) {
  return Number.isSafeInteger(value) && positiveBounded(value, maximum);
}

function validNumber(value, maximum) {
  return typeof value === "number" && Number.isFinite(value) && positiveBounded(value, maximum);
}

function validBudgetValue(metric, value) {
  if (value === null) return "off";
  const validator = INTEGER_METRICS.includes(metric) ? validInteger : validNumber;
  if (!validator(value, METRIC_MAXIMUMS[metric])) throw new Error("invalid_budget_override");
  return String(value);
}

function appendEnforcement(args, enforcement) {
  if (enforcement === undefined) return;
  if (!["warn-only", "hard"].includes(enforcement)) throw new Error("invalid_budget_enforcement");
  args.push("--budget-enforcement", enforcement);
}

function budgetEntries(overrides) {
  return LEVELS.flatMap((level) =>
    SCOPES.flatMap((scope) =>
      Object.entries(overrides[level]?.[scope] || {})
        .map(([metric, value]) => ({ level, scope, metric, value }))));
}

export function appendBudgetArgs(args, input) {
  const overrides = input.budgetOverrides;
  if (!overrides) return;
  appendEnforcement(args, overrides.enforcement);
  for (const { level, scope, metric, value } of budgetEntries(overrides)) {
    if (!METRICS.includes(metric)) throw new Error("invalid_budget_metric");
    args.push(
      "--budget-override",
      `${level}.${scope}.${metric}=${validBudgetValue(metric, value)}`,
    );
  }
}

function renderedThresholds(thresholds) {
  const rendered = Object.entries(thresholds)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([metric, value]) => `${metric}=${value}`)
    .join(", ");
  return rendered || "off";
}

function budgetScopeLines(policy) {
  return LEVELS.flatMap((level) =>
    SCOPES.map((scope) =>
      `${level}.${scope}: ${renderedThresholds(policy[level]?.[scope] || {})}`));
}

export function budgetConfirmation(policy) {
  if (!policy || typeof policy !== "object") return "unavailable";
  return [`mode=${policy.enforcement} (observational)`, ...budgetScopeLines(policy)].join("\n");
}
