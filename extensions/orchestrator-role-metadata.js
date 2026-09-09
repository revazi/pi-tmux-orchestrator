import { validCustomRoleId, validWorkerRole, workerRoleContract } from "./orchestrator-worker-roles.js";

const MAX_SURFACE_ROLES = 13;
export const controlRoleParameters = {
  type: "string",
  maxLength: 32,
  pattern: "^(?:implementer|reviewer|probe|playwright|django|custom-[a-z][a-z0-9]*(?:-[a-z0-9]+)*)$",
  description: "Exact enabled built-in or custom role for send; target run membership is checked by the CLI",
};

export function validControlRole(role) {
  return validWorkerRole(role, undefined) || validCustomRoleId(role);
}

// Consume only the authoritative CLI's bounded public manifest projection.
// Never derive selections/contracts from observer snapshots, reports, or prompts.
export function publicRoleContracts(roles) {
  if (!Array.isArray(roles) || roles.length < 2 || roles.length > MAX_SURFACE_ROLES) {
    throw new Error("invalid_public_roles");
  }
  const contracts = new Map();
  let customCount = 0;
  for (const item of roles) {
    if (!item || contracts.has(item.name)) throw new Error("invalid_public_roles");
    const contract = workerRoleContract(item.name, item.specialist_contract);
    if (validCustomRoleId(item.name)) {
      customCount += 1;
      if (customCount > 8 || item.tool_policy !== "custom-read-only-no-shell") {
        throw new Error("invalid_public_role_policy");
      }
    }
    contracts.set(item.name, contract);
  }
  if (!contracts.has("implementer") || !contracts.has("reviewer")) {
    throw new Error("missing_public_required_roles");
  }
  return contracts;
}
