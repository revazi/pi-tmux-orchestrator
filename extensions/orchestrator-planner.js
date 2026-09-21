import { randomUUID } from "node:crypto";
import { availableThinkingLevels } from "./orchestrator-models.js";
import { validCustomRoleId } from "./orchestrator-worker-roles.js";

const ROLE_ORDER = ["implementer", "reviewer", "probe", "playwright", "django"];
const REQUIRED_ROLES = new Set(["implementer", "reviewer"]);
const MAX_CUSTOM_ROLES = 8;
const MAX_PLANNER_ROLES = ROLE_ORDER.length + MAX_CUSTOM_ROLES;
const OPTIONAL_FIELDS = {
  probe: "withProbe",
  playwright: "withPlaywright",
  django: "withDjangoExpert",
};
const OPTIONAL_TASK_FIELDS = {
  probe: "probeTask",
  playwright: "playwrightTask",
  django: "djangoTask",
};
const THINKING_ORDER = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];
const THINKING_CAP = "medium";
const MAX_MODEL_SCAN = 4096;
const MAX_PLANNER_MODELS = 100;
const MAX_PROMPT_BYTES = 96 * 1024;
const MAX_RESPONSE_BYTES = 12 * 1024;
const MAX_REASON_CHARS = 240;
const PLANNER_TIMEOUT_MS = 60_000;
const PLANNER_MAX_TOKENS = 4096;

const SYSTEM_PROMPT = `You are the preflight decision-maker for Pi Tmux Orchestrator.
Choose the smallest useful bounded worker roster and exact model/thinking setting for each selected role.

Hard rules:
- Return exactly one JSON object and no Markdown or commentary.
- The object must have exactly {"version":1,"roles":[...]}.
- Every role object must have exactly: role, provider, model, thinking, reason.
- Include implementer and reviewer exactly once.
- Optional roles are only the exact identities listed in eligible_roles.
- Custom identities are fixed read-only specialists with the listed contract; never create an identity or change a contract.
- Choose only provider/model/thinking combinations listed in candidate_models.
- Honor every locked role, enabled/disabled role, model, and thinking constraint.
- Keep the roster as small as the task permits. The implementer is the only writer and reviewer is mandatory.
- reason must be one concise printable line of at most 240 characters and must not quote private task text.
- Do not choose workflow flow, tools, skills, budgets, context policy, trust, or continuation policy.`;

function boundedIdentifier(value) {
  return typeof value === "string"
    && value.length > 0
    && value.length <= 256
    && !/[\s\u0000-\u001f\u007f]/.test(value);
}

function utf8Bytes(value) {
  return Buffer.byteLength(value, "utf8");
}

function selectedModels(ctx) {
  const scoped = Array.isArray(ctx?.scopedModels) && ctx.scopedModels.length > 0;
  const source = scoped
    ? ctx.scopedModels
    : (typeof ctx?.modelRegistry?.getAvailable === "function"
      ? ctx.modelRegistry.getAvailable()
      : []);
  return { scoped, source: Array.isArray(source) ? source : [] };
}

function cappedThinkingLevels(model, pinnedThinking) {
  const capIndex = THINKING_ORDER.indexOf(THINKING_CAP);
  return availableThinkingLevels(model, pinnedThinking)
    .filter((level) => THINKING_ORDER.indexOf(level) <= capIndex);
}

function catalogCandidate(entry, scoped) {
  const model = scoped ? entry?.model : entry;
  if (!boundedIdentifier(model?.provider) || !boundedIdentifier(model?.id)) return undefined;
  const thinkingLevels = cappedThinkingLevels(model, scoped ? entry?.thinkingLevel : undefined);
  if (!thinkingLevels.length) return undefined;
  return {
    model,
    provider: model.provider,
    modelId: model.id,
    thinkingLevels,
  };
}

function candidateKey(candidate) {
  return `${candidate.provider}\0${candidate.modelId}`;
}

function candidatePriorities(prioritized = []) {
  return new Map(prioritized.map((item, index) => [
    `${item.provider}\0${item.model}`,
    index,
  ]));
}

