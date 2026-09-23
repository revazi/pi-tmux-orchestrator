import { randomUUID } from "node:crypto";
import {
  availableThinkingLevels,
  projectModelCapabilities,
} from "./orchestrator-models.js";
import {
  metadataDigest,
  plannerCandidateDigest,
  publicPlannerCandidate,
} from "./orchestrator-planning.js";
import { validCustomRoleId } from "./orchestrator-worker-roles.js";
import {
  normalizedTypeSafeApiKey,
  requestTypeSafe,
  TYPESAFE_MODEL,
} from "./orchestrator-typesafe.js";

export {
  metadataDigest,
  plannerCandidateDigest,
  planningRecordForPreview,
  publicPlannerCandidate,
} from "./orchestrator-planning.js";
export { projectModelCapabilities } from "./orchestrator-models.js";

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
const MAX_MODEL_SCAN = 4096;
const MAX_PLANNER_MODELS = 100;
const MAX_PROMPT_BYTES = 96 * 1024;
const MAX_RESPONSE_BYTES = 12 * 1024;
const MAX_REASON_CHARS = 240;
const PLANNER_TIMEOUT_MS = 60_000;
const PLANNER_MAX_TOKENS = 4096;
const TYPESAFE_PROVIDER = "typesafe";
const TYPESAFE_SOURCE = "typesafe-auth";
const TYPESAFE_MAX_CHOICE_OPTIONS = 255;
const TYPESAFE_MAX_QUESTIONS = 255;
const TYPESAFE_MODEL_PATTERN = /^jev-[a-z0-9.-]+$/;

