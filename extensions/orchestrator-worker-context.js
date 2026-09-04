import {
  BOUNDARY_ENTRY,
  DELIVERY_ENTRY,
  WORKER_MESSAGE_TYPE,
} from "./orchestrator-worker-protocol.js";
import { totalUsage, validUsageBaseline } from "./orchestrator-worker-usage.js";

const ORCHESTRATION_MESSAGE_KEY = `custom:${WORKER_MESSAGE_TYPE}`;
const PRUNABLE_PROVIDER_ROLES = new Set(["assistant", "toolResult"]);
const REPLACEABLE_CONTEXT_KINDS = new Set(["baseline", "run_state"]);

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

export function filterWorkerContext(messages) {
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

export function restoreWorkerState(entries) {
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

export function assignmentUsageBaseline(isAssignment, value, activeAssignment, ctx) {
  if (!isAssignment) return undefined;
  if (activeAssignment?.id === value.assignment_id) return activeAssignment.usageBaseline;
  return totalUsage(ctx);
}
