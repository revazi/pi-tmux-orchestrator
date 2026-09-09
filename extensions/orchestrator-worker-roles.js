const BUILTIN_ROLES = new Set(["implementer", "reviewer", "probe", "playwright", "django"]);
const SPECIALIST_CONTRACTS = new Set(["probe", "playwright", "django"]);
const RESERVED_SUFFIXES = new Set([
  ...BUILTIN_ROLES, "all", "parent", "controller", "broker", "monitor", "orchestrator",
  "coordinator", "writer", "review", "system", "user", "assistant", "tool",
  "django-expert", "playwright-tester", "technical-probe",
]);
const CUSTOM_READ_ONLY_TOOLS = new Set(["read", "grep", "find", "ls", "orchestrator_report"]);

function validCustomRoleId(role) {
  return typeof role === "string" && role.length <= 32
    && /^custom-[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/.test(role)
    && !RESERVED_SUFFIXES.has(role.slice(7));
}

export function workerRoleContract(role, specialistContract) {
  if (BUILTIN_ROLES.has(role) && specialistContract === undefined) return role;
  if (validCustomRoleId(role) && SPECIALIST_CONTRACTS.has(specialistContract)) return specialistContract;
  throw new Error("invalid_worker_role_contract");
}

export function validWorkerRole(role, specialistContract) {
  try {
    workerRoleContract(role, specialistContract);
    return true;
  } catch {
    return false;
  }
}

export function customRoleTools(role, tools) {
  return validCustomRoleId(role) ? tools.filter((name) => CUSTOM_READ_ONLY_TOOLS.has(name)) : [...tools];
}

export function customToolDecision(role, name) {
  if (!validCustomRoleId(role) || CUSTOM_READ_ONLY_TOOLS.has(name)) return undefined;
  return {
    block: true,
    reason: "Custom specialists are read-only: shell, edit/write, and unapproved tools are unavailable.",
  };
}

export function assertCustomAssignment(role, contract, kind) {
  if (validCustomRoleId(role) && kind !== contract) throw new Error("invalid_custom_assignment_contract");
}
