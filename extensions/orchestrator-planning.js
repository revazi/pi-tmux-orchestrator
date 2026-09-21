import { createHash } from "node:crypto";

const DIGEST_PATTERN = /^[a-f0-9]{64}$/;

function canonicalValue(value) {
  if (Array.isArray(value)) return value.map((item) => canonicalValue(item));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map(
      (key) => [key, canonicalValue(value[key])],
    ));
  }
  return value;
}

export function metadataDigest(value) {
  return createHash("sha256").update(JSON.stringify(canonicalValue(value)), "utf8").digest("hex");
}

export function plannerCandidateDigest(candidates) {
  return metadataDigest(candidates.map((candidate) => ({
    provider: candidate.provider,
    model: candidate.modelId,
    thinking_levels: candidate.thinkingLevels,
  })));
}

function planningUsage(value) {
  if (!value || typeof value !== "object") return null;
  const number = (item) => (typeof item === "number" && Number.isFinite(item) && item >= 0
    ? item
    : null);
  return {
    input_tokens: number(value.input),
    output_tokens: number(value.output),
    cache_read_tokens: number(value.cacheRead),
    cache_write_tokens: number(value.cacheWrite),
    total_tokens: number(value.totalTokens),
    cost_total: number(value.cost?.total),
  };
}

function retainedDecisionSource(source) {
  if (source === "per-run") return "explicit";
  if (source === "configured-preferred" || source === "configured-fallback") return source;
  throw new Error("invalid_planning_decision_source");
}

function planningRoles(roles) {
  return roles.map((role) => ({
    id: role.role,
    contract: role.specialistContract ?? role.role,
    provider: role.provider,
    model: role.model,
    thinking: role.thinking,
  }));
}

function validPlanningBindings(bindings) {
  if (!bindings) return false;
  return [bindings.plannerPolicy, bindings.topologyPolicy, bindings.candidateSet]
    .every((value) => DIGEST_PATTERN.test(value));
}

export function planningRecordForPreview(plan) {
  const roles = planningRoles(plan.roles);
  const decision = metadataDigest({ version: 1, roles });
  const bindings = plan.bindings;
  if (!validPlanningBindings(bindings)) throw new Error("invalid_planning_bindings");
  return {
    version: 1,
    mode: "dynamic",
    request_id: plan.requestId,
    status: "accepted",
    created_at_ms: plan.createdAtMs,
    accepted_at_ms: plan.acceptedAtMs,
    decision_schema_version: plan.version,
    decision_model: {
      provider: plan.decisionModel.provider,
      model: plan.decisionModel.model,
      thinking: plan.decisionModel.thinking,
      source: retainedDecisionSource(plan.decisionModel.source),
    },
    roles,
    bindings: {
      input: null,
      start_config: null,
      planner_policy: bindings.plannerPolicy,
      topology_policy: bindings.topologyPolicy,
      candidate_set: bindings.candidateSet,
      decision,
    },
    usage: planningUsage(plan.usage),
  };
}