export function plannerModelCandidates(ctx, prioritized = []) {
  const { scoped, source } = selectedModels(ctx);
  const unique = new Map();
  const seenIdentities = new Set();
  for (const entry of source.slice(0, MAX_MODEL_SCAN)) {
    const model = scoped ? entry?.model : entry;
    if (!boundedIdentifier(model?.provider) || !boundedIdentifier(model?.id)) continue;
    const key = `${model.provider}\0${model.id}`;
    if (seenIdentities.has(key)) throw new Error("ambiguous_decision_model_catalog");
    seenIdentities.add(key);
    const candidate = catalogCandidate(entry, scoped);
    if (candidate) unique.set(key, candidate);
  }
  const priorities = candidatePriorities(prioritized);
  return [...unique.values()]
    .sort((left, right) => (priorities.get(candidateKey(left)) ?? Number.MAX_SAFE_INTEGER)
      - (priorities.get(candidateKey(right)) ?? Number.MAX_SAFE_INTEGER)
      || `${left.provider}/${left.modelId}`.localeCompare(`${right.provider}/${right.modelId}`))
    .slice(0, MAX_PLANNER_MODELS);
}

function exactCandidate(candidates, provider, model) {
  return candidates.find((candidate) => candidate.provider === provider && candidate.modelId === model);
}

function highestThinking(levels) {
  return [...levels].sort(
    (left, right) => THINKING_ORDER.indexOf(right) - THINKING_ORDER.indexOf(left),
  )[0];
}

function decisionModelShape(value, exact = false) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const fields = Object.keys(value);
  const allowed = ["provider", "model", "thinking"];
  return fields.every((field) => allowed.includes(field))
    && (!exact || fields.length === allowed.length && allowed.every((field) => Object.hasOwn(value, field)));
}

function requestedDecisionModel(value) {
  if (value === undefined) return undefined;
  if (!decisionModelShape(value)
      || !boundedIdentifier(value.provider)
      || !boundedIdentifier(value.model)) {
    throw new Error("invalid_decision_model");
  }
  if (value.thinking !== undefined && !THINKING_ORDER.includes(value.thinking)) {
    throw new Error("invalid_decision_model_thinking");
  }
  if (THINKING_ORDER.indexOf(value.thinking ?? "off") > THINKING_ORDER.indexOf(THINKING_CAP)) {
    throw new Error("decision_model_thinking_exceeds_medium_cap");
  }
  return { ...value };
}

function configuredDecisionModel(value) {
  if (!decisionModelShape(value, true)
      || !boundedIdentifier(value.provider)
      || !boundedIdentifier(value.model)
      || !THINKING_ORDER.includes(value.thinking)
      || THINKING_ORDER.indexOf(value.thinking) > THINKING_ORDER.indexOf(THINKING_CAP)) {
    throw new Error("invalid_planner_policy_projection");
  }
  return { provider: value.provider, model: value.model, thinking: value.thinking };
}

export function validateDecisionModelPolicy(value) {
  if (!exactFields(value, ["version", "preferred", "fallbacks", "no_eligible"])
      || value.version !== 1
      || (value.preferred !== null && !decisionModelShape(value.preferred, true))
      || !Array.isArray(value.fallbacks)
      || value.fallbacks.length > 16
      || !["cancel", "static"].includes(value.no_eligible)) {
    throw new Error("invalid_planner_policy_projection");
  }
  const preferred = value.preferred === null ? null : configuredDecisionModel(value.preferred);
  const fallbacks = value.fallbacks.map(configuredDecisionModel);
  const identities = [...(preferred ? [preferred] : []), ...fallbacks]
    .map((item) => `${item.provider}\0${item.model}`);
  if (new Set(identities).size !== identities.length) {
    throw new Error("duplicate_planner_policy_identity");
  }
  return { version: 1, preferred, fallbacks, noEligible: value.no_eligible };
}

function selectedDecision(candidate, thinking, source, candidates) {
  return {
    kind: "model",
    model: candidate.model,
    provider: candidate.provider,
    modelId: candidate.modelId,
    thinking,
    source,
    candidateCount: candidates.length,
    candidates,
  };
}

function explicitDecision(candidates, explicit) {
  const candidate = exactCandidate(candidates, explicit.provider, explicit.model);
  if (!candidate) throw new Error("decision_model_unavailable");
  const thinking = explicit.thinking ?? highestThinking(candidate.thinkingLevels);
  if (!candidate.thinkingLevels.includes(thinking)) {
    throw new Error("decision_model_thinking_unsupported");
  }
  return selectedDecision(candidate, thinking, "per-run", candidates);
}

