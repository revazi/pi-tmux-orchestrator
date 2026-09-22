const MAX_VISIBLE_CHARS = 12_000;
const MAX_MODEL_RESULTS = 100;
const MAX_MODEL_SCAN = 4096;
const MAX_COST_TIERS = 16;
const MAX_INPUT_MODALITIES = 16;
const MAX_CATALOG_RATE = 1_000_000;
const MISSING = "missing";
const ZERO = "zero";
const DECLARED = "declared";
const UNAVAILABLE = "unavailable";

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

function catalogStatus(value) {
  if (value === undefined || value === null) return MISSING;
  return UNAVAILABLE;
}

function catalogInteger(value) {
  if (value === undefined || value === null) return { status: MISSING, tokens: null };
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    return { status: UNAVAILABLE, tokens: null };
  }
  if (value === 0) return { status: ZERO, tokens: 0 };
  return { status: DECLARED, tokens: value };
}

function catalogRate(value) {
  if (value === undefined || value === null) return { status: MISSING, amount: null };
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > MAX_CATALOG_RATE) {
    return { status: UNAVAILABLE, amount: null };
  }
  if (value === 0) return { status: ZERO, amount: 0 };
  return { status: DECLARED, amount: value };
}

function catalogBooleanFlag(value) {
  if (value === true) return true;
  if (value === false) return false;
  return catalogStatus(value);
}

function projectInputSupport(value) {
  if (value === undefined || value === null) {
    return { text: MISSING, image: MISSING };
  }
  if (!Array.isArray(value) || value.length > MAX_INPUT_MODALITIES) {
    return { text: UNAVAILABLE, image: UNAVAILABLE };
  }
  const known = new Set();
  for (const item of value) {
    if (item === "text" || item === "image") known.add(item);
  }
  return {
    text: known.has("text"),
    image: known.has("image"),
  };
}

function projectCostRates(cost) {
  const input = catalogRate(cost.input);
  const output = catalogRate(cost.output);
  const cacheRead = catalogRate(cost.cacheRead);
  const cacheWrite = catalogRate(cost.cacheWrite);
  if ([input, output, cacheRead, cacheWrite].some((item) => item.status !== DECLARED && item.status !== ZERO)) {
    return undefined;
  }
  return {
    input: input.amount,
    output: output.amount,
    cache_read: cacheRead.amount,
    cache_write: cacheWrite.amount,
    zero: [input, output, cacheRead, cacheWrite].every((item) => item.amount === 0),
  };
}

function projectCostTier(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const threshold = catalogInteger(value.inputTokensAbove);
  const rates = projectCostRates(value);
  if (!rates || threshold.status === MISSING || threshold.status === UNAVAILABLE) return undefined;
  return {
    input_tokens_above: threshold.tokens,
    input: rates.input,
    output: rates.output,
    cache_read: rates.cache_read,
    cache_write: rates.cache_write,
  };
}

function projectCostTiers(value) {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value) || value.length > MAX_COST_TIERS) return undefined;
  const tiers = [];
  const seen = new Set();
  for (const item of value) {
    const tier = projectCostTier(item);
    if (!tier) return undefined;
    if (seen.has(tier.input_tokens_above)) return undefined;
    seen.add(tier.input_tokens_above);
    tiers.push(tier);
  }
  tiers.sort((left, right) => left.input_tokens_above - right.input_tokens_above);
  return tiers;
}

function projectDeclaredCost(value) {
  const empty = {
    status: UNAVAILABLE,
    input: null,
    output: null,
    cache_read: null,
    cache_write: null,
    tiers: null,
  };
  if (value === undefined || value === null) return { ...empty, status: MISSING };
  if (typeof value !== "object" || Array.isArray(value)) return empty;
  const rates = projectCostRates(value);
  const tiers = projectCostTiers(value.tiers);
  if (!rates || tiers === undefined) return empty;
  const zero = rates.zero && tiers.every((tier) => (
    tier.input === 0
    && tier.output === 0
    && tier.cache_read === 0
    && tier.cache_write === 0
  ));
  return {
    status: zero ? ZERO : DECLARED,
    input: rates.input,
    output: rates.output,
    cache_read: rates.cache_read,
    cache_write: rates.cache_write,
    tiers,
  };
}

function retentionPresence(value) {
  if (value === undefined || value === null) return false;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return UNAVAILABLE;
  return true;
}

function projectPromptCacheRetention(value) {
  if (value === undefined || value === null) {
    return { status: MISSING, short: false, long: false };
  }
  if (typeof value !== "object" || Array.isArray(value)) {
    return { status: UNAVAILABLE, short: UNAVAILABLE, long: UNAVAILABLE };
  }
  const short = retentionPresence(value.short);
  const long = retentionPresence(value.long);
  if (short === UNAVAILABLE || long === UNAVAILABLE) {
    return { status: UNAVAILABLE, short, long };
  }
  if (short === false && long === false) {
    return { status: MISSING, short: false, long: false };
  }
  return { status: DECLARED, short, long };
}

export function projectModelCapabilities(model) {
  return {
    reasoning: catalogBooleanFlag(model?.reasoning),
    input: projectInputSupport(model?.input),
    context_window: catalogInteger(model?.contextWindow),
    max_output_tokens: catalogInteger(model?.maxTokens),
    declared_cost: projectDeclaredCost(model?.cost),
    prompt_cache_retention: projectPromptCacheRetention(model?.promptCache),
  };
}

function registryModels(ctx) {
  const registry = ctx && ctx.modelRegistry;
  const getAvailable = registry && registry.getAvailable;
  return typeof getAvailable === "function" ? getAvailable.call(registry) : [];
}

function selectedModels(ctx) {
  const scopedModels = ctx && ctx.scopedModels;
  if (Array.isArray(scopedModels) && scopedModels.length > 0) {
    return { models: scopedModels, scoped: true };
  }
  return { models: registryModels(ctx), scoped: false };
}

function modelKey(model) {
  if (!model || typeof model.provider !== "string" || typeof model.id !== "string") {
    return undefined;
  }
  return `${model.provider}\0${model.id}`;
}

function availableModelKeys(selection) {
  const keys = new Set();
  for (const entry of selection.models.slice(0, MAX_MODEL_SCAN)) {
    const key = modelKey(selection.scoped ? entry.model : entry);
    if (key) keys.add(key);
  }
  return keys;
}

function roleModelKey(role) {
  if (!role) return undefined;
  return modelKey({ provider: role.provider, id: role.model });
}

export function previewModelsAreAvailable(ctx, roles) {
  if (!Array.isArray(roles) || roles.length === 0) return false;
  const selection = selectedModels(ctx);
  if (!Array.isArray(selection.models) || selection.models.length === 0) return false;
  const available = availableModelKeys(selection);
  return roles.every((role) => available.has(roleModelKey(role)));
}

export function modelCatalogEnvelope(ctx, query = "") {
  const normalizedQuery = bounded(query, 200).toLowerCase();
  const selection = selectedModels(ctx);
  const scoped = selection.scoped;
  const source = scoped
    ? selection.models
    : selection.models.map((model) => ({ model }));
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
