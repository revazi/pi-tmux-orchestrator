const MAX_VISIBLE_CHARS = 12_000;
const MAX_MODEL_RESULTS = 100;
const MAX_MODEL_SCAN = 4096;

export const ROLES = ["implementer", "reviewer", "probe", "playwright", "django"];
export const MODEL_ROLES = ["all", ...ROLES];
const THINKING_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];
export const modelOverrideParameters = {
  type: "object",
  additionalProperties: false,
  properties: {
    provider: { type: "string", maxLength: 256 },
    model: { type: "string", maxLength: 256 },
    thinking: { type: "string", enum: THINKING_LEVELS },
  },
};

function bounded(value, limit) {
  let text = String(value ?? "");
  text = text.replace(/[\u0000-\u001f\u007f]+/g, " ");
  text = text.replace(/\s+/g, " ").trim();
  if (text.length > limit) text = `${text.slice(0, Math.max(0, limit - 1)).trimEnd()}…`;
  return text;
}

function modelIdentifier(value, field) {
  const candidate = String(value || "");
  if (!candidate || candidate.length > 256 || /[\s\u0000-\u001f\u007f]/.test(candidate)) {
    throw new Error(`invalid_${field}_identifier`);
  }
  return candidate;
}

export function startInputWithParentModel(input, ctx) {
  if (!input.useParentModel) return input;
  if (!ctx.model?.provider || !ctx.model?.id) throw new Error("parent_model_unavailable");
  const thinking = THINKING_LEVELS.includes(ctx.thinkingLevel) ? ctx.thinkingLevel : "off";
  const overrides = input.modelOverrides || {};
  const roleOverrides = Object.fromEntries(
    ROLES.filter((role) => overrides[role]).map((role) => [role, overrides[role]]),
  );
  return {
    ...input,
    modelOverrides: {
      all: {
        provider: ctx.model.provider,
        model: ctx.model.id,
        thinking,
        ...(overrides.all || {}),
      },
      ...roleOverrides,
    },
  };
}

export function appendModelArgs(args, input) {
  const overrides = input.modelOverrides || {};
  for (const role of ROLES) appendRoleModelArgs(args, role, overrides);
}

function appendRoleModelArgs(args, role, overrides) {
  const roleOverride = { ...(overrides.all || {}), ...(overrides[role] || {}) };
  appendIdentifierArg(args, `--${role}-provider`, roleOverride.provider, "provider");
  appendIdentifierArg(args, `--${role}-model`, roleOverride.model, "model");
  if (roleOverride.thinking === undefined) return;
  if (!THINKING_LEVELS.includes(roleOverride.thinking)) throw new Error("invalid_thinking_level");
  args.push(`--${role}-thinking`, roleOverride.thinking);
}

function appendIdentifierArg(args, flag, value, field) {
  if (value !== undefined) args.push(flag, modelIdentifier(value, field));
}

export function availableThinkingLevels(model, pinnedThinking) {
  if (THINKING_LEVELS.includes(pinnedThinking)) return [pinnedThinking];
  if (model?.reasoning !== true) return ["off"];
  const levelMap = model.thinkingLevelMap || {};
  const standard = ["off", "minimal", "low", "medium", "high"]
    .filter((level) => levelMap[level] !== null);
  const extended = ["xhigh", "max"]
    .filter((level) => typeof levelMap[level] === "string");
  return [...standard, ...extended];
}

export function modelCatalogEnvelope(ctx, query = "") {
  const normalizedQuery = bounded(query, 200).toLowerCase();
  const scoped = Array.isArray(ctx.scopedModels) && ctx.scopedModels.length > 0;
  const source = scoped
    ? ctx.scopedModels
    : (ctx.modelRegistry?.getAvailable?.() || []).map((model) => ({ model }));
  const { matches, scanned } = collectModelMatches(source, normalizedQuery);
  const models = matches.slice(0, MAX_MODEL_RESULTS);
  return {
    schema_version: "1",
    command: "models",
    success: true,
    data: {
      query: normalizedQuery || null,
      scoped,
      total: matches.length,
      shown: models.length,
      truncated: matches.length > models.length || source.length > scanned,
      catalog_scan_truncated: source.length > scanned,
      models,
    },
    error: null,
  };
}

function collectModelMatches(source, query) {
  const unique = new Map();
  let scanned = 0;
  for (const entry of source) {
    if (scanned >= MAX_MODEL_SCAN) break;
    scanned += 1;
    const item = publicModelEntry(entry, query);
    if (item && !unique.has(item.key)) unique.set(item.key, item.value);
  }
  const matches = [...unique.values()].sort((left, right) =>
    `${left.provider}/${left.model}`.localeCompare(`${right.provider}/${right.model}`));
  return { matches, scanned };
}

function publicModelEntry(entry, query) {
  const model = entry?.model;
  if (!model || typeof model.provider !== "string" || typeof model.id !== "string") return undefined;
  const haystack = `${model.provider}/${model.id} ${model.name || ""}`.toLowerCase();
  if (query && !haystack.includes(query)) return undefined;
  return {
    key: `${model.provider}/${model.id}`,
    value: {
      provider: bounded(model.provider, 256),
      model: bounded(model.id, 256),
      name: bounded(model.name || model.id, 256),
      reasoning: model.reasoning === true,
      thinking_levels: availableThinkingLevels(model, entry.thinkingLevel),
    },
  };
}

export function modelCatalogContent(data) {
  const lines = (data.models || []).map((item) =>
    `${item.provider}/${item.model} thinking=${item.thinking_levels.join(",")}`);
  const header = `${data.shown}/${data.total} available model(s)${data.query ? ` matching ${data.query}` : ""}${data.truncated ? " (truncated; refine query)" : ""}`;
  return bounded([header, ...lines].join("; "), MAX_VISIBLE_CHARS);
}
