const MAX_BROKER_FRAME_BYTES = 256 * 1024;
export const BROKER_PROTOCOL_VERSION = 1;
const ROLES = ["implementer", "reviewer", "probe", "playwright", "django"];
const WORKFLOW_STATES = new Set([
  "starting",
  "connecting",
  "initializing",
  "active",
  "needs_attention",
  "ready",
  "uncertain",
]);
const WORKER_STATES = new Set(["disconnected", "idle", "active", "waiting", "uncertain"]);

function exactKeys(value, keys) {
  return value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).sort().join("\0") === [...keys].sort().join("\0");
}

function invalidUnless(condition, code) {
  if (!condition) throw new Error(code);
}

export function brokerFrame(value) {
  const payload = Buffer.from(JSON.stringify(value), "utf8");
  if (!payload.length || payload.length > MAX_BROKER_FRAME_BYTES) {
    throw new Error("observer_frame_too_large");
  }
  const prefix = Buffer.allocUnsafe(4);
  prefix.writeUInt32BE(payload.length);
  return Buffer.concat([prefix, payload]);
}

function validateResponse(value, requestId) {
  invalidUnless(
    exactKeys(value, ["version", "type", "id", "success", "status"])
      && value.id === requestId
      && value.success === true
      && value.status === "observing",
    "invalid_observer_response",
  );
}

function validSnapshotRole(item) {
  return exactKeys(item, ["role", "state"])
    && ROLES.includes(item.role)
    && WORKER_STATES.has(item.state);
}

function validSnapshotRoles(roles) {
  return Array.isArray(roles)
    && roles.length <= ROLES.length
    && roles.every(validSnapshotRole);
}

function validSnapshotRoundAndReplay(value) {
  return [
    Number.isInteger(value.round),
    value.round > 0,
    Number.isInteger(value.report_count),
    value.report_count >= 0,
    typeof value.report_replay_complete === "boolean",
  ].every(Boolean);
}

function validateSnapshot(value) {
  invalidUnless(
    [
      exactKeys(value, [
        "version", "type", "session", "state", "round", "roles",
        "report_count", "report_replay_complete",
      ]),
      WORKFLOW_STATES.has(value.state),
      validSnapshotRoundAndReplay(value),
      validSnapshotRoles(value.roles),
    ].every(Boolean),
    "invalid_observer_snapshot",
  );
}

function validateWorkflow(value) {
  invalidUnless(
    exactKeys(value, ["version", "type", "session", "state", "round"])
      && WORKFLOW_STATES.has(value.state)
      && Number.isInteger(value.round)
      && value.round > 0,
    "invalid_observer_workflow",
  );
}

function validateLifecycle(value) {
  invalidUnless(
    exactKeys(value, ["version", "type", "session", "role", "state"])
      && ROLES.includes(value.role)
      && WORKER_STATES.has(value.state),
    "invalid_observer_lifecycle",
  );
}

const REPORT_USAGE_REQUIRED = ["providerCalls", "input", "output", "cacheRead", "cacheWrite", "cost"];
const REPORT_USAGE_OPTIONAL = ["reasoning", "contextTokens", "contextWindow", "contextPercent", "peakContextTokens"];
const REPORT_USAGE_INTEGERS = ["providerCalls", "input", "output", "cacheRead", "cacheWrite"];
const REPORT_USAGE_OPTIONAL_INTEGERS = ["reasoning", "contextTokens", "contextWindow", "peakContextTokens"];
const REPORT_USAGE_KEYS = new Set([...REPORT_USAGE_REQUIRED, ...REPORT_USAGE_OPTIONAL]);

function plainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function nonnegativeInteger(value) {
  return Number.isInteger(value) && value >= 0;
}

function hasRequiredUsageKeys(value) {
  return REPORT_USAGE_REQUIRED.every((key) => Object.hasOwn(value, key));
}

