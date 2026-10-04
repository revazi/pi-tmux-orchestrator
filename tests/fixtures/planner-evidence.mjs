// Model-free cross-language acceptance fixture. All completions are local stubs.
import { fileURLToPath } from "node:url";
import { runPreflightPlanner, selectDecisionModel, validatePlannerTopology, planningRecordForPreview } from "../../extensions/orchestrator-planner.js";

export async function syntheticEvidenceFixture({ source = "typesafe_choice", scopes, mixed = false, fixed = false, custom = false, mutateAnswer, modelCount = 5 } = {}) {
  const models = Array.from({ length: modelCount }, (_, index) => ({
    provider: mixed && index === modelCount - 1 ? "q" : "p", id: `candidate-${index}`,
    reasoning: true, input: ["text", "image"], contextWindow: 32000, maxTokens: 4000,
    thinkingLevelMap: { off: null, minimal: null, low: "low", medium: "medium", high: "high" },
    cost: { input: index === 0 ? 1e-7 : index === 1 ? 1e-6 : index, output: index + 1, cacheRead: 0, cacheWrite: 0, tiers: [{ inputTokensAbove: 10000, input: 2, output: 3, cacheRead: 0, cacheWrite: 0 }] },
    promptCache: { short: 300, long: 3600 },
    baseUrl: "PRIVATE_CATALOG_ENDPOINT", headers: { key: "PRIVATE_CREDENTIAL" }, description: "PRIVATE_CATALOG_BODY",
  }));
  const raw = {
    version: 3,
    builtins: Object.fromEntries(["implementer", "reviewer", "probe", "playwright", "django"].map((role) => {
      const model = mixed && role === "reviewer" ? models.at(-1) : models[0];
      const effective = { provider: model.provider, model: model.id, thinking: "low" };
      return [role, { effective, constraint: fixed || mixed && role === "reviewer" ? effective : {} }];
    })),
    static_roles: [], optional_roles: fixed ? [] : ["probe"],
    custom_roles: custom ? [{ role: "custom-security", contract: "probe", provider: "p", model: models[0].id, thinking: "low" }] : [],
    worker_candidates: { version: 1, all: models.map((item) => ({ provider: item.provider, model: item.id })), roles: {} },
  };
  const topology = validatePlannerTopology(raw);
  let captured;
  const answer = (request) => {
    captured = request;
    const answers = Object.fromEntries(Object.entries(request.questions).map(([id, question]) => {
      const labels = Object.keys(question.criteria);
      const choice = id.startsWith("include_") ? "omit" : id === "task_intent" ? "change" : labels[0];
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
    contextCapsule: { currentState: ["PRIVATE_CONTEXT_BODY"] },
  }, "/synthetic", selection, topology, undefined, undefined, {
    typesafeApiKey: "SYNTHETIC_KEY", dynamicGuidance: { text: "PRIVATE_GUIDANCE_BODY" },
    typesafeFetch: async (_endpoint, options) => new Response(JSON.stringify({
      model: "jev-1.13.0", answers: answer(JSON.parse(options.body)), usage: { input_tokens: 10, output_tokens: 5 },
    }), { status: 200 }),
  });
  return { ...result, record: planningRecordForPreview(result.plan), request: captured };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const scopes = [
    ["topology"], ["models"], ["thinking"], ["topology", "models"],
    ["topology", "thinking"], ["models", "thinking"], ["topology", "models", "thinking"],
  ];
  const options = [
    ...scopes.map((value) => ({ scopes: value })), { source: "pi_selection" },
    { mixed: true }, { fixed: true }, { custom: true },
  ];
  console.log(JSON.stringify(await Promise.all(options.map(async (option) => (await syntheticEvidenceFixture(option)).record))));
}