function configuredDecision(candidates, policy, ordered) {
  for (let index = 0; index < ordered.length; index += 1) {
    const configured = ordered[index];
    const candidate = exactCandidate(candidates, configured.provider, configured.model);
    if (!candidate || !candidate.thinkingLevels.includes(configured.thinking)) continue;
    const preferred = index === 0 && policy.preferred;
    const source = preferred ? "configured-preferred" : "configured-fallback";
    return selectedDecision(candidate, configured.thinking, source, candidates);
  }
  if (policy.noEligible !== "static") throw new Error("no_eligible_decision_model");
  return {
    kind: "static",
    source: "configured-static-fallback",
    candidateCount: candidates.length,
  };
}

export function selectDecisionModel(ctx, requested, configuredPolicy, candidatePriorities = []) {
  const explicit = requestedDecisionModel(requested);
  const policy = validateDecisionModelPolicy(configuredPolicy);
  const ordered = explicit
    ? [explicit]
    : [...(policy.preferred ? [policy.preferred] : []), ...policy.fallbacks];
  const candidates = plannerModelCandidates(ctx, [...ordered, ...candidatePriorities]);
  return explicit
    ? explicitDecision(candidates, explicit)
    : configuredDecision(candidates, policy, ordered);
}

export function decisionModelConfirmation(selection) {
  if (selection.kind !== "model") throw new Error("decision_model_not_selected");
  return [
    `Decision model: ${selection.provider}/${selection.modelId}`,
    `Thinking: ${selection.thinking} (dynamic-planning cap=${THINKING_CAP})`,
    `Source: ${selection.source}`,
    `Eligible model candidates: ${selection.candidateCount}`,
    "This makes one additional provider-backed call before preview. It sends the bounded task/context and eligible role/model metadata, starts no workers, and may incur provider usage.",
  ].join("\n");
}

export function staticPlannerFallbackConfirmation(selection) {
  if (selection.kind !== "static") throw new Error("invalid_static_planner_fallback");
  return [
    "No configured decision-model identity is eligible in Pi's current available/scoped catalog.",
    `Configured action: static/manual start (source=${selection.source}).`,
    "Continuing makes no preflight provider call. The ordinary CLI preview and final launch confirmation still apply.",
  ].join("\n");
}

function topologyConstraint(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)
      || Object.keys(value).some((field) => !["provider", "model", "thinking"].includes(field))
      || (value.provider === undefined) !== (value.model === undefined)
      || (value.provider !== undefined && !boundedIdentifier(value.provider))
      || (value.model !== undefined && !boundedIdentifier(value.model))
      || (value.thinking !== undefined && !THINKING_ORDER.includes(value.thinking))) {
    throw new Error("invalid_planner_topology_constraint");
  }
  return { ...value };
}

export function validatePlannerTopology(value) {
  if (!exactFields(value, ["version", "builtins", "optional_roles", "custom_roles"])
      || value.version !== 1
      || !value.builtins || typeof value.builtins !== "object" || Array.isArray(value.builtins)
      || Object.keys(value.builtins).length !== ROLE_ORDER.length
      || !ROLE_ORDER.every((role) => exactFields(value.builtins[role], ["constraint"]))
      || !Array.isArray(value.optional_roles)
      || value.optional_roles.some((role) => !Object.hasOwn(OPTIONAL_FIELDS, role))
      || new Set(value.optional_roles).size !== value.optional_roles.length
      || !Array.isArray(value.custom_roles)
      || value.custom_roles.length > MAX_CUSTOM_ROLES) {
    throw new Error("invalid_planner_topology_projection");
  }
  const builtins = Object.fromEntries(ROLE_ORDER.map((role) => [
    role,
    { constraint: topologyConstraint(value.builtins[role].constraint) },
  ]));
  const customRoles = value.custom_roles.map((item) => {
    if (!exactFields(item, ["role", "contract", "provider", "model", "thinking"])
        || !validCustomRoleId(item.role)
        || !Object.hasOwn(OPTIONAL_FIELDS, item.contract)
        || !boundedIdentifier(item.provider)
        || !boundedIdentifier(item.model)
        || !THINKING_ORDER.includes(item.thinking)) {
      throw new Error("invalid_planner_custom_role");
    }
    return { ...item };
  });
  if (new Set(customRoles.map((item) => item.role)).size !== customRoles.length) {
    throw new Error("duplicate_planner_custom_role");
  }
  return {
    version: 1,
    builtins,
    optionalRoles: ROLE_ORDER.filter((role) => value.optional_roles.includes(role)),
    customRoles: customRoles.sort((left, right) => left.role.localeCompare(right.role)),
  };
}