function hasOnlyUsageKeys(value) {
  return Object.keys(value).every((key) => REPORT_USAGE_KEYS.has(key));
}

function requiredUsageIntegersAreValid(value) {
  return REPORT_USAGE_INTEGERS.every((key) => nonnegativeInteger(value[key]));
}

function optionalUsageIntegersAreValid(value) {
  return REPORT_USAGE_OPTIONAL_INTEGERS.every(
    (key) => value[key] === undefined || nonnegativeInteger(value[key]),
  );
}

function usagePercentIsValid(value) {
  return value.contextPercent === undefined
    || (Number.isFinite(value.contextPercent) && value.contextPercent >= 0);
}

function usageCostIsValid(value) {
  return exactKeys(value.cost, ["total"])
    && Number.isFinite(value.cost.total)
    && value.cost.total >= 0;
}

function validReportUsage(value) {
  if (value === null) return true;
  if (!plainObject(value)) return false;
  return [
    hasRequiredUsageKeys(value),
    hasOnlyUsageKeys(value),
    requiredUsageIntegersAreValid(value),
    optionalUsageIntegersAreValid(value),
    usagePercentIsValid(value),
    usageCostIsValid(value),
  ].every(Boolean);
}

function validReportKeys(value, legacyKeys) {
  return exactKeys(value, legacyKeys)
    || exactKeys(value, [...legacyKeys, "usage"]);
}

function validReportIdentity(value) {
  return /^[a-f0-9]{32}$/.test(value.id)
    && /^[a-f0-9]{32}$/.test(value.assignment_id);
}

function validReportRoleAndRound(value) {
  return ROLES.includes(value.role)
    && Number.isInteger(value.round)
    && value.round > 0;
}

function validReportBody(value, encodedReport) {
  return plainObject(value.report)
    && typeof value.report.summary === "string"
    && value.report.summary.length <= 2000
    && encodedReport.length <= 32 * 1024;
}

function validateReport(value) {
  const encodedReport = Buffer.from(JSON.stringify(value.report ?? null), "utf8");
  const legacyKeys = [
    "version", "type", "session", "id", "assignment_id", "role", "round", "report",
  ];
  invalidUnless(
    [
      validReportKeys(value, legacyKeys),
      value.usage === undefined || validReportUsage(value.usage),
      validReportIdentity(value),
      validReportRoleAndRound(value),
      validReportBody(value, encodedReport),
    ].every(Boolean),
    "invalid_observer_report",
  );
}

const FRAME_VALIDATORS = new Map([
  ["snapshot", validateSnapshot],
  ["workflow", validateWorkflow],
  ["lifecycle", validateLifecycle],
  ["report", validateReport],
]);

export function validateObserverFrame(value, session, requestId) {
  invalidUnless(
    value && value.version === BROKER_PROTOCOL_VERSION && typeof value.type === "string",
    "invalid_observer_frame",
  );
  if (value.type === "response") {
    validateResponse(value, requestId);
    return value;
  }
  invalidUnless(value.session === session, "observer_session_mismatch");
  const validator = FRAME_VALIDATORS.get(value.type);
  invalidUnless(Boolean(validator), "unsupported_observer_frame");
  validator(value);
  return value;
}

export function consumeObserverFrames(state, chunk, onValue) {
  state.buffer = Buffer.concat([state.buffer, chunk]);
  while (state.buffer.length >= 4) {
    const size = state.buffer.readUInt32BE(0);
    invalidUnless(size > 0 && size <= MAX_BROKER_FRAME_BYTES, "invalid_observer_frame_size");
    if (state.buffer.length < size + 4) break;
    const payload = state.buffer.subarray(4, size + 4);
    state.buffer = state.buffer.subarray(size + 4);
    onValue(JSON.parse(payload.toString("utf8")));
  }
  invalidUnless(state.buffer.length <= MAX_BROKER_FRAME_BYTES + 4, "observer_buffer_too_large");
}
