// Accepted-plan metadata only. No prompts, response bodies, or inferred reasoning.
import { SUPPORT_CHOICES, SUPPORT_ERROR, supportIdentity, deriveProviderSupport, providerSupportLine } from "./orchestrator-provider-support.js";
import { metadataDigest, plannerEvidenceDigest, publicPlannerCandidate } from "./orchestrator-planning.js";

export const MAX_EVIDENCE_BYTES = 224 * 1024;
export const TOP_ALTERNATIVES = 3;
export const PROBABILITY_TOLERANCE = 1e-6;
const compare = (a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b));
const key = (item) => `${item.provider}\0${item.model}`;
function scalarIdentity(value) {
  return value.support ?? value.provider_composition ?? value.intent ?? value.decision;
}
function identityKey(value) {
  const scalar = scalarIdentity(value);
  if (scalar) return scalar;
  if (Object.hasOwn(value, "inclusion")) return `${value.role}\0${value.inclusion ? "include" : "omit"}`;
  if (value.model) {
    return value.thinking
      ? `${value.role}\0${value.provider}\0${value.model}\0${value.thinking}`
      : `${value.role}\0${value.provider}\0${value.model}`;
  }
  return `${value.role}\0${value.thinking}`;
}

function tupleChoiceKey(tuple) {
  return `m${tuple.modelIndex.toString(36)}_${tuple.thinking}`;
}
const sorted = (items) => [...items].sort((a, b) => compare(key(a), key(b)));

// Used only when the role was omitted and has no fixed assignment. The answered
// pair is reconstructed; it is not an assignment and is not truncated.
function answeredAssignmentTuple(assignment, answers) {
  if (assignment?.tupleQuestion) {
    return assignment.axes.eligible.find((item) => tupleChoiceKey(item) === answers[assignment.tupleQuestion].choice);
  }
  const modelIndex = assignment.modelQuestion
    ? Number.parseInt(answers[assignment.modelQuestion].choice.slice(1), 36)
    : assignment.axes.candidateIndexes[0];
  const thinking = assignment.thinkingQuestion
    ? answers[assignment.thinkingQuestion].choice
    : assignment.axes.thinkingLevels[0];
  return assignment.axes.eligible.find((item) => item.modelIndex === modelIndex && item.thinking === thinking);
}

function tupleEvidenceChoices(lock, assignment, catalog) {
  return assignment.axes.eligible.map((item) => {
    const projected = catalog.find((entry) => entry.provider === item.provider && entry.model === item.model);
    return [tupleChoiceKey(item), {
      role: lock.role, provider: item.provider, model: item.model, thinking: item.thinking,
      facts: plannerEvidenceDigest(projected),
    }];
  });
}

function addAssignmentEvidence(lock, assignment, tuple, models, candidates, catalog, answers, add) {
  if (assignment?.tupleQuestion) {
    add("tuple", lock.role, tupleEvidenceChoices(lock, assignment, catalog), answers[assignment.tupleQuestion].choice, assignment.tupleQuestion);
    return;
  }
  const modelChoices = models.map((item) => [
    `m${candidates.findIndex((candidate) => candidate.provider === item.provider && candidate.modelId === item.model).toString(36)}`,
    modelIdentity(lock.role, item),
  ]);
  const selectedModel = modelChoices.find(([, identity]) => identity.provider === tuple.provider && identity.model === tuple.model)[0];
  add("model", lock.role, modelChoices, selectedModel, assignment?.modelQuestion);
  const levels = ["off", "minimal", "low", "medium", "high", "xhigh", "max"].filter((level) =>
    (lock.thinking === null || lock.thinking === level) && models.some((item) => item.thinking_levels.includes(level)));
  add("thinking", lock.role, levels.map((level) => [level, thinkingIdentity(lock.role, level, models)]), tuple.thinking, assignment?.thinkingQuestion);
}

function eligibleModels(catalog, eligible, lock) {
  return catalog.filter((item) => eligible.some((identity) => key(identity) === key(item))
    && (lock.thinking === null || item.thinking_levels.includes(lock.thinking)));
}

