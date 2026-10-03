// Independent preflight authority; fixed axes resolve before candidate admission.
const SCOPES = ["topology", "models", "thinking"];
const FIELDS = { probe: "withProbe", playwright: "withPlaywright", django: "withDjangoExpert" };
const TASKS = { probe: "probeTask", playwright: "playwrightTask", django: "djangoTask" };

export function planningScopes(input) {
  const value = input.planningScopes;
  if (value === undefined) return [...SCOPES];
  if (input.dynamicPlan !== true) throw new Error("planning_scopes_require_dynamic_plan");
  if (!Array.isArray(value) || !value.length || value.length > SCOPES.length
      || new Set(value).size !== value.length || value.some((axis) => !SCOPES.includes(axis))) {
    throw new Error("invalid_planning_scopes");
  }
  return SCOPES.filter((axis) => value.includes(axis));
}

function fixedConstraint(input, role, item, scopes) {
  const overrides = { ...(input.modelOverrides?.all ?? {}), ...(input.modelOverrides?.[role] ?? {}) };
  const fixed = { ...item.constraint };
  if (!scopes.includes("models")) {
    if (!item.effective) throw new Error("planning_scopes_policy_unavailable");
    fixed.provider = item.effective.provider;
    fixed.model = item.effective.model;
  }
  if (!scopes.includes("thinking")) {
    if (!item.effective) throw new Error("planning_scopes_policy_unavailable");
    fixed.thinking = item.effective.thinking;
  }
  return { ...item, constraint: { ...fixed, ...overrides } };
}

function fixedBuiltinRoster(input, topology) {
  if (!Array.isArray(topology.staticRoles)) throw new Error("planning_scopes_policy_unavailable");
  const fixedRoles = ["implementer", "reviewer"];
  for (const [role, field] of Object.entries(FIELDS)) {
    const required = input[field] === true || Boolean(input[TASKS[role]]) || input.forceSpecialists?.includes(role);
    if (required && input[field] === false) throw new Error("dynamic_planning_specialist_constraint_conflict");
    if (required || input[field] !== false && topology.staticRoles.includes(role)) fixedRoles.push(role);
  }
  return fixedRoles;
}

function fixedCustomRoster(input, topology) {
  const requested = input.requiredCustomRoleIds;
  if (requested !== undefined && (!Array.isArray(requested)
      || new Set(requested).size !== requested.length
      || requested.some((role) => !topology.customRoles.some((item) => item.role === role)))) {
    throw new Error("dynamic_planning_custom_role_not_allowlisted");
  }
  if (input.projectCustomRoles === false && requested?.length) throw new Error("dynamic_planning_custom_role_constraint_conflict");
  return input.projectCustomRoles === false ? [] : topology.customRoles.filter((role) => requested === undefined || requested.includes(role.role));
}

export function scopedTopology(input, topology) {
  const scopes = planningScopes(input);
  if (topology.scopes) {
    if (JSON.stringify(topology.scopes) !== JSON.stringify(scopes)) throw new Error("planning_scope_constraint_conflict");
    return topology;
  }
  const builtins = Object.fromEntries(Object.entries(topology.builtins).map(
    ([role, item]) => [role, fixedConstraint(input, role, item, scopes)],
  ));
  if (scopes.includes("topology")) return { ...topology, builtins, scopes };
  const fixedRoles = fixedBuiltinRoster(input, topology);
  const customRoles = fixedCustomRoster(input, topology);
  return { ...topology, builtins, customRoles, scopes, fixedRoles: [...fixedRoles, ...customRoles.map((role) => role.role)] };
}

export function planningLocks(topology, roles, required, constraints) {
  const fixed = topology.fixedRoles;
  return constraints.map(({ role, provider, model, thinking }) => ({
    role,
    inclusion: fixed ? fixed.includes(role) : required.has(role) ? true : null,
    provider: provider ?? null,
    model: model ?? null,
    thinking: thinking ?? null,
  })).concat(Object.keys(FIELDS).filter((role) => !roles.includes(role)).map((role) => ({
    role, inclusion: false, provider: null, model: null, thinking: null,
  })));
}

export function planningScopesConfirmation(scopes, locks = [], showIdentities = true) {
  if (!scopes) return "Planning scopes: unavailable (legacy record).\nLocks: unavailable (legacy record).";
  return [
    `Planning scopes: ${scopes.join(", ")}.`,
    "Only authorized, unlocked axes are decision questions. Other values are authoritative operator/policy locks, not planner choices.",
    ...(locks.length ? locks.map((lock) => {
      if (lock.inclusion === false) return `${lock.role}: inclusion=locked omit; model=not applicable; thinking=not applicable`;
      const inclusion = lock.inclusion === true ? "locked include" : "planner";
      const model = lock.provider === null ? "planner" : showIdentities ? `locked ${lock.provider}/${lock.model}` : "locked";
      return `${lock.role}: inclusion=${inclusion}; model=${model}; thinking=${lock.thinking === null ? "planner" : `locked ${lock.thinking}`}`;
    }) : ["Locks: unavailable (legacy record)."]),
  ].join("\n");
}
