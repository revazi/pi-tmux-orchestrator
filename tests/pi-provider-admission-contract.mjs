// Installed-Pi SDK dispatch contract, with an injected fetch sentinel only.
// No ModelRuntime/AuthStorage construction, credential reads, network, or inference.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, realpathSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

let piRoot = process.argv[2] ?? dirname(realpathSync(execFileSync("which", ["pi"], { encoding: "utf8" }).trim()));
while (!existsSync(join(piRoot, "dist/core/sdk.js"))) {
  if (process.argv[2] || piRoot === dirname(piRoot)) throw new Error("installed_pi_sdk_unavailable");
  piRoot = dirname(piRoot);
}
const { version } = JSON.parse(await readFile(join(piRoot, "package.json"), "utf8"));
// Older SDK factories may ignore modelRuntime and construct file-backed auth.
// Review their isolation contract before permitting this probe to execute them.
assert.equal(version, "1.4.0", "provider_admission_probe_requires_reviewed_pi_1_4_0");
const installed = (path) => import(pathToFileURL(join(piRoot, path)));
const { createAgentSession } = await installed("dist/core/sdk.js");
const { SessionManager } = await installed("dist/core/session-manager.js");
const { SettingsManager } = await installed("dist/core/settings-manager.js");
const { createExtensionRuntime, loadExtensionFromFactory } = await installed("dist/core/extensions/loader.js");
const { createEventBus } = await installed("dist/core/event-bus.js");
const { InteractiveMode } = await installed("dist/modes/interactive/interactive-mode.js");

// A deliberately synthetic model, never a selectable worker or catalog identity.
const fixtureModel = {
  id: "synthetic-admission-contract", name: "Synthetic admission contract", provider: "synthetic",
  baseUrl: "https://example.invalid/v1", reasoning: false, input: ["text"],
  contextWindow: 32768, maxTokens: 1024,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
};

async function checkDispatch(api, mode, decision) {
  const model = { ...fixtureModel, api };
  const { stream } = await installed(`node_modules/@earendil-works/pi-ai/dist/api/${api}.js`);
  const runtime = createExtensionRuntime();
  let hooks = 0;
  let dispatches = 0;
  let observedMode;
  const extension = await loadExtensionFromFactory((pi) => {
    pi.on("before_provider_request", async (_event, ctx) => {
      hooks += 1;
      observedMode = ctx.mode;
      if (decision === "throw_only") throw new Error("synthetic_denial");
      if (decision === "allow") return undefined;
      // Simulate an async authorization result, not a broker concurrency test.
      try {
        await Promise.resolve();
        if (decision === "authorization_failure") throw new Error("synthetic_transport_failure");
        throw new Error("synthetic_allowance_exhausted");
      } catch {
        // Throwing alone is fail-open in Pi's transforming hook. Abort first.
        ctx.abort();
        throw new Error("synthetic_provider_turn_denied");
      }
    });
  }, process.cwd(), createEventBus(), runtime);
  const resourceLoader = {
    getExtensions: () => ({ extensions: [extension], errors: [], runtime }),
    getSkills: () => ({ skills: [], diagnostics: [] }),
    getPrompts: () => ({ prompts: [], diagnostics: [] }),
    getThemes: () => ({ themes: [], diagnostics: [] }),
    getAgentsFiles: () => ({ agentsFiles: [] }),
    getSystemPrompt: () => "Synthetic dispatch-boundary check.",
    getSystemPromptSource: () => undefined,
    getAppendSystemPrompt: () => [], getAppendSystemPromptSources: () => [],
    extendResources() {}, async reload() {},
  };
  // Replace the registry/auth boundary, not the SDK/agent/provider dispatch path.
  const modelRuntime = {
    getModel: () => model,
    hasConfiguredAuth: () => true,
    getAuth: async () => ({ apiKey: "synthetic-not-a-credential" }),
    streamSimple: (selected, context, options) => stream(selected, context, {
      ...options, apiKey: "synthetic-not-a-credential", maxRetries: 0,
      fetch: async () => {
        dispatches += 1;
        // Never delegate to real fetch or simulate a provider response.
        throw new Error("synthetic_fetch_sentinel");
      },
    }),
  };
  const { session } = await createAgentSession({
    model, modelRuntime, resourceLoader, tools: [], thinkingLevel: "off",
    sessionManager: SessionManager.inMemory(),
    settingsManager: SettingsManager.inMemory({
      compaction: { enabled: false }, retry: { enabled: false, maxRetries: 0 }, cacheWarming: "off",
    }),
  });
  const errors = [];
  try {
    const bindings = { mode, onError: (error) => errors.push(error) };
    if (mode === "tui") {
      // Exercise the real TUI abort adapter, without constructing a terminal UI.
      // Only rendering/queue inputs are synthetic; it calls the real session.abort.
      const ui = {
        session, clearAllQueues: () => ({ steering: [], followUp: [] }),
        updatePendingMessagesDisplay() {},
      };
      bindings.abortHandler = () => InteractiveMode.prototype.restoreQueuedMessagesToEditor.call(ui, { abort: true });
    }
    await session.bindExtensions(bindings);
    await session.prompt("Synthetic boundary check.");
    assert.equal(observedMode, mode);
    assert.equal(hooks, 1, `${api}/${mode}/${decision}: hook must execute exactly once`);
    const denied = decision === "deny" || decision === "authorization_failure";
    assert.equal(dispatches, denied ? 0 : 1, `${api}/${mode}/${decision}: wrong dispatch count`);
    assert.equal(session.messages.at(-1).stopReason, denied ? "aborted" : "error");
    assert.equal(errors.length, decision === "allow" ? 0 : 1);
    assert.equal(session.isIdle, true);
  } finally {
    session.dispose();
  }
}

// Guard against accidental fallback network paths, including cache warming.
const originalFetch = globalThis.fetch;
let unexpectedDispatches = 0;
globalThis.fetch = async () => {
  unexpectedDispatches += 1;
  throw new Error("unexpected_global_network_dispatch");
};
try {
  let checks = 0;
  for (const api of ["openai-completions", "openai-responses"]) {
    for (const mode of ["tui", "rpc"]) {
      for (const decision of ["allow", "throw_only", "deny", "authorization_failure"]) {
        await checkDispatch(api, mode, decision);
        checks += 1;
      }
    }
  }
  assert.equal(unexpectedDispatches, 0, "a request bypassed the injected fetch sentinel");
  console.log(`Installed Pi ${version}: ${checks} SDK provider-admission checks passed (synthetic fetch, no network/inference).`);
} finally {
  globalThis.fetch = originalFetch;
}