const SYSTEM_PROMPT = `You are the preflight decision-maker for Pi Tmux Orchestrator.
Choose the smallest useful bounded worker roster and the smallest sufficient exact model/thinking setting for each selected role.

Hard rules:
- Return exactly one JSON object and no Markdown or commentary.
- The object must have exactly {"version":1,"roles":[...]}.
- Every role object must have exactly: role, provider, model, thinking, reason.
- Include implementer and reviewer exactly once.
- Optional roles are only the exact identities listed in eligible_roles.
- Custom identities are fixed read-only specialists with the listed contract; never create an identity or change a contract.
- candidate_models is the authoritative exact Pi model scope: a non-empty scopedModels selection when present, otherwise the current available catalog. Choose only provider/model/thinking combinations listed there; never invent an identity.
- Use only listed technical capabilities and declared catalog cost hints.
- Prefer the smallest sufficient model for the role's technical needs and declared cost.
- Do not infer quality, coding skill, latency, or reliability from model names.
- Declared rates are catalog hints, not billing, observed spend, or runtime eligibility.
- Treat missing, zero, or unavailable capability/cost metadata as explicit unknowns; never guess or fill them in.
- Honor every locked role, enabled/disabled role, model, and thinking constraint. Locked exact overrides remain authoritative even when another candidate looks cheaper or larger.
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

function workerThinkingLevels(model, pinnedThinking) {
  return availableThinkingLevels(model, pinnedThinking);
}

function catalogCandidate(entry, scoped) {
  const model = scoped ? entry?.model : entry;
  if (!boundedIdentifier(model?.provider) || !boundedIdentifier(model?.id)) return undefined;
  const thinkingLevels = workerThinkingLevels(model, scoped ? entry?.thinkingLevel : undefined);
  if (!thinkingLevels.length) return undefined;
  return {
    model,
    provider: model.provider,
    modelId: model.id,
    thinkingLevels,
    capabilities: projectModelCapabilities(model),
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
  return { ...value };
}

function configuredDecisionModel(value) {
  if (!decisionModelShape(value, true)
      || !boundedIdentifier(value.provider)
      || !boundedIdentifier(value.model)
      || !THINKING_ORDER.includes(value.thinking)) {
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

function typesafeDecision(candidates) {
  return {
    kind: "typesafe",
    provider: TYPESAFE_PROVIDER,
    modelId: TYPESAFE_MODEL,
    thinking: "off",
    source: TYPESAFE_SOURCE,
    candidateCount: candidates.length,
    candidates,
  };
}

export function selectDecisionModel(
  ctx,
  requested,
  configuredPolicy,
  candidatePriorities = [],
  options = {},
) {
  const explicit = requestedDecisionModel(requested);
  const policy = validateDecisionModelPolicy(configuredPolicy);
  const useTypeSafe = Boolean(normalizedTypeSafeApiKey(options.typesafeApiKey));
  const ordered = useTypeSafe
    ? []
    : (explicit
      ? [explicit]
      : [...(policy.preferred ? [policy.preferred] : []), ...policy.fallbacks]);
  const resolvedPriorities = [...ordered, ...candidatePriorities];
  const candidates = plannerModelCandidates(ctx, resolvedPriorities);
  let selection;
  if (useTypeSafe) selection = typesafeDecision(candidates);
  else if (explicit) selection = explicitDecision(candidates, explicit);
  else selection = configuredDecision(candidates, policy, ordered);
  return { ...selection, candidatePriorities: resolvedPriorities };
}

export function decisionModelConfirmation(selection) {
  if (selection.kind === "typesafe") {
    return [
      `Decision model: TypeSafe/${selection.modelId}`,
      `Source: ${selection.source} (TypeSafe authentication is configured; credential value is not retained in the plan)`,
      `Eligible Pi worker-model candidates: ${selection.candidateCount}`,
      "Payload categories: bounded task/context, canonical project identity, fixed role/contract authority, exact candidate model/thinking/capability metadata, declared catalog cost hints, and locked operator/config constraints.",
      "Declared catalog rates are hints, not observed spend, billing, quality, or runtime eligibility. No model-name quality inference is used.",
      "The credential is used only as an in-memory HTTPS Authorization header. No credential, endpoint, custom resource body, tool, or configuration mutation surface is included in the planning state. This one TypeSafe call starts no workers and may incur provider usage.",
    ].join("\n");
  }
  if (selection.kind !== "model") throw new Error("decision_model_not_selected");
  return [
    `Decision model: ${selection.provider}/${selection.modelId}`,
    `Thinking: ${selection.thinking} (must be supported by the selected decision model)`,
    `Source: ${selection.source}`,
    `Eligible model candidates: ${selection.candidateCount}`,
    "Payload categories: bounded task/context, canonical project identity, fixed role/contract authority, exact candidate model/thinking/capability metadata, declared catalog cost hints, and locked operator/config constraints.",
    "Declared catalog rates are hints, not observed spend, billing, quality, or runtime eligibility. No model-name quality inference is used.",
    "No credentials, endpoints, custom resource bodies, tools, or configuration mutation surface are sent. This additional call starts no workers and may incur provider usage.",
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

function validRequiredCustomRoleIds(value) {
  if (!Array.isArray(value)) return false;
  if (value.length > MAX_CUSTOM_ROLES) return false;
  if (new Set(value).size !== value.length) return false;
  return value.every((role) => validCustomRoleId(role));
}

function validateRequiredCustomRolePolicy(input, roleIds) {
  if (input.projectCustomRoles === false && roleIds.length) {
    throw new Error("dynamic_planning_custom_role_constraint_conflict");
  }
}

function validateRequiredCustomRoleAllowlist(topology, roleIds) {
  const configured = new Set(topology.customRoles.map((role) => role.role));
  if (roleIds.some((role) => !configured.has(role))) {
    throw new Error("dynamic_planning_custom_role_not_allowlisted");
  }
}

function requiredCustomRoleIds(input, topology) {
  if (input.requiredCustomRoleIds === undefined) return [];
  const roleIds = input.requiredCustomRoleIds;
  if (!validRequiredCustomRoleIds(roleIds)) {
    throw new Error("dynamic_planning_custom_role_constraint_invalid");
  }
  validateRequiredCustomRolePolicy(input, roleIds);
  validateRequiredCustomRoleAllowlist(topology, roleIds);
  return roleIds;
}

function admitCustomRole(input, candidates, required, requiredIds, role) {
  const candidate = exactCandidate(candidates, role.provider, role.model);
  const eligible = candidate?.thinkingLevels.includes(role.thinking) === true;
  const explicitlyRequired = input.projectCustomRoles === true || requiredIds.includes(role.role);
  if (!explicitlyRequired) return eligible;
  if (!eligible) throw new Error("dynamic_planning_custom_role_model_unavailable");
  required.add(role.role);
  return true;
}

function eligibleCustomRoles(input, topology, candidates, required) {
  const requiredIds = requiredCustomRoleIds(input, topology);
  if (input.projectCustomRoles === false) return [];
  return topology.customRoles.filter(
    (role) => admitCustomRole(input, candidates, required, requiredIds, role),
  );
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
    context_capsule: plannerContextCapsule(input),
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
    candidate_models: candidates.map((candidate) => publicPlannerCandidate(candidate)),
    locked_role_constraints: lockedRoleConstraints(input, policy, topology),
    worker_thinking_levels: THINKING_ORDER,
  };
}

function typesafeRoleAxes(role, candidates, input, policy, topology) {
  const locked = roleConstraint(input, role, policy, topology);
  const eligible = candidates.flatMap((candidate, index) => candidate.thinkingLevels.map((thinking) => ({
    provider: candidate.provider,
    model: candidate.modelId,
    thinking,
    modelIndex: index,
  }))).filter((tuple) => (
    (locked.provider === undefined || tuple.provider === locked.provider && tuple.model === locked.model)
    && (locked.thinking === undefined || tuple.thinking === locked.thinking)
  ));
  if (!eligible.length) throw new Error("typesafe_assignment_options_unavailable");
  const candidateIndexes = [...new Set(eligible.map((tuple) => tuple.modelIndex))];
  const thinkingLevels = THINKING_ORDER.filter((level) => eligible.some((tuple) => tuple.thinking === level));
  return { eligible, candidateIndexes, thinkingLevels };
}

function typesafeQuestionState(payload) {
  const { candidate_models: candidateModels, worker_thinking_levels: _levels, ...state } = payload;
  return {
    ...state,
    worker_model_candidate_count: candidateModels.length,
    candidate_model_capabilities: candidateModels,
  };
}

function typesafeDecisionRequest(payload, candidates, input, policy, topology) {
  const questions = {};
  const inclusions = new Map();
  const assignments = new Map();
  const fixedAssignments = new Map();
  for (const [index, role] of policy.roles.entries()) {
    const descriptor = payload.eligible_roles.find((item) => item.role === role);
    if (!descriptor) throw new Error("typesafe_role_descriptor_missing");
    if (!policy.required.has(role)) {
      const questionId = `include_${String(index).padStart(2, "0")}`;
      questions[questionId] = {
        type: "choice",
        instructions: {
          decision: "Choose include only when this specialist materially improves the task; otherwise choose omit. Keep the worker roster as small as useful.",
          role,
          contract: descriptor.contract,
          authority: descriptor.authority,
        },
        criteria: {
          include: "Include this exact read-only specialist.",
          omit: "Omit this specialist from the run.",
        },
      };
      inclusions.set(questionId, role);
    }
    const axes = typesafeRoleAxes(role, candidates, input, policy, topology);
    const modelQuestion = axes.candidateIndexes.length > 1 ? `model_${String(index).padStart(2, "0")}` : undefined;
    const thinkingQuestion = axes.thinkingLevels.length > 1 ? `thinking_${String(index).padStart(2, "0")}` : undefined;
    if (modelQuestion) {
      questions[modelQuestion] = {
        type: "choice",
        instructions: {
          decision: "Choose the smallest sufficient exact eligible provider/model identity by its catalog index. candidate_model_capabilities is the authoritative exact Pi model scope; choose only an index in this question and never invent an identity. Capability metadata and declared catalog cost hints are at that index. Missing, zero, or unavailable metadata is unknown; never guess. Declared rates are catalog hints, not billing or observed spend. Do not infer quality, coding skill, latency, or reliability from model names; honor role locks.",
          role, contract: descriptor.contract, authority: descriptor.authority,
        },
        criteria: Object.fromEntries(axes.candidateIndexes.map((candidateIndex) => [
          `m${candidateIndex.toString(36)}`, `catalog index ${candidateIndex}`,
        ])),
      };
    }
    if (thinkingQuestion) {
      questions[thinkingQuestion] = {
        type: "choice",
        instructions: {
          decision: "Choose one exact model-supported worker thinking level from this role's eligible levels; the selected model/level pair must be an eligible catalog tuple.",
          role, contract: descriptor.contract, authority: descriptor.authority,
        },
        criteria: Object.fromEntries(axes.thinkingLevels.map((level) => [level, level])),
      };
    }
    if (!modelQuestion && !thinkingQuestion) fixedAssignments.set(role, axes.eligible[0]);
    else assignments.set(role, { axes, modelQuestion, thinkingQuestion });
  }
  let lockedConfirmation;
  if (Object.keys(questions).length > TYPESAFE_MAX_QUESTIONS) throw new Error("typesafe_question_limit_exceeded");
  if (!Object.keys(questions).length) {
    lockedConfirmation = "locked_plan";
    questions[lockedConfirmation] = {
      type: "choice",
      instructions: "Decide whether the fully locked mandatory worker topology is suitable for the supplied task and constraints.",
      criteria: {
        accept: "The locked plan is suitable.",
        reject: "The locked plan is unsafe or materially unsuitable.",
      },
    };
  }
  return {
    request: {
      state: typesafeQuestionState(payload),
      model: TYPESAFE_MODEL,
      questions,
    },
    questions,
    inclusions,
    assignments,
    fixedAssignments,
    lockedConfirmation,
  };
}

function typesafeChoiceAnswer(value, options) {
  if (!exactFields(value, ["type", "choice", "confidence", "probabilities"])
      || value.type !== "choice" || typeof value.choice !== "string"
      || !options.has(value.choice)
      || typeof value.confidence !== "number" || !Number.isFinite(value.confidence)
      || value.confidence < 0 || value.confidence > 1
      || !value.probabilities || typeof value.probabilities !== "object"
      || Array.isArray(value.probabilities)) {
    throw new Error("typesafe_answer_invalid");
  }
  const probabilityKeys = Object.keys(value.probabilities);
  if (probabilityKeys.length !== options.size
      || probabilityKeys.some((choice) => !options.has(choice))
      || probabilityKeys.some((choice) => {
        const probability = value.probabilities[choice];
        return typeof probability !== "number" || !Number.isFinite(probability)
          || probability < 0 || probability > 1;
      })) {
    throw new Error("typesafe_answer_invalid");
  }
  return { choice: value.choice, confidence: value.confidence };
}

function validatedTypeSafeModel(value) {
  if (!boundedIdentifier(value) || !TYPESAFE_MODEL_PATTERN.test(value)) {
    throw new Error("typesafe_response_invalid");
  }
  return value;
}

function validatedTypeSafeAnswers(value, expectedQuestions) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("typesafe_response_invalid");
  }
  const expected = Object.keys(expectedQuestions).sort();
  const actual = Object.keys(value).sort();
  if (expected.length !== actual.length
      || expected.some((question, index) => question !== actual[index])) {
    throw new Error("typesafe_answers_incomplete");
  }
  return value;
}

function validatedTypeSafeUsage(value) {
  if (!exactFields(value, ["input_tokens", "output_tokens"])) {
    throw new Error("typesafe_response_invalid");
  }
  const input = value.input_tokens;
  const output = value.output_tokens;
  if (!Number.isSafeInteger(input) || input < 0 || !Number.isSafeInteger(output) || output < 0
      || !Number.isSafeInteger(input + output)) {
    throw new Error("typesafe_usage_invalid");
  }
  return {
    input,
    output,
    cacheRead: 0,
    cacheWrite: 0,
    totalTokens: input + output,
    cost: { total: null },
  };
}

function typesafeResponse(value, expectedQuestions) {
  if (!exactFields(value, ["model", "answers", "usage"])) {
    throw new Error("typesafe_response_invalid");
  }
  return {
    model: validatedTypeSafeModel(value.model),
    answers: validatedTypeSafeAnswers(value.answers, expectedQuestions),
    usage: validatedTypeSafeUsage(value.usage),
  };
}

function typesafeReason(required, inclusionConfidence, assignmentConfidence) {
  const signals = [];
  if (inclusionConfidence !== undefined) {
    signals.push(`roster confidence=${inclusionConfidence.toFixed(2)}`);
  }
  if (assignmentConfidence !== undefined) {
    signals.push(`assignment confidence=${assignmentConfidence.toFixed(2)}`);
  }
  const selection = required ? "retained the required role" : "included this specialist";
  return `Jev ${selection}${signals.length ? ` (${signals.join(", ")})` : " with a locked assignment"}.`;
}

function parseTypeSafeDecision(responseValue, requestValue, candidates, input, policy, topology) {
  const response = typesafeResponse(responseValue, requestValue.questions);
  const inclusionByRole = new Map();
  for (const [questionId, role] of requestValue.inclusions) {
    const answer = typesafeChoiceAnswer(
      response.answers[questionId],
      new Set(["include", "omit"]),
    );
    inclusionByRole.set(role, answer);
  }
  const assignmentByRole = new Map(requestValue.fixedAssignments);
  const assignmentConfidence = new Map();
  for (const [role, assignment] of requestValue.assignments) {
    let selectedModelIndex = assignment.axes.candidateIndexes[0];
    let selectedThinking = assignment.axes.thinkingLevels[0];
    let confidence = 1;
    if (assignment.modelQuestion) {
      const options = new Set(Object.keys(requestValue.questions[assignment.modelQuestion].criteria));
      const answer = typesafeChoiceAnswer(response.answers[assignment.modelQuestion], options);
      selectedModelIndex = Number.parseInt(answer.choice.slice(1), 36);
      confidence = answer.confidence;
    }
    if (assignment.thinkingQuestion) {
      const options = new Set(Object.keys(requestValue.questions[assignment.thinkingQuestion].criteria));
      const answer = typesafeChoiceAnswer(response.answers[assignment.thinkingQuestion], options);
      selectedThinking = answer.choice;
      confidence = Math.min(confidence, answer.confidence);
    }
    const tuple = assignment.axes.eligible.find((item) => (
      item.modelIndex === selectedModelIndex && item.thinking === selectedThinking
    ));
    if (!tuple) throw new Error("typesafe_assignment_tuple_invalid");
    assignmentByRole.set(role, tuple);
    assignmentConfidence.set(role, confidence);
  }
  if (requestValue.lockedConfirmation) {
    const answer = typesafeChoiceAnswer(
      response.answers[requestValue.lockedConfirmation],
      new Set(["accept", "reject"]),
    );
    if (answer.choice !== "accept") throw new Error("typesafe_locked_plan_rejected");
  }
  const roles = [];
  for (const role of policy.roles) {
    const inclusion = inclusionByRole.get(role);
    if (!policy.required.has(role) && inclusion?.choice !== "include") continue;
    const assignment = assignmentByRole.get(role);
    if (!assignment) throw new Error("typesafe_assignment_missing");
    roles.push({
      role,
      provider: assignment.provider,
      model: assignment.model,
      thinking: assignment.thinking,
      reason: typesafeReason(
        policy.required.has(role),
        inclusion?.confidence,
        assignmentConfidence.get(role),
      ),
    });
  }
  const decision = validatePlannerDecision(
    { version: 1, roles },
    candidates,
    input,
    policy,
    topology,
  );
  return { decision, model: response.model, usage: response.usage };
}

async function completeTypeSafeDecision(
  payload,
  candidates,
  input,
  policy,
  topology,
  signal,
  adapter,
) {
  const requestValue = typesafeDecisionRequest(payload, candidates, input, policy, topology);
  const response = await requestTypeSafe(requestValue.request, {
    apiKey: adapter.typesafeApiKey,
    fetchImpl: adapter.typesafeFetch,
    signal,
  });
  return parseTypeSafeDecision(
    response,
    requestValue,
    candidates,
    input,
    policy,
    topology,
  );
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

function plannerContextCapsule(input) {
  if (input.renderedContextCapsule !== undefined) return input.renderedContextCapsule;
  return input.contextCapsule ?? null;
}

function planningOverrideSummary(input) {
  const roleValues = Object.entries(OPTIONAL_FIELDS)
    .filter(([, field]) => typeof input[field] === "boolean")
    .map(([role, field]) => `${role}=${input[field] ? "required" : "disabled"}`);
  const projectCustom = new Map([
    [true, ["project-custom=required"]],
    [false, ["project-custom=disabled"]],
  ]).get(input.projectCustomRoles) || [];
  const modelRoles = Object.keys(input.modelOverrides || {}).sort();
  const models = modelRoles.length ? [`model-overrides=${modelRoles.join(",")}`] : [];
  return [...roleValues, ...projectCustom, ...models];
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

function resolvedPlannerBindings(bindingDigests, topology) {
  if (bindingDigests) return bindingDigests;
  return {
    plannerPolicy: metadataDigest({ version: 1 }),
    topologyPolicy: metadataDigest(topology),
  };
}

async function completePlannerDecision(ctx, selection, serialized, requestId, signal) {
  return ctx.modelRegistry.complete(
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
      sessionId: requestId,
      signal,
    },
  );
}

export async function runPreflightPlanner(
  ctx,
  input,
  project,
  selection,
  topologyValue,
  bindingDigests,
  signal,
  adapter = {},
) {
  const topology = topologyValue;
  const candidates = selection.candidates;
  const resolvedBindings = resolvedPlannerBindings(bindingDigests, topology);
  if (!Array.isArray(candidates) || !candidates.length) {
    throw new Error("decision_model_candidates_unavailable");
  }
  const policy = eligiblePlannerRoles(input, topology, candidates);
  validatePlannerInputConstraints(input, candidates, policy, topology);
  const payload = plannerPayload(input, project, candidates, policy, topology);
  const serialized = JSON.stringify(payload);
  if (utf8Bytes(serialized) > MAX_PROMPT_BYTES) throw new Error("planner_input_too_large");
  const requestId = randomUUID().replaceAll("-", "");
  const createdAtMs = Date.now();
  let decision;
  let usage;
  let decisionModel = selection.modelId;
  if (selection.kind === "typesafe") {
    const completed = await completeTypeSafeDecision(
      payload,
      candidates,
      input,
      policy,
      topology,
      signal,
      adapter,
    );
    decision = completed.decision;
    usage = completed.usage;
    decisionModel = completed.model;
  } else {
    if (typeof ctx?.modelRegistry?.complete !== "function") {
      throw new Error("decision_model_completion_unavailable");
    }
    const response = await completePlannerDecision(ctx, selection, serialized, requestId, signal);
    decision = parsePlannerResponse(response, candidates, input, policy, topology);
    usage = response.usage;
  }
  const acceptedAtMs = Date.now();
  return {
    input: plannedStartInput(input, decision),
    plan: {
      version: decision.version,
      decisionModel: {
        provider: selection.provider,
        model: decisionModel,
        thinking: selection.thinking,
        source: selection.source,
      },
      roles: decision.roles,
      usage,
      requestId,
      createdAtMs,
      acceptedAtMs,
      bindings: {
        plannerPolicy: resolvedBindings.plannerPolicy,
        topologyPolicy: resolvedBindings.topologyPolicy,
        candidateSet: plannerCandidateDigest(candidates),
      },
      operatorOverrides: planningOverrideSummary(input),
    },
  };
}

export function plannerPlanConfirmation(plan) {
  if (!plan) return "Static/manual policy (no preflight model call)";
  const roles = plan.roles.map((role) => {
    const contract = role.specialistContract ? ` contract=${role.specialistContract}` : "";
    return `${role.role}:${contract} ${role.provider}/${role.model} thinking=${role.thinking} — ${role.reason}`;
  });
  const decision = plan.decisionModel.provider === TYPESAFE_PROVIDER
    ? `Decision model: TypeSafe/${plan.decisionModel.model} source=${plan.decisionModel.source}`
    : `Decision model: ${plan.decisionModel.provider}/${plan.decisionModel.model} thinking=${plan.decisionModel.thinking} source=${plan.decisionModel.source}`;
  return [
    decision,
    `Operator planning constraints: ${plan.operatorOverrides.length ? plan.operatorOverrides.join("; ") : "none"}`,
    `Selected ${plan.roles.length} workers:`,
    ...roles,
  ].join("\n");
}

export const plannerTestHooks = {
  MAX_PROMPT_BYTES,
  MAX_RESPONSE_BYTES,
  MAX_PLANNER_MODELS,
  TYPESAFE_MAX_QUESTIONS,
  PLANNER_MAX_TOKENS,
  PLANNER_TIMEOUT_MS,
  ROLE_ORDER,
  SYSTEM_PROMPT,
  TYPESAFE_MAX_CHOICE_OPTIONS,
  TYPESAFE_PROVIDER,
  TYPESAFE_SOURCE,
  parseTypeSafeDecisionForTest: (response, request, candidates, ctx, selection, topology, policy, bindingOptions) => {
    const parsed = parseTypeSafeDecision(
      response, request, candidates, ctx, policy ?? selection.policy, topology,
    );
    if (bindingOptions) Object.assign(parsed, bindingOptions);
    return parsed;
  },
  typeSafeDecisionRequestForTest: (payload, candidates, input, policy, topology) => typesafeDecisionRequest(
    payload, candidates, input, eligiblePlannerRoles(input, topology, candidates), topology,
  ),
  plannerPayloadForTest: (input, project, candidates, policy, topology) => plannerPayload(
    input, project, candidates, eligiblePlannerRoles(input, topology, candidates), topology,
  ),
};