function explicitRoleConstraint(input, role, topology) {
  const configured = topology.builtins[role]?.constraint || {};
  return {
    ...configured,
    ...(input.modelOverrides?.all || {}),
    ...(input.modelOverrides?.[role] || {}),
  };
}

function roleIsExplicitlyRequired(input, role, field) {
  return input[field] === true
    || input.forceSpecialists?.includes(role)
    || Boolean(input[OPTIONAL_TASK_FIELDS[role]]);
}

function eligibleCustomRoles(input, topology, candidates, required) {
  if (input.projectCustomRoles === false) return [];
  return topology.customRoles.filter((role) => {
    const candidate = exactCandidate(candidates, role.provider, role.model);
    const eligible = candidate?.thinkingLevels.includes(role.thinking) === true;
    if (input.projectCustomRoles === true && !eligible) {
      throw new Error("dynamic_planning_custom_role_model_unavailable");
    }
    if (input.projectCustomRoles === true) required.add(role.role);
    return eligible;
  });
}

function eligiblePlannerRoles(input, topology, candidates) {
  const roles = ROLE_ORDER.filter((role) => {
    if (REQUIRED_ROLES.has(role)) return true;
    const field = OPTIONAL_FIELDS[role];
    if (input[field] === false) return false;
    return roleIsExplicitlyRequired(input, role, field) || topology.optionalRoles.includes(role);
  });
  const required = new Set(REQUIRED_ROLES);
  for (const [role, field] of Object.entries(OPTIONAL_FIELDS)) {
    if (roleIsExplicitlyRequired(input, role, field)) required.add(role);
    if (input[field] === false && required.has(role)) {
      throw new Error("dynamic_planning_specialist_constraint_conflict");
    }
  }
  const custom = eligibleCustomRoles(input, topology, candidates, required);
  return {
    roles: [...roles, ...custom.map((item) => item.role)],
    required,
    custom: new Map(custom.map((item) => [item.role, item])),
  };
}

function lockedRoleConstraints(input, policy, topology) {
  return policy.roles.map((role) => {
    const custom = policy.custom.get(role);
    const constraint = custom
      ? { provider: custom.provider, model: custom.model, thinking: custom.thinking }
      : explicitRoleConstraint(input, role, topology);
    return { role, ...constraint };
  });
}

function plannerPayload(input, project, candidates, policy, topology) {
  const enabled = {};
  const roleTasks = {};
  for (const [role, field] of Object.entries(OPTIONAL_FIELDS)) {
    const task = input[OPTIONAL_TASK_FIELDS[role]];
    if (typeof input[field] === "boolean") enabled[role] = input[field];
    if (input.forceSpecialists?.includes(role) || task) enabled[role] = true;
    if (task) roleTasks[role] = String(task);
  }
  return {
    task: String(input.task),
    role_tasks: roleTasks,
    context_capsule: input.contextCapsule ?? null,
    project,
    eligible_roles: policy.roles.map((role) => {
      const custom = policy.custom.get(role);
      return {
        role,
        contract: custom?.contract ?? role,
        authority: role === "implementer"
          ? "writer"
          : (role === "reviewer" ? "mandatory-reviewer" : "read-only-specialist"),
      };
    }),
    mandatory_roles: [...policy.required],
    optional_role_constraints: enabled,
    candidate_models: candidates.map((candidate) => ({
      provider: candidate.provider,
      model: candidate.modelId,
      thinking_levels: candidate.thinkingLevels,
    })),
    locked_role_constraints: lockedRoleConstraints(input, policy, topology),
    thinking_cap: THINKING_CAP,
  };
}

function exactFields(value, fields) {
  return value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === fields.length
    && fields.every((field) => Object.hasOwn(value, field));
}

