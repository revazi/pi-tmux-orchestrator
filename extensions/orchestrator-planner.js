import { randomUUID } from "node:crypto";
import { availableThinkingLevels } from "./orchestrator-models.js";

const ROLE_ORDER = ["implementer", "reviewer", "probe", "playwright", "django"];
const REQUIRED_ROLES = new Set(["implementer", "reviewer"]);
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
- Optional roles are only probe, playwright, and django.
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

function candidatePriority(candidate, preferredKey, parentKey) {
  const key = candidateKey(candidate);
  if (preferredKey && key === preferredKey) return 0;
  if (key === parentKey) return 1;
  return 2;
}

export function plannerModelCandidates(ctx, preferred) {
  const { scoped, source } = selectedModels(ctx);
  const unique = new Map();
  for (const entry of source.slice(0, MAX_MODEL_SCAN)) {
    const candidate = catalogCandidate(entry, scoped);
    if (!candidate) continue;
    const key = candidateKey(candidate);
    if (!unique.has(key)) unique.set(key, candidate);
  }
  const preferredKey = preferred ? `${preferred.provider}\0${preferred.model}` : undefined;
  const parentKey = `${ctx?.model?.provider || ""}\0${ctx?.model?.id || ""}`;
  return [...unique.values()]
    .sort((left, right) => candidatePriority(left, preferredKey, parentKey)
      - candidatePriority(right, preferredKey, parentKey)
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

function decisionModelShape(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).every((field) => ["provider", "model", "thinking"].includes(field));
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
  return value;
}

export function selectDecisionModel(ctx, requested) {
  const explicit = requestedDecisionModel(requested);
  const candidates = plannerModelCandidates(ctx, explicit);
  if (!candidates.length) throw new Error("no_eligible_decision_model");
  let candidate;
  let source;
  if (explicit) {
    candidate = exactCandidate(candidates, explicit.provider, explicit.model);
    source = "explicit";
    if (!candidate) throw new Error("decision_model_unavailable");
  } else {
    candidate = exactCandidate(candidates, ctx?.model?.provider, ctx?.model?.id);
    source = candidate ? "parent-model-fallback" : "available-model-fallback";
    candidate ??= candidates[0];
  }
  const thinking = explicit?.thinking ?? highestThinking(candidate.thinkingLevels);
  if (!candidate.thinkingLevels.includes(thinking)) {
    throw new Error("decision_model_thinking_unsupported");
  }
  return {
    model: candidate.model,
    provider: candidate.provider,
    modelId: candidate.modelId,
    thinking,
    source,
    candidateCount: candidates.length,
    candidates,
  };
}

export function decisionModelConfirmation(selection) {
  return [
    `Decision model: ${selection.provider}/${selection.modelId}`,
    `Thinking: ${selection.thinking} (dynamic-planning cap=${THINKING_CAP})`,
    `Source: ${selection.source}`,
    `Eligible model candidates: ${selection.candidateCount}`,
    "This makes one additional provider-backed call before preview. It sends the bounded task/context and eligible role/model metadata, starts no workers, and may incur provider usage.",
  ].join("\n");
}

function lockedRoleConstraints(input, selectedRoles) {
  const all = input.modelOverrides?.all || {};
  return selectedRoles.map((role) => {
    const specific = input.modelOverrides?.[role] || {};
    return { role, ...all, ...specific };
  });
}

function plannerPayload(input, project, candidates) {
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
    eligible_roles: ROLE_ORDER,
    mandatory_roles: [...REQUIRED_ROLES],
    optional_role_constraints: enabled,
    candidate_models: candidates.map((candidate) => ({
      provider: candidate.provider,
      model: candidate.modelId,
      thinking_levels: candidate.thinkingLevels,
    })),
    locked_role_constraints: lockedRoleConstraints(input, ROLE_ORDER),
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

function explicitRoleConstraint(input, role) {
  return { ...(input.modelOverrides?.all || {}), ...(input.modelOverrides?.[role] || {}) };
}

function roleIsExplicitlyRequired(input, role, field) {
  return input[field] === true
    || input.forceSpecialists?.includes(role)
    || Boolean(input[OPTIONAL_TASK_FIELDS[role]]);
}

function requiredPlannerRoles(input) {
  const required = new Set(REQUIRED_ROLES);
  for (const [role, field] of Object.entries(OPTIONAL_FIELDS)) {
    const requiredRole = roleIsExplicitlyRequired(input, role, field);
    if (requiredRole) required.add(role);
    if (input[field] === false && requiredRole) {
      throw new Error("dynamic_planning_specialist_constraint_conflict");
    }
  }
  return required;
}

function validateLockedConstraint(locked, candidateRequired, candidates) {
  if ((locked.provider === undefined) !== (locked.model === undefined)) {
    throw new Error("dynamic_planning_model_override_incomplete");
  }
  if (locked.thinking !== undefined && !THINKING_ORDER.includes(locked.thinking)) {
    throw new Error("dynamic_planning_thinking_invalid");
  }
  if (THINKING_ORDER.indexOf(locked.thinking ?? "off") > THINKING_ORDER.indexOf(THINKING_CAP)) {
    throw new Error("dynamic_planning_thinking_exceeds_medium_cap");
  }
  if (!candidateRequired || locked.provider === undefined) return;
  const candidate = exactCandidate(candidates, locked.provider, locked.model);
  if (!candidate) throw new Error("dynamic_planning_locked_model_unavailable");
  if (locked.thinking !== undefined && !candidate.thinkingLevels.includes(locked.thinking)) {
    throw new Error("dynamic_planning_locked_thinking_unsupported");
  }
}

function validatePlannerInputConstraints(input, candidates) {
  const required = requiredPlannerRoles(input);
  for (const role of ROLE_ORDER) {
    validateLockedConstraint(explicitRoleConstraint(input, role), required.has(role), candidates);
  }
}

function validatedDecisionRole(item, seen, candidates, input) {
  if (!exactFields(item, ["role", "provider", "model", "thinking", "reason"])
      || !ROLE_ORDER.includes(item.role)
      || seen.has(item.role)) {
    throw new Error("invalid_planner_role");
  }
  if (!boundedIdentifier(item.provider) || !boundedIdentifier(item.model)) {
    throw new Error("invalid_planner_model");
  }
  const candidate = exactCandidate(candidates, item.provider, item.model);
  if (!candidate || !candidate.thinkingLevels.includes(item.thinking)) {
    throw new Error("unavailable_planner_role_model");
  }
  if (!printableReason(item.reason)) throw new Error("invalid_planner_reason");
  const locked = explicitRoleConstraint(input, item.role);
  if (locked.provider !== undefined && locked.provider !== item.provider) {
    throw new Error("planner_overrode_explicit_model");
  }
  if (locked.model !== undefined && locked.model !== item.model) {
    throw new Error("planner_overrode_explicit_model");
  }
  if (locked.thinking !== undefined && locked.thinking !== item.thinking) {
    throw new Error("planner_overrode_explicit_thinking");
  }
  seen.add(item.role);
  return { ...item };
}

function validateDecisionRoster(seen, input) {
  for (const role of REQUIRED_ROLES) {
    if (!seen.has(role)) throw new Error("planner_removed_required_role");
  }
  for (const [role, field] of Object.entries(OPTIONAL_FIELDS)) {
    if (roleIsExplicitlyRequired(input, role, field) && !seen.has(role)) {
      throw new Error("planner_removed_explicit_role");
    }
    if (input[field] === false && seen.has(role)) throw new Error("planner_added_disabled_role");
  }
}

export function validatePlannerDecision(value, candidates, input) {
  if (!exactFields(value, ["version", "roles"]) || value.version !== 1) {
    throw new Error("invalid_planner_decision");
  }
  if (!Array.isArray(value.roles) || value.roles.length < 2 || value.roles.length > ROLE_ORDER.length) {
    throw new Error("invalid_planner_roles");
  }
  const seen = new Set();
  const roles = value.roles.map((item) => validatedDecisionRole(item, seen, candidates, input));
  validateDecisionRoster(seen, input);
  return {
    version: 1,
    roles: roles.sort((left, right) => ROLE_ORDER.indexOf(left.role) - ROLE_ORDER.indexOf(right.role)),
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

function parsePlannerResponse(response, candidates, input) {
  const text = responseText(response);
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error("planner_response_not_json");
  }
  return validatePlannerDecision(value, candidates, input);
}

function plannedStartInput(input, decision) {
  const selected = new Map(decision.roles.map((role) => [role.role, role]));
  const plannedOverrides = Object.fromEntries(decision.roles.map((role) => [
    role.role,
    { provider: role.provider, model: role.model, thinking: role.thinking },
  ]));
  return {
    ...input,
    withProbe: selected.has("probe"),
    withPlaywright: selected.has("playwright"),
    withDjangoExpert: selected.has("django"),
    projectCustomRoles: false,
    modelOverrides: plannedOverrides,
  };
}

export async function runPreflightPlanner(ctx, input, project, selection, signal) {
  if (typeof ctx?.modelRegistry?.complete !== "function") {
    throw new Error("decision_model_completion_unavailable");
  }
  if (input.projectCustomRoles === true) {
    throw new Error("dynamic_planning_custom_roles_not_supported_yet");
  }
  const candidates = selection.candidates;
  if (!Array.isArray(candidates) || !candidates.length) {
    throw new Error("decision_model_candidates_unavailable");
  }
  validatePlannerInputConstraints(input, candidates);
  const payload = plannerPayload(input, project, candidates);
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
  const decision = parsePlannerResponse(response, candidates, input);
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