function addLockedRoleEvidence(request, candidates, decision, selection, answers, catalog, eligibility, add) {
  const inclusionQuestions = new Map([...request.inclusions].map(([id, role]) => [role, id]));
  for (const lock of selection.locks) {
    const inclusion = inclusionQuestions.get(lock.role);
    const included = decision.roles.some((item) => item.role === lock.role);
    const inclusionChoices = inclusion ? [true, false] : [included];
    add(
      "roster",
      lock.role,
      inclusionChoices.map((value) => [value ? "include" : "omit", { role: lock.role, inclusion: value }]),
      included ? "include" : "omit",
      inclusion,
    );
    if (lock.inclusion === false) continue;
    const assignment = request.assignments.get(lock.role);
    const fixed = request.fixedAssignments.get(lock.role);
    const eligible = eligibility.find((item) => item.role === lock.role).identities;
    const models = eligibleModels(catalog, eligible, lock);
    const tuple = decision.roles.find((item) => item.role === lock.role) ?? fixed ?? answeredAssignmentTuple(assignment, answers);
    if (!tuple) throw new Error("invalid_planner_evidence");
    addAssignmentEvidence(lock, assignment, tuple, models, candidates, catalog, answers, add);
  }
}

function modelIdentity(role, candidate) {
  return { role, provider: candidate.provider, model: candidate.model, thinking: null, facts: plannerEvidenceDigest(candidate) };
}

function thinkingIdentity(role, thinking, candidates) {
  return {
    role, thinking,
    models: candidates.filter((item) => item.thinking_levels.includes(thinking)).map((item) => ({
      provider: item.provider, model: item.model, facts: plannerEvidenceDigest(item),
    })),
  };
}

function decisionEvidence(axis, role, choices, selected, answer, source) {
  const probabilistic = source === "typesafe_choice" && answer !== undefined;
  const options = choices.map(([label, identity]) => ({
    id: metadataDigest(identity), identity,
    probability: probabilistic ? answer.probabilities[label] : null,
  })).sort((a, b) => compare(identityKey(a.identity), identityKey(b.identity)));
  const selectedIdentity = choices.find(([label]) => label === selected)?.[1];
  if (!selectedIdentity) throw new Error("invalid_planner_evidence");
  const selectedId = metadataDigest(selectedIdentity);
  const alternatives = probabilistic ? options.filter((item) => item.id !== selectedId)
    .sort((a, b) => b.probability - a.probability || compare(identityKey(a.identity), identityKey(b.identity)))
    .slice(0, TOP_ALTERNATIVES).map((item) => item.id) : [];
  return {
    axis, role, authority: answer === undefined ? "fixed" : "planner",
    selected: selectedId,
    confidence: probabilistic ? answer.confidence : null,
    selected_probability: probabilistic ? options.find((item) => item.id === selectedId).probability : null,
    options, alternatives,
  };
}

function compositionRoleFacts(catalog, eligibility, lock) {
  const identities = new Set(eligibility.find((entry) => entry.role === lock.role).identities.map(key));
  const candidates = [];
  for (const item of catalog) {
    if (!identities.has(key(item))) continue;
    if (lock.provider !== null && (item.provider !== lock.provider || item.model !== lock.model)) continue;
    const levels = item.thinking_levels.filter((level) => lock.thinking === null || level === lock.thinking);
    if (!levels.length) continue;
    candidates.push({ provider: item.provider, model: item.model, thinking_levels: levels, facts: plannerEvidenceDigest(item) });
  }
  return { role: lock.role, inclusion: lock.inclusion, candidates };
}

