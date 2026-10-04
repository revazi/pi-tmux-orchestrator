// Accepted-plan metadata only. No prompts, response bodies, or inferred reasoning.
import { metadataDigest, plannerEvidenceDigest, publicPlannerCandidate } from "./orchestrator-planning.js";

export const MAX_EVIDENCE_BYTES = 224 * 1024;
export const TOP_ALTERNATIVES = 3;
export const PROBABILITY_TOLERANCE = 1e-6;
const compare = (a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b));
const key = (item) => `${item.provider}\0${item.model}`;
function identityKey(value) {
  if (value.intent) return value.intent;
  if (value.decision) return value.decision;
  if (Object.hasOwn(value, "inclusion")) return `${value.role}\0${value.inclusion ? "include" : "omit"}`;
  if (value.model) return `${value.role}\0${value.provider}\0${value.model}`;
  return `${value.role}\0${value.thinking}`;
}
const sorted = (items) => [...items].sort((a, b) => compare(key(a), key(b)));

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

export function buildPlannerEvidence(request, candidates, decision, selection, answers, source) {
  const catalog = sorted(candidates.map(publicPlannerCandidate));
  const eligibility = [...selection.workerScope.candidatesByRole].map(([role, identities]) => ({
    role,
    identities: catalog.filter((item) => identities.has(key(item))).map((item) => ({ provider: item.provider, model: item.model })),
  }));
  const decisions = [];
  const inclusionQuestions = new Map([...request.inclusions].map(([id, role]) => [role, id]));
  const add = (axis, role, choices, selected, question) => decisions.push(decisionEvidence(
    axis, role, choices, selected, question ? answers[question] : undefined, source,
  ));
  for (const lock of selection.locks) {
    const inclusion = inclusionQuestions.get(lock.role);
    const included = decision.roles.some((item) => item.role === lock.role);
    const inclusionChoices = inclusion ? [true, false] : [included];
    add("roster", lock.role, inclusionChoices.map((value) => [value ? "include" : "omit", { role: lock.role, inclusion: value }]), included ? "include" : "omit", inclusion);
    if (lock.inclusion === false) continue;
    const assignment = request.assignments.get(lock.role);
    const fixed = request.fixedAssignments.get(lock.role);
    const eligible = eligibility.find((item) => item.role === lock.role).identities;
    const models = catalog.filter((item) => eligible.some((identity) => key(identity) === key(item))
      && (lock.thinking === null || item.thinking_levels.includes(lock.thinking)));
    const tuple = decision.roles.find((item) => item.role === lock.role) ?? fixed ?? (() => {
      const modelIndex = assignment.modelQuestion ? Number.parseInt(answers[assignment.modelQuestion].choice.slice(1), 36) : assignment.axes.candidateIndexes[0];
      const thinking = assignment.thinkingQuestion ? answers[assignment.thinkingQuestion].choice : assignment.axes.thinkingLevels[0];
      return assignment.axes.eligible.find((item) => item.modelIndex === modelIndex && item.thinking === thinking);
    })();
    if (!tuple) throw new Error("invalid_planner_evidence");
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
  add("task_intent", null, ["change", "investigation", "review", "advisory"].map((intent) => [intent, { intent }]), decision.task_intent, "task_intent");
  if (request.lockedConfirmation) add("composition", null, ["accept", "reject"].map((value) => [value, { decision: value }]), "accept", request.lockedConfirmation);
  const evidence = {
    version: 1, source, catalog, eligibility, decisions,
    provider_comparison: {
      state: new Set(decision.roles.map((item) => item.provider)).size === 1 ? "homogeneous" : "mixed",
      rationale: "rationale_unavailable",
    },
  };
  if (Buffer.byteLength(JSON.stringify(evidence), "utf8") > MAX_EVIDENCE_BYTES) throw new Error("planner_evidence_too_large");
  return evidence;
}

function optionLabel(item) {
  const value = item.identity;
  if (value.intent) return value.intent;
  if (value.decision) return value.decision;
  if (Object.hasOwn(value, "inclusion")) return value.inclusion ? "include" : "omit";
  if (value.model) return `${value.provider}/${value.model}`;
  return `${value.thinking} [${value.models.map((model) => `${model.provider}/${model.model}`).join(",")}]`;
}

export function plannerEvidenceLines(evidence) {
  if (!evidence || evidence.status === "unavailable" || evidence.version !== 1) return ["Planner evidence: unavailable (static or legacy record)."];
  const lines = [`Planner evidence v1: ${evidence.source}; independent axes, not joint confidence or reasoning.`,
    `Provider comparison: ${evidence.provider_comparison.state}; rationale_unavailable (not Jev reasoning).`];
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
