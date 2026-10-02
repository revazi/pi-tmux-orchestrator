import { metadataDigest, plannerCandidateDigest } from "./orchestrator-planning.js";

const BUILTINS = ["implementer", "reviewer", "probe", "playwright", "django"];
const key = (item) => `${item.provider}\0${item.modelId ?? item.model}`;
const identifier = (value) => typeof value === "string" && value.length > 0
  && value.length <= 256 && !/[\s\u0000-\u001f\u007f]/.test(value);
const object = (value) => value && typeof value === "object" && !Array.isArray(value);

export function workerCandidatesFailure(error) {
  if (!(error instanceof Error) || error.message !== "approved_worker_pool_required") return undefined;
  return {
    code: "approved_worker_pool_required",
    message: "Dynamic planning requires approved worker models. Configure version 5 workerCandidates in the external tmux-orchestrator.json, or supply exact provider/model overrides for every planner-eligible role (including optional roles).",
  };
}

export function validateWorkerCandidates(value) {
  if (value === null) return null;
  if (!object(value) || value.version !== 1 || !Object.hasOwn(value, "all")
      || Object.keys(value).some((field) => !["version", "all", "roles"].includes(field))) {
    throw new Error("invalid_worker_candidates_policy");
  }
  const pool = (items) => {
    if (!Array.isArray(items) || items.length < 1 || items.length > 32
        || items.some((item) => !object(item) || Object.keys(item).length !== 2
          || !identifier(item.provider) || !identifier(item.model))) {
      throw new Error("invalid_worker_candidates_pool");
    }
    if (new Set(items.map(key)).size !== items.length) {
      throw new Error("duplicate_worker_candidates_identity");
    }
    return items.map((item) => ({ provider: item.provider, model: item.model }));
  };
  const roles = Object.hasOwn(value, "roles") ? value.roles : {};
  if (!object(roles) || Object.keys(roles).some((role) => !BUILTINS.includes(role))) {
    throw new Error("invalid_worker_candidates_roles");
  }
  const policy = {
    version: 1,
    all: pool(value.all),
    roles: Object.fromEntries(Object.entries(roles).map(([role, items]) => [role, pool(items)])),
  };
  if (new Set([policy.all, ...Object.values(policy.roles)].flat().map(key)).size > 100) {
    throw new Error("worker_candidates_limit_exceeded");
  }
  return policy;
}

export function resolveWorkerCandidates(catalog, configured, locks) {
  const policy = validateWorkerCandidates(configured);
  const available = new Map(catalog.map((candidate) => [key(candidate), candidate]));
  // Check every configured pool, even when a role lock overrides it.
  for (const identity of [policy?.all ?? [], ...Object.values(policy?.roles ?? {})].flat()) {
    if (!available.has(key(identity))) throw new Error("approved_worker_candidate_unavailable");
  }
  const candidatesByRole = new Map();
  const roles = [];
  for (const lock of locks) {
    const fixed = lock.provider !== undefined;
    if (!fixed && !policy) {
      throw new Error("approved_worker_pool_required");
    }
    const source = fixed ? (BUILTINS.includes(lock.role) ? "exact-lock" : "custom-binding")
      : (policy.roles[lock.role] ? "role-pool" : "all-pool");
    const identities = fixed ? [lock] : (policy.roles[lock.role] ?? policy.all);
    const candidates = identities.map((identity) => available.get(key(identity)));
    if (candidates.some((candidate) => !candidate)) {
      throw new Error("dynamic_planning_locked_model_unavailable");
    }
    const eligible = candidates.filter((candidate) => lock.thinking === undefined
      || candidate.thinkingLevels.includes(lock.thinking));
    if (!eligible.length) throw new Error("dynamic_planning_locked_thinking_unsupported");
    candidatesByRole.set(lock.role, new Set(eligible.map(key)));
    roles.push({ role: lock.role, source, count: eligible.length });
  }
  const eligibleKeys = new Set([...candidatesByRole.values()].flatMap((items) => [...items]));
  const candidates = catalog.filter((candidate) => eligibleKeys.has(key(candidate)));
  if (!candidates.length || candidates.length > 100) throw new Error("worker_candidates_limit_exceeded");
  const metadata = { version: 1, source: policy ? "configured" : "authoritative-locks", count: candidates.length, roles };
  const digest = metadataDigest({
    metadata,
    capabilities: plannerCandidateDigest(candidates),
    eligibility: [...candidatesByRole].map(([role, items]) => ({ role, identities: [...items].sort() })),
  });
  return { candidates, candidatesByRole, metadata, digest };
}

export function workerCandidateAllowed(scope, role, provider, model) {
  return scope.candidatesByRole.get(role)?.has(key({ provider, model })) === true;
}

export function workerCandidatesConfirmation(metadata) {
  if (!metadata) return "Worker candidates: unavailable (legacy planning record)";
  return `Approved worker candidates: source=${metadata.source}; count=${metadata.count}\n`
    + metadata.roles.map((role) => `${role.role}: source=${role.source}; count=${role.count}`).join("\n");
}