// Bounded per-role catalog scan, never an assignment-product search. Optional
// roles may be omitted for single, but can make mixed possible.
export function providerCompositionFacts(catalog, eligibility, locks) {
  catalog = sorted(catalog);
  const roles = locks.filter((lock) => lock.inclusion !== false).map((lock) => compositionRoleFacts(catalog, eligibility, lock));
  if (roles.some((role) => !role.candidates.length)) throw new Error("provider_composition_unavailable");
  const providers = roles.map((role) => new Set(role.candidates.map((item) => item.provider)));
  const required = roles.flatMap((role, index) => role.inclusion === true ? [providers[index]] : []);
  if (required.length < 2) throw new Error("provider_composition_unavailable");
  const single = [...required[0]].some((provider) => required.every((items) => items.has(provider)));
  const mixed = new Set(providers.flatMap((items) => [...items])).size > 1;
  return { feasible: [...(single ? ["single_provider"] : []), ...(mixed ? ["mixed_provider"] : [])], roles };
}

export function validateProviderComposition(value, facts, roles) {
  const choices = facts.feasible.length > 1 ? [...facts.feasible, "no_material_preference"] : facts.feasible;
  const actual = new Set(roles.map((item) => item.provider)).size === 1 ? "single_provider" : "mixed_provider";
  if (!choices.includes(value) || value !== "no_material_preference" && value !== actual) {
    throw new Error(`provider_composition_inconsistent. ${SUPPORT_ERROR}`);
  }
  return value;
}

export function providerCompositionSummary(evidence) {
  const item = evidence?.decisions?.find((decision) => decision.axis === "provider_composition");
  if (!item) return { state: "unavailable" };
  const option = (id) => item.options.find((entry) => entry.id === id);
  return {
    state: item.authority === "fixed" ? "fixed" : "choice",
    selected: option(item.selected).identity.provider_composition,
    authority: item.authority, confidence: item.confidence, selected_probability: item.selected_probability,
    feasible: evidence.provider_composition.feasible,
    alternatives: item.alternatives.map((id) => ({ composition: option(id).identity.provider_composition, probability: option(id).probability })),
  };
}

export function buildPlannerEvidence(request, candidates, decision, selection, answers, source) {
  const catalog = sorted(candidates.map(publicPlannerCandidate));
  const eligibility = [...selection.workerScope.candidatesByRole].map(([role, identities]) => ({
    role,
    identities: catalog.filter((item) => identities.has(key(item))).map((item) => ({ provider: item.provider, model: item.model })),
  }));
  const decisions = [];
  const add = (axis, role, choices, selected, question) => decisions.push(decisionEvidence(
    axis, role, choices, selected, question ? answers[question] : undefined, source,
  ));
  addLockedRoleEvidence(request, candidates, decision, selection, answers, catalog, eligibility, add);
  add("task_intent", null, ["change", "investigation", "review", "advisory"].map((intent) => [intent, { intent }]), decision.task_intent, "task_intent");
  if (request.lockedConfirmation) add("composition", null, ["accept", "reject"].map((value) => [value, { decision: value }]), "accept", request.lockedConfirmation);
  const facts = request.compositionFacts;
  const choices = facts.feasible.length > 1 ? [...facts.feasible, "no_material_preference"] : facts.feasible;
  const references = request.supportQuestion ? SUPPORT_CHOICES : ["none"];
  add("provider_support", null, references.map((reference) => [reference, supportIdentity(reference, facts, catalog)]), decision.provider_support_reference, request.supportQuestion);
  add("provider_composition", null, choices.map((value) => [value, { provider_composition: value, facts: plannerEvidenceDigest(facts) }]), decision.provider_composition, request.compositionQuestion);
  const joint = [...request.assignments.values()].some((item) => item.tupleQuestion);
  const evidence = {
    version: joint ? 4 : 3, source, catalog, eligibility, decisions,
    provider_support: deriveProviderSupport(decision.provider_support_reference, decision.provider_composition, facts, catalog, decision.roles),
    provider_composition: facts,
    provider_comparison: {
      state: new Set(decision.roles.map((item) => item.provider)).size === 1 ? "homogeneous" : "mixed",
      rationale: "rationale_unavailable",
    },
  };
  if (Buffer.byteLength(JSON.stringify(evidence), "utf8") > MAX_EVIDENCE_BYTES) throw new Error("planner_evidence_too_large");
  return evidence;
}

