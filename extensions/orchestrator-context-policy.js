import { ROLES } from "./orchestrator-models.js";

export const workerContextParameters = {
  type: "object",
  additionalProperties: false,
  description: "Explicit per-run provider-context overrides for enabled roles. Omitted roles use prune; retain keeps prior investigation and enables advisory reuse hints, not old approval. No live switching or compact/fresh",
  properties: Object.fromEntries(ROLES.map((role) => [role, {
    type: "string", enum: ["prune", "retain"],
  }])),
};

export function validateWorkerContext(overrides = {}) {
  if (!overrides || typeof overrides !== "object" || Array.isArray(overrides)) {
    throw new Error("invalid_worker_context_overrides");
  }
  for (const [role, mode] of Object.entries(overrides)) {
    if (!ROLES.includes(role) || !["prune", "retain"].includes(mode)) {
      throw new Error("invalid_worker_context_selection");
    }
  }
  return overrides;
}

export function appendWorkerContextArgs(args, overrides) {
  for (const [role, mode] of Object.entries(validateWorkerContext(overrides))) {
    args.push("--worker-context", `${role}=${mode}`);
  }
}

function parseWorkerContext(text) {
  if (text.length > 256) throw new Error("worker_context_input_too_large");
  if (!text.trim()) return {};
  const entries = new Map();
  for (const selection of text.split(",")) {
    const pair = selection.split("=").map((value) => value.trim());
    if (pair.length !== 2 || entries.has(pair[0])) throw new Error("invalid_worker_context_input");
    entries.set(pair[0], pair[1]);
  }
  return validateWorkerContext(Object.fromEntries(entries));
}

export async function selectWorkerContext(ctx) {
  const text = await ctx.ui.input(
    "Worker context overrides (blank keeps prune; comma-separated ROLE=MODE)",
    "reviewer=retain, implementer=prune; retain may increase cost",
  );
  if (text === undefined || text === null) return null;
  return parseWorkerContext(text);
}

function previewOverrides(data) {
  const policy = data?.worker_context_policy;
  if (policy === undefined) return undefined;
  if (!policy || policy.version !== 1 || policy.overrides === undefined) {
    throw new Error("invalid_worker_context_preview");
  }
  return validateWorkerContext(policy.overrides);
}

export function validateWorkerContextPreview(data, requested) {
  const selections = Object.entries(validateWorkerContext(requested));
  if (!selections.length) return;
  const resolved = previewOverrides(data);
  for (const [role, mode] of selections) {
    if (resolved?.[role] !== mode || !data?.roles?.some((item) => item.name === role)) {
      throw new Error("worker_context_preview_mismatch");
    }
  }
}

export function workerContextConfirmation(data) {
  const overrides = previewOverrides(data);
  if (overrides === undefined) return "unavailable";
  const selections = Object.entries(overrides).map(([role, mode]) => `${role}=${mode}`).join(", ");
  return `default=prune; per-run overrides: ${selections || "none"}\nRetain may increase context cost. Reuse hints are metadata observations, not proof of check freshness; required checks and independent review remain mandatory.`;
}