function printableReason(value) {
  return typeof value === "string"
    && value.trim() === value
    && value.length > 0
    && value.length <= MAX_REASON_CHARS
    && !/[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/u.test(value);
}

function validateLockedConstraint(locked, candidates) {
  if ((locked.provider === undefined) !== (locked.model === undefined)) {
    throw new Error("dynamic_planning_model_override_incomplete");
  }
  if (locked.thinking !== undefined && !THINKING_ORDER.includes(locked.thinking)) {
    throw new Error("dynamic_planning_thinking_invalid");
  }
  if (THINKING_ORDER.indexOf(locked.thinking ?? "off") > THINKING_ORDER.indexOf(THINKING_CAP)) {
    throw new Error("dynamic_planning_thinking_exceeds_medium_cap");
  }
  if (locked.provider === undefined) return;
  const candidate = exactCandidate(candidates, locked.provider, locked.model);
  if (!candidate) throw new Error("dynamic_planning_locked_model_unavailable");
  if (locked.thinking !== undefined && !candidate.thinkingLevels.includes(locked.thinking)) {
    throw new Error("dynamic_planning_locked_thinking_unsupported");
  }
}

function roleConstraint(input, role, policy, topology) {
  const custom = policy.custom.get(role);
  return custom
    ? { provider: custom.provider, model: custom.model, thinking: custom.thinking }
    : explicitRoleConstraint(input, role, topology);
}

function validatePlannerInputConstraints(input, candidates, policy, topology) {
  for (const role of policy.roles) {
    validateLockedConstraint(roleConstraint(input, role, policy, topology), candidates);
  }
}

function validateDecisionRoleIdentity(item, seen, input, policy) {
  if (!exactFields(item, ["role", "provider", "model", "thinking", "reason"])
      || seen.has(item.role)) {
    throw new Error("invalid_planner_role");
  }
  if (policy.roles.includes(item.role)) return;
  const disabledField = OPTIONAL_FIELDS[item.role];
  if (disabledField !== undefined && input[disabledField] === false) {
    throw new Error("planner_added_disabled_role");
  }
  throw new Error("invalid_planner_role");
}

function validateDecisionRoleModel(item, candidates) {
  if (!boundedIdentifier(item.provider) || !boundedIdentifier(item.model)) {
    throw new Error("invalid_planner_model");
  }
  const candidate = exactCandidate(candidates, item.provider, item.model);
  if (!candidate || !candidate.thinkingLevels.includes(item.thinking)) {
    throw new Error("unavailable_planner_role_model");
  }
  if (!printableReason(item.reason)) throw new Error("invalid_planner_reason");
}

function validateDecisionRoleConstraint(item, locked) {
  if ((locked.provider !== undefined && locked.provider !== item.provider)
      || (locked.model !== undefined && locked.model !== item.model)) {
    throw new Error("planner_overrode_explicit_model");
  }
  if (locked.thinking !== undefined && locked.thinking !== item.thinking) {
    throw new Error("planner_overrode_explicit_thinking");
  }
}

function validatedDecisionRole(item, seen, candidates, input, policy, topology) {
  validateDecisionRoleIdentity(item, seen, input, policy);
  validateDecisionRoleModel(item, candidates);
  validateDecisionRoleConstraint(
    item,
    roleConstraint(input, item.role, policy, topology),
  );
  seen.add(item.role);
  const custom = policy.custom.get(item.role);
  return {
    ...item,
    ...(custom ? { specialistContract: custom.contract } : {}),
  };
}

function validateDecisionRoster(seen, input, policy) {
  for (const role of policy.required) {
    if (!seen.has(role)) {
      throw new Error(REQUIRED_ROLES.has(role)
        ? "planner_removed_required_role"
        : "planner_removed_explicit_role");
    }
  }
  for (const [role, field] of Object.entries(OPTIONAL_FIELDS)) {
    if (input[field] === false && seen.has(role)) throw new Error("planner_added_disabled_role");
  }
}

function compatibilityTopology() {
  return {
    version: 1,
    builtins: Object.fromEntries(ROLE_ORDER.map((role) => [role, { constraint: {} }])),
    optionalRoles: Object.keys(OPTIONAL_FIELDS),
    customRoles: [],
  };
}

export function validatePlannerDecision(value, candidates, input, policy, topology) {
  topology ??= compatibilityTopology();
  policy ??= eligiblePlannerRoles(input, topology, candidates);
  if (!exactFields(value, ["version", "roles"]) || value.version !== 1) {
    throw new Error("invalid_planner_decision");
  }
  if (!Array.isArray(value.roles) || value.roles.length < 2
      || value.roles.length > MAX_PLANNER_ROLES
      || value.roles.length > policy.roles.length) {
    throw new Error("invalid_planner_roles");
  }
  const seen = new Set();
  const roles = value.roles.map(
    (item) => validatedDecisionRole(item, seen, candidates, input, policy, topology),
  );
  validateDecisionRoster(seen, input, policy);
  return {
    version: 1,
    roles: roles.sort(
      (left, right) => policy.roles.indexOf(left.role) - policy.roles.indexOf(right.role),
    ),
  };
}

function responseText(response) {
  if (response?.stopReason !== "stop" || !Array.isArray(response.content)) {
    throw new Error("planner_response_incomplete");
  }
  const text = response.content
    .filter((item) => item?.type === "text" && typeof item.text === "string")
    .map((item) => item.text)
    .join("\n");
  if (!text || utf8Bytes(text) > MAX_RESPONSE_BYTES) throw new Error("planner_response_invalid_size");
  return text;
}

function parsePlannerResponse(response, candidates, input, policy, topology) {
  const text = responseText(response);
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error("planner_response_not_json");
  }
  return validatePlannerDecision(value, candidates, input, policy, topology);
}