// Shortest round-trip JSON for a [0,1] number is at most 24 characters: fixed
// notation stops at 1e-6, and 17 significant digits there are 24 characters.
// Smaller magnitudes use shorter scientific notation. This is a measured width,
// not a flat evidence pad.
const LONG_PROBABILITY = 0.0000010010719171047012;
const HOMOGENEOUS_COMPARISON = { state: "homogeneous", rationale: "rationale_unavailable" };
const MIXED_COMPARISON = { state: "mixed", rationale: "rationale_unavailable" };
const LOCKED_SINGLE_SUPPORT = { source: "derived", state: "locked_fixed", feasible: ["single_provider"], reference: "none", roles: [] };
const LOCKED_MIXED_SUPPORT = { source: "derived", state: "locked_fixed", feasible: ["mixed_provider"], reference: "none", roles: [] };
const MIXED_SUPPORT = { source: "derived", state: "mixed", feasible: ["single_provider", "mixed_provider"], reference: "none", roles: [] };

function jsonBytes(value) {
  return Buffer.byteLength(JSON.stringify(value));
}

// Selected probability is retained twice. Give that duplicated slot the wider
// JSON number. The residual near 1 is shorter than LONG_PROBABILITY, so a
// reachable distribution puts it on a non-selected choice.
function maxWidthAnswer(source, choice, keys) {
  if (source !== "typesafe_choice") return { choice };
  if (keys.length <= 1) {
    return { choice, confidence: LONG_PROBABILITY, probabilities: { [choice]: 1 } };
  }
  const others = keys.filter((key) => key !== choice);
  const probabilities = {};
  let consumed = 0;
  for (const key of others) {
    probabilities[key] = LONG_PROBABILITY;
    consumed += LONG_PROBABILITY;
  }
  const residual = 1 - consumed;
  if (JSON.stringify(residual).length > JSON.stringify(LONG_PROBABILITY).length) {
    probabilities[choice] = residual;
  } else {
    probabilities[others[0]] = residual;
    probabilities[choice] = LONG_PROBABILITY;
  }
  return { choice, confidence: LONG_PROBABILITY, probabilities };
}

function supportedBytes(reference, sliceJsons) {
  const prefix = `{"source":"derived","state":"supported","feasible":["single_provider","mixed_provider"],"reference":${JSON.stringify(reference)},"roles":[`;
  let bytes = Buffer.byteLength(prefix) + 2;
  for (let index = 0; index < sliceJsons.length; index += 1) {
    if (index) bytes += 1;
    bytes += Buffer.byteLength(sliceJsons[index]);
  }
  return bytes;
}

function supportedSlice(role, provider, model, thinking, reference, catalog) {
  try {
    const support = deriveProviderSupport(reference, "single_provider", { roles: [role] }, catalog, [{
      role: role.role, provider, model, thinking,
    }]);
    if (support.state !== "supported" || support.roles.length !== 1) return undefined;
    return JSON.stringify(support.roles[0]);
  } catch {
    return undefined;
  }
}

// Widest successful model, not the longest name and not only the first provider.
// Advantage is per role, so this is not an assignment-product search.
function passingSlice(role, provider, reference, catalog) {
  if (!role.candidates.some((item) => item.provider !== provider)) return undefined;
  const pairs = [];
  for (const candidate of role.candidates) {
    if (candidate.provider !== provider || !candidate.thinking_levels.length) continue;
    const levels = reference === "thinking"
      ? candidate.thinking_levels
      : [candidate.thinking_levels.reduce((left, right) => (
        Buffer.byteLength(JSON.stringify(left)) >= Buffer.byteLength(JSON.stringify(right)) ? left : right
      ))];
    for (const thinking of levels) {
      pairs.push({
        model: candidate.model,
        thinking,
        width: Buffer.byteLength(JSON.stringify(candidate.model)) + Buffer.byteLength(JSON.stringify(thinking)),
      });
    }
  }
  pairs.sort((left, right) => right.width - left.width || compare(left.model, right.model));
  for (const pair of pairs) {
    const slice = supportedSlice(role, provider, pair.model, pair.thinking, reference, catalog);
    if (slice) return slice;
  }
  return undefined;
}

