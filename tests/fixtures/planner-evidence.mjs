// Model-free cross-language acceptance fixture. All completions are local stubs.
import { fileURLToPath } from "node:url";
import { runPreflightPlanner, selectDecisionModel, validatePlannerTopology, planningRecordForPreview } from "../../extensions/orchestrator-planner.js";

export async function syntheticEvidenceFixture({ source = "typesafe_choice", scopes, mixed = false, fixed = false, custom = false, mutateAnswer, modelCount = 5, multiProvider = false, assignMixed = false, composition, customMixed = false, includeSpecialists = false, lockMandatory = false, inputOverrides = {}, candidatePools = {}, support, mutateModels, onCall, customCount = 1, heterogeneous = false } = {}) {
  const models = Array.from({ length: modelCount }, (_, index) => ({
    provider: (mixed || multiProvider || customMixed) && index === modelCount - 1 ? "q" : "p", id: `candidate-${index}`,
    reasoning: true, input: ["text", "image"], contextWindow: index === modelCount - 1 ? 32000 : 64000, maxTokens: 4000,
    thinkingLevelMap: { off: null, minimal: null, low: "low", medium: "medium", high: "high" },
    cost: { input: index === 0 ? 1e-7 : index === 1 ? 1e-6 : index, output: index + 1, cacheRead: 0, cacheWrite: 0, tiers: [{ inputTokensAbove: 10000, input: 2, output: 3, cacheRead: 0, cacheWrite: 0 }] },
    promptCache: { short: 300, long: 3600 },
    baseUrl: "PRIVATE_CATALOG_ENDPOINT", headers: { key: "PRIVATE_CREDENTIAL" }, description: "PRIVATE_CATALOG_BODY",
  }));
  if (heterogeneous) {
    models.forEach((model, index) => {
      model.thinkingLevelMap = index % 2 === 0
        ? { off: null, minimal: null, low: "low", medium: "medium", high: "high" }
        : { off: null, minimal: null, low: "low", medium: null, high: null };
    });
  }
  if (mutateModels) mutateModels(models);
  const raw = {
    version: 3,
    builtins: Object.fromEntries(["implementer", "reviewer", "probe", "playwright", "django"].map((role) => {
      const model = mixed && role === "reviewer" ? models.at(-1) : models[0];
      const effective = { provider: model.provider, model: model.id, thinking: "low" };
      return [role, { effective, constraint: fixed || lockMandatory && ["implementer", "reviewer"].includes(role) || mixed && role === "reviewer" ? effective : {} }];
    })),
    static_roles: [], optional_roles: fixed ? [] : ["probe"],
    custom_roles: custom || customMixed ? Array.from({ length: customCount }, (_, index) => ({ role: customCount === 1 ? "custom-security" : `custom-security-${index}`, contract: "probe", provider: customMixed ? "q" : "p", model: customMixed ? models.at(-1).id : models[0].id, thinking: "low" })) : [],
    worker_candidates: { version: 1, all: models.map((item) => ({ provider: item.provider, model: item.id })), roles: candidatePools },
  };
  const topology = validatePlannerTopology(raw);
  let captured;
  let calls = 0;
  const answer = (request) => {
    calls += 1;
    onCall?.();
    captured = request;
    const answers = Object.fromEntries(Object.entries(request.questions).map(([id, question]) => {
      const labels = Object.keys(question.criteria);
      const choice = id === "provider_composition" ? composition ?? (mixed || assignMixed || customMixed && includeSpecialists ? "mixed_provider" : "single_provider")
        : id === "provider_support" ? support ?? (mixed || assignMixed || lockMandatory || customMixed && includeSpecialists ? "none" : "context_window")
        : id === "model_01" && assignMixed ? `m${(modelCount - 1).toString(36)}`
        : id.startsWith("thinking_") ? labels.find((level) => request.state.candidate_model_capabilities[id === "thinking_01" && assignMixed ? modelCount - 1 : 0].thinking_levels.includes(level))
        : id.startsWith("include_") ? includeSpecialists ? "include" : "omit" : id === "task_intent" ? "change" : labels[0];
      const probabilities = Object.fromEntries(labels.map((label) => [label, labels.length === 1 ? 1 : label === choice ? 0.4 : 0.6 / (labels.length - 1)]));
      const item = { type: "choice", choice, confidence: 0.77, probabilities };
      if (mutateAnswer) mutateAnswer(id, item);
      return [id, source === "pi_selection" ? item.choice : item];
    }));
    return answers;
  };
  const ctx = { modelRegistry: { getAvailable: () => models, complete: async (_model, value) => ({
    stopReason: "stop", content: [{ type: "text", text: JSON.stringify({ answers: answer(JSON.parse(value.messages[0].content[0].text)) }) }],
  }) } };
  const selection = selectDecisionModel(ctx, { provider: "p", model: models[0].id }, { version: 1, preferred: null, fallbacks: [], no_eligible: "cancel" }, [], source === "typesafe_choice" ? { typesafeApiKey: "SYNTHETIC_KEY" } : {});
  const result = await runPreflightPlanner(ctx, {
    dynamicPlan: true, ...(scopes ? { planningScopes: scopes } : {}), task: "PRIVATE_TASK_BODY", taskIntent: "change",
    contextCapsule: { currentState: ["PRIVATE_CONTEXT_BODY"] }, ...inputOverrides,
  }, "/synthetic", selection, topology, undefined, undefined, {
    typesafeApiKey: "SYNTHETIC_KEY", dynamicGuidance: { text: "PRIVATE_GUIDANCE_BODY" },
    typesafeFetch: async (_endpoint, options) => new Response(JSON.stringify({
      model: "jev-1.13.0", answers: answer(JSON.parse(options.body)), usage: { input_tokens: 10, output_tokens: 5 },
    }), { status: 200 }),
  });
  return { ...result, record: planningRecordForPreview(result.plan), request: captured, calls };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const scopes = [
    ["topology"], ["models"], ["thinking"], ["topology", "models"],
    ["topology", "thinking"], ["models", "thinking"], ["topology", "models", "thinking"],
  ];
  const options = [
    ...scopes.map((value) => ({ scopes: value })), { source: "pi_selection" },
    { mixed: true }, { fixed: true }, { custom: true },
    ...["typesafe_choice", "pi_selection"].flatMap((source) => [
      ...scopes.map((scopes) => ({ source, scopes, multiProvider: true })),
      { source, multiProvider: true, assignMixed: true },
      { source, customMixed: true, lockMandatory: true, includeSpecialists: true },
      { source, customMixed: true, fixed: true, inputOverrides: { projectCustomRoles: true } },
      { source, mixed: true, inputOverrides: { modelOverrides: { implementer: { provider: "p", model: "candidate-0", thinking: "low" } }, withProbe: false } },
    ]),
  ];
  for (const source of ["typesafe_choice", "pi_selection"]) {
    options.push({ source, fixed: true, custom: true, customCount: 8, inputOverrides: { projectCustomRoles: true, withProbe: true, withPlaywright: true, withDjangoExpert: true } });
    options.push({ source, multiProvider: true, includeSpecialists: true });
    options.push({ source, heterogeneous: true });
    options.push({ source, multiProvider: true, support: "image", mutateModels: (models) => { models.at(-1).input = ["text"]; } });
    options.push({ source, multiProvider: true, support: "declared_cost", mutateModels: (models) => { models.forEach((model, index) => { model.cost = { input: index === models.length - 1 ? 2 : 1, output: index === models.length - 1 ? 2 : 1, cacheRead: 0, cacheWrite: 0 }; }); } });
  }
  console.log(JSON.stringify(await Promise.all(options.map(async (option) => (await syntheticEvidenceFixture(option)).record))));
}
