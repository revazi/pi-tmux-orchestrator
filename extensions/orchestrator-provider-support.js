// Model-free admission. Names are identifiers only; no provider-quality inference.
import { plannerEvidenceDigest } from "./orchestrator-planning.js";

export const SUPPORT_CHOICES = ["none", "reasoning", "image", "context_window", "max_output_tokens", "thinking", "cache_short", "cache_long", "declared_cost"];
const LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];
export const SUPPORT_ERROR = "Unsupported single-provider plan: choose revised constraints, exact overrides, explicit static/manual planning, or cancel; no retry or substitute plan.";

function costAdvantage(left, right) {
  // Comparable declared hints only, never billing. Require >=20% improvement in
  // both positive base input/output rates, no tier ambiguity or cache tradeoff.
  if (left.status !== "declared" || right.status !== "declared" || left.tiers.length || right.tiers.length) return false;
  return ["input", "output"].every((field) => left[field] > 0 && right[field] > 0 && left[field] <= right[field] * 0.8)
    && ["cache_read", "cache_write"].every((field) => left[field] <= right[field]);
}

function advantage(reference, selected, other, thinking, levels) {
  const left = selected.capabilities;
  const right = other.capabilities;
  if (reference === "reasoning") return left.reasoning === true && right.reasoning === false;
  if (reference === "image") return left.input.image === true && right.input.image === false;
  if (["context_window", "max_output_tokens"].includes(reference)) {
    const a = left[reference], b = right[reference];
    return a.status === "declared" && b.status === "declared" && a.tokens > 0 && b.tokens > 0 && a.tokens >= b.tokens * 1.25;
  }
  if (reference === "thinking") return levels.every((level) => LEVELS.indexOf(thinking) > LEVELS.indexOf(level));
  if (reference.startsWith("cache_")) {
    const field = reference.slice(6);
    return left.prompt_cache_retention.status === "declared" && left.prompt_cache_retention[field] === true
      && ["declared", "missing"].includes(right.prompt_cache_retention.status) && right.prompt_cache_retention[field] === false;
  }
  if (reference === "declared_cost") return costAdvantage(left.declared_cost, right.declared_cost);
  return false;
}

export function supportIdentity(reference, facts, catalog) {
  return { support: reference, applicability: reference === "none" ? "unavailable" : "material_task_advantage_all_selected_roles", facts: plannerEvidenceDigest({ catalog, composition: facts }) };
}

// Linear scans per selected role, at most 13 roles x 100 candidates. No product
// search. Optional roster decisions cannot themselves provide material support.
export function deriveProviderSupport(reference, composition, facts, catalog, roles) {
  if (!SUPPORT_CHOICES.includes(reference)) throw new Error(SUPPORT_ERROR);
  const selectedRoles = facts.roles.filter((role) => roles.some((item) => item.role === role.role || item.id === role.role));
  const providers = selectedRoles.map((role) => new Set(role.candidates.map((item) => item.provider)));
  const single = [...providers[0]].some((provider) => providers.every((items) => items.has(provider)));
  const mixed = new Set(providers.flatMap((items) => [...items])).size > 1;
  const feasible = [...(single ? ["single_provider"] : []), ...(mixed ? ["mixed_provider"] : [])];
  const actual = new Set(roles.map((item) => item.provider)).size === 1 ? "single_provider" : "mixed_provider";
  if (!feasible.includes(actual) || composition !== "no_material_preference" && composition !== actual) throw new Error(SUPPORT_ERROR);
  const base = { source: "derived", state: feasible.length === 1 ? "locked_fixed" : actual === "mixed_provider" ? "mixed" : "supported", feasible, reference, roles: [] };
  if (base.state !== "supported") {
    if (reference !== "none") throw new Error(SUPPORT_ERROR);
    return base;
  }
  if (composition !== "single_provider" || reference === "none") throw new Error(SUPPORT_ERROR);
  const lookup = new Map(catalog.map((item) => [`${item.provider}\0${item.model}`, item]));
  base.roles = roles.map((assignment) => supportingRole(reference, assignment, selectedRoles, lookup));
  return base;
}

function supportingRole(reference, assignment, selectedRoles, lookup) {
  const role = assignment.role ?? assignment.id;
  const eligible = selectedRoles.find((item) => item.role === role).candidates;
  const selected = lookup.get(`${assignment.provider}\0${assignment.model}`);
  const alternatives = eligible.filter((item) => item.provider !== assignment.provider);
  // Vacuous coverage is forbidden, including exact-locked/custom roles.
  if (!selected || !alternatives.length || !alternatives.every((item) => advantage(reference, selected, lookup.get(`${item.provider}\0${item.model}`), assignment.thinking, item.thinking_levels))) throw new Error(SUPPORT_ERROR);
  return { role, selected: { provider: selected.provider, model: selected.model, thinking: assignment.thinking, facts: plannerEvidenceDigest(selected) }, alternatives };
}

export function providerSupportSummary(evidence) {
  const support = evidence?.provider_support;
  if (!support) return { state: "unavailable" };
  return { source: support.source, state: support.state, reference: support.reference, feasible: support.feasible, role_count: support.roles.length };
}

export function providerSupportLine(evidence) {
  const item = evidence?.projection === "summary" ? evidence.provider_support : providerSupportSummary(evidence);
  return `Single-provider support: ${item?.state ?? "unavailable"}; ${item?.source ?? "unavailable"}${item?.reference ? ` reference=${item.reference}` : ""} (not planner confidence or reasoning).`;
}