function supportReferences() {
  return SUPPORT_CHOICES.filter((reference) => reference !== "none");
}

function referenceSupportSlices(role, provider, catalog) {
  const byReference = new Map();
  for (const reference of supportReferences()) {
    const slice = passingSlice(role, provider, reference, catalog);
    if (slice) byReference.set(reference, slice);
  }
  return byReference;
}

function providerSupportSlices(role, catalog) {
  const byProvider = new Map();
  for (const provider of new Set(role.candidates.map((item) => item.provider))) {
    const byReference = referenceSupportSlices(role, provider, catalog);
    if (byReference.size) byProvider.set(provider, byReference);
  }
  return byProvider;
}

function supportSliceIndex(facts, catalog) {
  const slices = new Map();
  for (const role of facts.roles) slices.set(role.role, providerSupportSlices(role, catalog));
  return slices;
}

// Strict greater preserves the first equal-width candidate. Callers must keep
// the original mask, provider, and reference order.
function rememberWidestSupport(best, supportBytes, comparison) {
  const total = supportBytes + jsonBytes(comparison);
  if (total > best.total) {
    best.total = total;
    best.supportBytes = supportBytes;
    best.comparison = comparison;
  }
}

function commonProviders(selected) {
  const providerSets = selected.map((role) => new Set(role.candidates.map((item) => item.provider)));
  return [...providerSets[0]].filter((provider) => providerSets.every((items) => items.has(provider)));
}

function providersAreDiverse(selected) {
  return new Set(selected.flatMap((role) => role.candidates.map((item) => item.provider))).size > 1;
}

function homogeneousSliceBytes(selected, slices, provider, reference) {
  const roleSlices = [];
  for (const role of selected) {
    const slice = slices.get(role.role)?.get(provider)?.get(reference);
    if (!slice) return undefined;
    roleSlices.push(slice);
  }
  return supportedBytes(reference, roleSlices);
}

function considerHomogeneousSupport(selected, slices, common, best) {
  for (const provider of common) {
    for (const reference of supportReferences()) {
      const bytes = homogeneousSliceBytes(selected, slices, provider, reference);
      if (bytes !== undefined) rememberWidestSupport(best, bytes, HOMOGENEOUS_COMPARISON);
    }
  }
}

function considerLockedCompositions(common, diverse, best) {
  if (common.length && !diverse) rememberWidestSupport(best, jsonBytes(LOCKED_SINGLE_SUPPORT), HOMOGENEOUS_COMPARISON);
  if (!common.length && diverse) rememberWidestSupport(best, jsonBytes(LOCKED_MIXED_SUPPORT), MIXED_COMPARISON);
  return Boolean(common.length && diverse);
}

function considerRosterSupport(selected, slices, best) {
  if (selected.length < 2) return;
  const common = commonProviders(selected);
  const diverse = providersAreDiverse(selected);
  if (!considerLockedCompositions(common, diverse, best)) return;
  rememberWidestSupport(best, jsonBytes(MIXED_SUPPORT), MIXED_COMPARISON);
  considerHomogeneousSupport(selected, slices, common, best);
}

// Largest reachable derived support object. Optional omission is searched
// because an included role can make support unreachable. Required roles stay
// included. Providers and references are enumerated; model selection is the
// widest successful candidate per role, not an assignment product. More than
// 12 optional roles fails closed before HTTP. Equal totals keep the earlier candidate.
function maximumProviderSupport(facts, catalog) {
  const required = facts.roles.filter((role) => role.inclusion === true);
  const optional = facts.roles.filter((role) => role.inclusion !== true);
  if (optional.length > 12) return { supportBytes: MAX_EVIDENCE_BYTES + 1, comparison: HOMOGENEOUS_COMPARISON };
  const slices = supportSliceIndex(facts, catalog);
  const best = { supportBytes: 0, comparison: null, total: -1 };
  for (let mask = 0; mask < (1 << optional.length); mask += 1) {
    const selected = [...required, ...optional.filter((_, index) => (mask & (1 << index)) !== 0)];
    considerRosterSupport(selected, slices, best);
  }
  return { supportBytes: best.supportBytes, comparison: best.comparison };
}