function plannedStartInput(input, decision) {
  const selected = new Map(decision.roles.map((role) => [role.role, role]));
  const plannedOverrides = Object.fromEntries(decision.roles
    .filter((role) => ROLE_ORDER.includes(role.role))
    .map((role) => [
      role.role,
      { provider: role.provider, model: role.model, thinking: role.thinking },
    ]));
  const plannedCustomRoleIds = decision.roles
    .map((role) => role.role)
    .filter((role) => validCustomRoleId(role));
  return {
    ...input,
    withProbe: selected.has("probe"),
    withPlaywright: selected.has("playwright"),
    withDjangoExpert: selected.has("django"),
    projectCustomRoles: plannedCustomRoleIds.length > 0,
    plannedCustomRoleIds,
    modelOverrides: plannedOverrides,
  };
}

export async function runPreflightPlanner(ctx, input, project, selection, topologyValue, signal) {
  if (typeof ctx?.modelRegistry?.complete !== "function") {
    throw new Error("decision_model_completion_unavailable");
  }
  const topology = topologyValue;
  const candidates = selection.candidates;
  if (!Array.isArray(candidates) || !candidates.length) {
    throw new Error("decision_model_candidates_unavailable");
  }
  const policy = eligiblePlannerRoles(input, topology, candidates);
  validatePlannerInputConstraints(input, candidates, policy, topology);
  const payload = plannerPayload(input, project, candidates, policy, topology);
  const serialized = JSON.stringify(payload);
  if (utf8Bytes(serialized) > MAX_PROMPT_BYTES) throw new Error("planner_input_too_large");
  const response = await ctx.modelRegistry.complete(
    selection.model,
    {
      systemPrompt: SYSTEM_PROMPT,
      messages: [{
        role: "user",
        content: [{ type: "text", text: serialized }],
        timestamp: Date.now(),
      }],
    },
    {
      reasoning: selection.thinking,
      maxTokens: PLANNER_MAX_TOKENS,
      timeoutMs: PLANNER_TIMEOUT_MS,
      maxRetries: 0,
      cacheRetention: "none",
      sessionId: randomUUID(),
      signal,
    },
  );
  const decision = parsePlannerResponse(response, candidates, input, policy, topology);
  return {
    input: plannedStartInput(input, decision),
    plan: {
      version: decision.version,
      decisionModel: {
        provider: selection.provider,
        model: selection.modelId,
        thinking: selection.thinking,
        source: selection.source,
      },
      roles: decision.roles,
      usage: response.usage,
    },
  };
}

export function plannerPlanConfirmation(plan) {
  if (!plan) return "Static/manual policy (no preflight model call)";
  const roles = plan.roles.map(
    (role) => `${role.role}: ${role.provider}/${role.model} thinking=${role.thinking} — ${role.reason}`,
  );
  return [
    `Decision model: ${plan.decisionModel.provider}/${plan.decisionModel.model} thinking=${plan.decisionModel.thinking} source=${plan.decisionModel.source}`,
    `Selected ${plan.roles.length} workers:`,
    ...roles,
  ].join("\n");
}

export const plannerTestHooks = {
  MAX_PROMPT_BYTES,
  MAX_RESPONSE_BYTES,
  PLANNER_MAX_TOKENS,
  PLANNER_TIMEOUT_MS,
  ROLE_ORDER,
  SYSTEM_PROMPT,
  THINKING_CAP,
};