function probeRoster(request) {
  const entries = [];
  for (const [role, assignment] of request.assignments) {
    entries.push({ role, assignment, tuple: assignment.axes.eligible[0] });
  }
  for (const [role, tuple] of request.fixedAssignments) entries.push({ role, tuple });
  if (new Set(entries.map((entry) => entry.tuple.provider)).size < 2) {
    const alternate = entries.find((entry) => (
      (entry.assignment?.axes.eligible ?? [entry.tuple]).some((tuple) => tuple.provider !== entry.tuple.provider)
    ));
    if (alternate) {
      const pool = alternate.assignment?.axes.eligible ?? [alternate.tuple];
      const anchor = entries[0].tuple.provider;
      alternate.tuple = pool.find((tuple) => tuple.provider !== anchor) ?? alternate.tuple;
    }
  }
  return entries;
}

function probeAssignmentAnswers(answers, entry, source) {
  const assignment = entry.assignment;
  if (!assignment) return;
  if (assignment.tupleQuestion) {
    const keys = assignment.axes.eligible.map((tuple) => tupleChoiceKey(tuple));
    answers[assignment.tupleQuestion] = maxWidthAnswer(source, tupleChoiceKey(entry.tuple), keys);
    return;
  }
  if (assignment.modelQuestion) {
    const keys = assignment.axes.candidateIndexes.map((index) => `m${index.toString(36)}`);
    answers[assignment.modelQuestion] = maxWidthAnswer(source, `m${entry.tuple.modelIndex.toString(36)}`, keys);
  }
  if (assignment.thinkingQuestion) {
    answers[assignment.thinkingQuestion] = maxWidthAnswer(source, entry.tuple.thinking, assignment.axes.thinkingLevels);
  }
}

function probeAnswers(request, entries, source) {
  const answers = {};
  for (const [questionId] of request.inclusions) {
    answers[questionId] = maxWidthAnswer(source, "include", ["include", "omit"]);
  }
  for (const entry of entries) probeAssignmentAnswers(answers, entry, source);
  const providers = new Set(entries.map((entry) => entry.tuple.provider));
  const composition = providers.size === 1 ? "single_provider" : "mixed_provider";
  const compositionKeys = request.compositionFacts.feasible.length > 1
    ? [...request.compositionFacts.feasible, "no_material_preference"]
    : request.compositionFacts.feasible;
  if (request.compositionQuestion) {
    answers[request.compositionQuestion] = maxWidthAnswer(source, composition, compositionKeys);
  }
  if (request.supportQuestion) {
    answers[request.supportQuestion] = maxWidthAnswer(source, "none", SUPPORT_CHOICES);
  }
  answers.task_intent = maxWidthAnswer(source, "change", ["change", "investigation", "review", "advisory"]);
  if (request.lockedConfirmation) {
    answers[request.lockedConfirmation] = maxWidthAnswer(source, "accept", ["accept", "reject"]);
  }
  return {
    answers,
    decision: {
      roles: entries.map((entry) => ({
        role: entry.role,
        provider: entry.tuple.provider,
        model: entry.tuple.model,
        thinking: entry.tuple.thinking,
      })),
      task_intent: "change",
      provider_composition: composition,
      provider_support_reference: "none",
    },
  };
}

// Measured upper bound of reachable retained evidence. Probability width puts
// the widest [0,1] JSON number on the duplicated selected slot. Support uses the
// largest successful object across common providers, references, and optional
// omissions. No flat pad and no tuple omission.
export function tupleEvidenceBoundBytes(request, candidates, selection, source = "typesafe_choice") {
  const entries = probeRoster(request);
  const probed = probeAnswers(request, entries, source);
  let evidence;
  try {
    evidence = buildPlannerEvidence(request, candidates, probed.decision, selection, probed.answers, source);
  } catch (error) {
    if (error.message === "planner_evidence_too_large") return MAX_EVIDENCE_BYTES + 1;
    throw error;
  }
  const catalog = sorted(candidates.map(publicPlannerCandidate));
  const maximum = maximumProviderSupport(request.compositionFacts, catalog);
  let bytes = jsonBytes(evidence);
  if (maximum.comparison) {
    const supportDelta = maximum.supportBytes - jsonBytes(evidence.provider_support);
    const comparisonDelta = jsonBytes(maximum.comparison) - jsonBytes(evidence.provider_comparison);
    if (supportDelta + comparisonDelta > 0) bytes += supportDelta + comparisonDelta;
  }
  return bytes;
}

function optionLabel(item) {
  const value = item.identity;
  const scalar = scalarIdentity(value);
  if (scalar) return scalar;
  if (Object.hasOwn(value, "inclusion")) return value.inclusion ? "include" : "omit";
  if (value.model) {
    return value.thinking ? `${value.provider}/${value.model} thinking=${value.thinking}` : `${value.provider}/${value.model}`;
  }
  return `${value.thinking} [${value.models.map((model) => `${model.provider}/${model.model}`).join(",")}]`;
}

export function plannerEvidenceLines(evidence) {
  if (!evidence || evidence.status === "unavailable" || ![1, 2, 3, 4].includes(evidence.version)) return ["Planner evidence: unavailable (static or legacy record).", "Provider composition decision: unavailable."];
  const composition = evidence.projection === "summary" ? evidence.provider_composition : providerCompositionSummary(evidence);
  const axes = evidence.version === 4
    ? "tuple axes are joint over exact eligible pairs, not synthesized conditional probability or reasoning."
    : "independent axes, not joint confidence or reasoning.";
  const lines = [`Planner evidence v${evidence.version}: ${evidence.source}; ${axes}`,
    `Provider comparison: ${evidence.provider_comparison.state}; rationale_unavailable (not Jev reasoning).`,
    composition?.selected ? `Provider composition decision: ${composition.selected}; ${composition.state} authority=${composition.authority}; ${composition.confidence === null ? "probabilities unavailable" : `confidence=${composition.confidence}; probability=${composition.selected_probability}`}.` : "Provider composition decision: unavailable (legacy record)."];
  lines.push(providerSupportLine(evidence));
  if (evidence.projection === "summary") return [...lines, "Summary projection; exact status/snapshot provides axis alternatives and facts."];
  for (const item of evidence.decisions) {
    const selected = item.options.find((option) => option.id === item.selected);
    const confidence = item.confidence === null ? "probabilities unavailable" : `confidence=${item.confidence}; probability=${item.selected_probability}`;
    const alternatives = item.alternatives.map((id) => item.options.find((option) => option.id === id))
      .map((option) => `${optionLabel(option)}=${option.probability}`).join("; ");
    lines.push(`${item.role ?? "plan"}/${item.axis}: ${item.authority} ${optionLabel(selected)}; ${confidence}${alternatives ? `; alternatives: ${alternatives}` : ""}`);
  }
  return lines;
}

// JSON.parse alone silently accepts duplicate fields. Bound nesting and reject them
// before a choice or accepted-record projection can discard ambiguous metadata.
function acceptJsonKey(token, next, fields) {
  if (!token.startsWith('"') || next !== ":") return;
  const field = JSON.parse(token);
  if (!fields || fields.has(field)) throw new Error("invalid_planner_json");
  fields.add(field);
}

export function strictPlannerJson(text) {
  const value = JSON.parse(text);
  const tokens = text.match(/"(?:\\.|[^"\\])*"|[{}\[\]:,]|[^\s{}\[\]:,]+/g) ?? [];
  const stack = [];
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index];
    if (token === "{" || token === "[") {
      stack.push(token === "{" ? new Set() : null);
      if (stack.length > 64) throw new Error("invalid_planner_json");
    } else if (token === "}" || token === "]") stack.pop();
    else acceptJsonKey(token, tokens[index + 1], stack.at(-1));
  }
  return value;
}
