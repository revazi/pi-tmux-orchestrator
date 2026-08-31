import assert from "node:assert/strict";
import { access, chmod, mkdtemp, readFile, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import net from "node:net";
import { test } from "node:test";
import extension, { testHooks } from "../extensions/tmux-orchestrator.js";
import {
  OrchestrationDashboardOverlay,
  testHooks as dashboardHooks,
} from "../extensions/orchestrator-dashboard.js";
import { updateTestHooks as updateHooks } from "../extensions/orchestrator-update.js";
import { testHooks as workerHooks } from "../extensions/orchestrator-worker.js";
import {
  applyToolInputPolicy,
  applyToolResultPolicy,
  immediateFollowupObservation,
  RESULT_POLICY,
} from "../extensions/orchestrator-result-policy.js";
import { buildTokenEfficiencyBaseline } from "../scripts/token-efficiency-baseline.mjs";
import { buildResultVolumeBaseline } from "../scripts/result-volume-baseline.mjs";
import { buildExecutionProfileBaseline } from "../scripts/execution-profile-baseline.mjs";
import { buildPhasedImplementationBaseline } from "../scripts/phased-implementation-baseline.mjs";
import { buildWorkerPromptBaselineIfAvailable } from "../scripts/worker-prompt-baseline.mjs";

const packageJson = JSON.parse(await readFile(new URL("../package.json", import.meta.url), "utf8"));

async function drainPromises() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

async function waitFor(predicate, timeoutMs = 1_000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("timed out waiting for async extension work");
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

function success(command, data = {}) {
  return { schema_version: "1", command, success: true, data, error: null };
}

function harness(exec) {
  const tools = [];
  const commands = new Map();
  const events = new Map();
  const messages = [];
  const shortcuts = new Map();
  const pi = {
    exec,
    sendMessage(message, options) { messages.push({ message, options }); },
    registerTool(tool) { tools.push(tool); },
    registerCommand(name, command) { commands.set(name, command); },
    registerShortcut(name, shortcut) { shortcuts.set(name, shortcut); },
    on(name, handler) { events.set(name, handler); },
  };
  extension(pi);
  return { pi, tool: tools[0], tools, commands, shortcuts, events, messages };
}

function context(overrides = {}) {
  const confirmations = [...(overrides.confirmations || [])];
  const selections = overrides.selections ? [...overrides.selections] : null;
  const editors = overrides.editors ? [...overrides.editors] : null;
  const inputs = overrides.inputs ? [...overrides.inputs] : null;
  const calls = {
    confirmations: [],
    notifications: [],
    statuses: [],
    titles: [],
    widgets: [],
    selections: [],
    editors: [],
    inputs: [],
  };
  return {
    calls,
    mode: "tui",
    hasUI: true,
    cwd: process.cwd(),
    signal: overrides.signal,
    model: { provider: "synthetic-parent", id: "parent-model", reasoning: true },
    thinkingLevel: "high",
    scopedModels: [],
    modelRegistry: { getAvailable: () => [] },
    isProjectTrusted: () => overrides.trusted ?? false,
    ui: {
      confirm: async (title, message) => {
        calls.confirmations.push({ title, message });
        return confirmations.shift() ?? false;
      },
      notify: (message, level) => calls.notifications.push({ message, level }),
      setStatus: (key, value) => calls.statuses.push({ key, value }),
      setTitle: (value) => calls.titles.push(value),
      setWidget: (key, value) => calls.widgets.push({ key, value }),
      select: async (title, options) => {
        calls.selections.push({ title, options });
        return selections ? selections.shift() : overrides.selection;
      },
      editor: async (title, prefill) => {
        calls.editors.push({ title, prefill });
        return editors ? editors.shift() : overrides.editor;
      },
      input: async (title, placeholder) => {
        calls.inputs.push({ title, placeholder });
        return inputs ? inputs.shift() : overrides.input;
      },
      custom: async () => {
        if (overrides.customError) throw overrides.customError;
        return overrides.customResult;
      },
      theme: { fg: (_color, text) => text },
    },
    ...overrides.context,
  };
}

test("registers one bounded model tool and the exact short-only command surface", () => {
  const { tool, tools, commands, shortcuts, events } = harness(async () => ({ code: 0, stdout: "" }));
  assert.equal(tools.length, 1);
  assert.equal(tool.name, "tmux_orchestrator");
  assert.deepEqual(tool.parameters.properties.action.enum, ["doctor", "models", "list", "status", "watch", "attach", "start", "send"]);
  assert.equal(tool.parameters.properties.action.enum.includes("restart"), false);
  assert.equal(tool.parameters.properties.action.enum.includes("stop"), false);
  assert.equal(tool.parameters.properties.profile.pattern, "^[a-z][a-z0-9-]{0,31}$");
  assert.deepEqual(tool.parameters.properties.implementationFlow.enum, ["single", "phased"]);
  assert.equal(tool.parameters.properties.workspaceCapsule.type, "boolean");
  assert.equal(tool.parameters.properties.workspaceRelevantPaths.maxItems, 16);
  assert.equal(tool.parameters.properties.workspaceRelevantPaths.uniqueItems, true);
  assert.equal(tool.parameters.properties.workspaceRelevantPaths.items.maxLength, 256);
  assert.equal(tool.parameters.properties.forceSpecialists.uniqueItems, true);
  assert.deepEqual(tool.parameters.properties.forceSpecialists.items.enum, ["probe", "playwright", "django"]);
  assert.equal(tool.parameters.properties.budgetOverrides.additionalProperties, false);
  assert.equal(tool.parameters.properties.workerSkills.additionalProperties, false);
  assert.equal(tool.parameters.properties.workerSkills.properties.reviewer.maxItems, 8);
  assert.deepEqual(
    Object.keys(tool.parameters.properties.budgetOverrides.properties),
    ["enforcement", "warning", "hard"],
  );
  assert.equal(
    Object.hasOwn(tool.parameters.properties.budgetOverrides.properties, "apiKey"),
    false,
  );
  const budgetRun = tool.parameters.properties.budgetOverrides.properties.warning
    .properties.run;
  assert.equal(budgetRun.additionalProperties, false);
  assert.equal(Object.hasOwn(budgetRun.properties, "endpoint"), false);
  assert.equal(tool.renderCall, undefined);
  assert.equal(tool.renderResult, undefined);
  assert.match(
    tool.promptGuidelines.join(" "),
    /synthesize a bounded contextCapsule.*never the full transcript/,
  );
  assert.match(
    tool.promptGuidelines.join(" "),
    /workspaceCapsule only for an explicit cold-assignment experiment.*never a repository tree/,
  );
  assert.match(
    tool.promptGuidelines.join(" "),
    /Do not claim workspace-capsule savings or correctness without authoritative provider and review evidence/,
  );
  assert.match(
    tool.promptGuidelines.join(" "),
    /Once watching, end the turn.*never run sleep commands.*poll status\/tmux/,
  );
  assert.match(
    tool.promptGuidelines.join(" "),
    /Worker skill discovery is disabled.*exact Markdown paths.*explicitly reviewed/,
  );
  assert.deepEqual(
    [...commands.keys()],
    ["or-models", "or-dashboard", "or-start", "or-send", "or-stop"],
  );
  for (const removed of ["help", "about", "doctor", "list", "status", "watch", "attach"]) {
    assert.equal(commands.has(`or-${removed}`), false);
  }
  assert.equal([...commands.keys()].some((name) => name.startsWith("orchestrator-")), false);
  assert.equal(commands.has("orchestrate"), false);
  assert.equal(commands.has("orchestrations"), false);
  assert.equal(shortcuts.get("ctrl+shift+g").handler instanceof Function, true);
  assert.equal(events.has("session_start"), true);
  assert.ok(events.has("session_before_switch"));
  assert.ok(events.has("session_before_fork"));
  assert.equal(events.has("session_shutdown"), true);
});

function dashboardSnapshot() {
  return {
    list: success("list", {
      sessions: [
        {
          session: "pi-alpha-agents",
          valid: true,
          project: "/work/alpha",
          execution_profile: { name: "balanced", kind: "packaged", source: "project" },
          roles: [{ name: "implementer" }, { name: "reviewer" }],
          dashboard: {
            available: true,
            workflow: { state: "active", round: 2, implementation_flow: "phased" },
            usage: {
              provider_calls: 7,
              operational_tokens: 12500,
              cost_total: 0.2345,
              context_percent: 42.25,
              actual_provider_usage_only: true,
            },
            roles: { total: 2, connected: 2 },
          },
        },
        {
          session: "pi-beta-agents",
          valid: true,
          project: "/work/beta",
          execution_profile: { name: "economy", kind: "packaged", source: "per-run" },
          roles: [{ name: "implementer" }, { name: "reviewer" }],
          dashboard: { available: false, reason: "temporarily-unavailable" },
        },
        { session: "invalid", valid: false, project: null },
      ],
    }),
    about: {
      currentVersion: packageJson.version,
      latestVersion: packageJson.version,
      updateAvailable: false,
      npmUrl: "https://www.npmjs.com/package/pi-tmux-orchestrator",
      repositoryUrl: "https://github.com/revazi/pi-tmux-orchestrator",
      issuesUrl: "https://github.com/revazi/pi-tmux-orchestrator/issues",
      updateCommand: "pi update npm:pi-tmux-orchestrator",
    },
    doctor: success("doctor", {
      commands: [
        { name: "pi", status: "ok" },
        { name: "tmux", status: "ok" },
        { name: "python3", status: "ok" },
      ],
      tmux: { version: "tmux 3.5" },
      model_checks: [{ role: "implementer", available: true }],
      model_policy: {
        config_path: "/external/tmux-orchestrator.json",
        execution_profile: { name: "balanced", source: "project" },
        project_config: { matched: true, directory: "/work/alpha" },
      },
      budget_policy: { effective: { enforcement: "warn-only" } },
    }),
  };
}

const dashboardTheme = {
  bold: (text) => text,
  fg: (_color, text) => text,
  bg: (_color, text) => text,
};

test("dashboard display is bounded and keeps unavailable usage explicit", () => {
  const display = dashboardHooks.dashboardDisplay(dashboardSnapshot());
  assert.equal(display.sessions.length, 2);
  assert.equal(display.error, undefined);
  const lines = dashboardHooks.dashboardPlainLines(dashboardSnapshot());
  assert.ok(lines.length <= 40);
  assert.match(lines.join("\n"), /7 calls · 12\.5k tok · \$0\.234 · ctx 42\.3%/);
  assert.match(lines.join("\n"), /usage unavailable/);
  assert.match(lines.join("\n"), new RegExp(`About: v${packageJson.version}`));
  assert.match(lines.join("\n"), /Repository: https:\/\/github\.com\/revazi\/pi-tmux-orchestrator/);
  assert.match(lines.join("\n"), /Issues: https:\/\/github\.com\/revazi\/pi-tmux-orchestrator\/issues/);
  assert.match(lines.join("\n"), /NPM: https:\/\/www\.npmjs\.com\/package\/pi-tmux-orchestrator/);
  assert.match(lines.join("\n"), /Contribute: Ideas, issues, and PRs are welcome\./);
  const empty = dashboardSnapshot();
  empty.list.data.sessions = [];
  assert.match(dashboardHooks.dashboardPlainLines(empty).join("\n"), /none running/);

  const warning = dashboardHooks.doctorDisplay({
    ...dashboardSnapshot().doctor,
    success: false,
    data: {
      ...dashboardSnapshot().doctor.data,
      commands: [{ name: "tmux", status: "fail" }],
      model_checks: [{ role: "reviewer", available: false }],
    },
  });
  assert.equal(warning.status, "warning");
  assert.match(warning.headline, /issues/);
});

test("dashboard loads doctor only on demand, shows help, refreshes sessions, and selects actions", async () => {
  let listLoads = 0;
  let doctorLoads = 0;
  let aboutLoads = 0;
  let selected;
  let renders = 0;
  const overlay = new OrchestrationDashboardOverlay(
    dashboardTheme,
    (value) => { selected = value; },
    () => { renders += 1; },
    async () => { listLoads += 1; return dashboardSnapshot().list; },
    async () => { doctorLoads += 1; return dashboardSnapshot().doctor; },
    async () => { aboutLoads += 1; return dashboardSnapshot().about; },
    3,
    { matches: () => false },
  );
  assert.match(overlay.render(100).join("\n"), /Loading running orchestrations/);
  await overlay.refresh();
  assert.equal(listLoads, 1);
  assert.equal(doctorLoads, 0);
  await waitFor(() => aboutLoads === 1);
  assert.ok(renders >= 2);
  const lines = overlay.render(100);
  assert.ok(lines.every((line) => dashboardHooks.visibleWidth(line) <= 100));
  assert.match(lines.join("\n"), /Orchestration Dashboard/);
  assert.doesNotMatch(lines.join("\n"), /Doctor passed|Running doctor/);
  assert.equal(dashboardHooks.visibleWidth(overlay.render(3)[0]), 3);

  overlay.handleInput("?");
  assert.match(overlay.render(100).join("\n"), /\/or-start.*\/or-models.*\/or-send.*\/or-stop/);
  overlay.handleInput("d");
  await waitFor(() => doctorLoads === 1 && overlay.doctorLoading === false);
  assert.match(overlay.render(100).join("\n"), /Doctor passed/);
  overlay.handleInput("r");
  await waitFor(() => listLoads === 2);
  assert.equal(doctorLoads, 1);
  assert.equal(aboutLoads, 1);
  overlay.handleInput("x");
  assert.deepEqual(selected, { type: "stop", session: "pi-alpha-agents" });
  overlay.handleInput("j");
  overlay.handleInput("\r");
  assert.deepEqual(selected, { type: "attach", session: "pi-beta-agents" });

  const aboutFooter = overlay.render(100).join("\n");
  assert.match(aboutFooter, new RegExp(`About.*v${packageJson.version}`));
  assert.match(aboutFooter, /Repository.*github\.com\/revazi\/pi-tmux-orchestrator/);
  assert.match(aboutFooter, /Issues.*pi-tmux-orchestrator\/issues/);
  assert.match(aboutFooter, /NPM.*npmjs\.com\/package\/pi-tmux-orchestrator/);
  assert.match(aboutFooter, /Contribute.*Ideas, issues, and PRs are welcome/);
  assert.ok(aboutFooter.indexOf("Contribute") > aboutFooter.indexOf("NPM"));
  overlay.dispose();
});

test("dashboard remains within a short terminal row budget for doctor and help", async () => {
  const overlay = new OrchestrationDashboardOverlay(
    dashboardTheme,
    () => {},
    () => {},
    async () => dashboardSnapshot().list,
    async () => dashboardSnapshot().doctor,
    async () => dashboardSnapshot().about,
    10,
    { matches: () => false },
    18,
  );

  await overlay.refresh();
  overlay.handleInput("d");
  await waitFor(() => overlay.doctorLoading === false);
  assert.ok(overlay.render(100).length <= 18);
  overlay.handleInput("?");
  const help = overlay.render(100).join("\n");
  assert.ok(overlay.render(100).length <= 18);
  assert.match(help, /Enter watches and attaches/);
  assert.doesNotMatch(help, /Doctor passed/);
  overlay.dispose();
});

test("dashboard sessions become usable while an explicitly requested doctor remains pending", async () => {
  let releaseDoctor;
  let doctorLoads = 0;
  let selected;
  const pendingDoctor = new Promise((resolve) => { releaseDoctor = resolve; });
  const overlay = new OrchestrationDashboardOverlay(
    dashboardTheme,
    (value) => { selected = value; },
    () => {},
    async () => dashboardSnapshot().list,
    () => { doctorLoads += 1; return pendingDoctor; },
    async () => dashboardSnapshot().about,
    3,
    { matches: () => false },
  );

  await overlay.refresh();
  assert.equal(doctorLoads, 0);
  overlay.handleInput("d");
  await waitFor(() => doctorLoads === 1);
  await waitFor(() => overlay.sessions.length === 2 && overlay.loading === false);
  const partial = overlay.render(100).join("\n");
  assert.match(partial, /pi-alpha-agents/);
  assert.match(partial, /2 running/);
  assert.doesNotMatch(partial, /refreshing/);
  assert.match(partial, /Running doctor/);
  overlay.handleInput("j");
  overlay.handleInput("\r");
  assert.deepEqual(selected, { type: "attach", session: "pi-beta-agents" });

  releaseDoctor(dashboardSnapshot().doctor);
  await waitFor(() => overlay.doctorLoading === false);
  assert.match(overlay.render(100).join("\n"), /Doctor passed/);
  overlay.dispose();
});

test("dashboard command has a bounded non-TUI fallback", async () => {
  const calls = [];
  const { commands } = harness(async (_command, args) => {
    calls.push(args);
    if (args[2] === "list") return { code: 0, stdout: JSON.stringify(dashboardSnapshot().list) };
    if (args[2] === "doctor") return { code: 0, stdout: JSON.stringify(dashboardSnapshot().doctor) };
    throw new Error("unexpected action");
  });
  const ctx = context({ context: { mode: "rpc", hasUI: true } });
  await commands.get("or-dashboard").handler("", ctx);
  assert.deepEqual(calls.map((args) => args[2]), ["list"]);
  assert.equal(ctx.calls.notifications.length, 1);
  assert.match(ctx.calls.notifications[0].message, /Orchestrations: 2/);
});

test("shows a non-blocking update notice once and reuses cached version details in the dashboard", async () => {
  const originalFetch = globalThis.fetch;
  const originalDisable = process.env.PI_TMUX_ORCHESTRATOR_DISABLE_UPDATE_NOTICE;
  const originalRole = process.env.PI_TMUX_ORCHESTRATOR_ROLE;
  const originalController = process.env.PI_TMUX_CONTROLLER;
  let fetchCalls = 0;
  globalThis.fetch = async () => {
    fetchCalls += 1;
    return { ok: true, async json() { return { version: "99.0.0" }; } };
  };
  delete process.env.PI_TMUX_ORCHESTRATOR_DISABLE_UPDATE_NOTICE;
  delete process.env.PI_TMUX_ORCHESTRATOR_ROLE;
  delete process.env.PI_TMUX_CONTROLLER;
  updateHooks.reset();

  try {
    const { commands, events } = harness(async (_command, args) => {
      if (args[2] === "list") {
        return { code: 0, stdout: JSON.stringify(success("list", { sessions: [] })) };
      }
      throw new Error("unexpected action");
    });
    const ctx = context();
    events.get("session_start")({}, ctx);
    await waitFor(() => ctx.calls.notifications.length === 1);
    assert.deepEqual(ctx.calls.notifications, [{
      message: `Pi Tmux Orchestrator 99.0.0 is available (you have ${packageJson.version}). Update: pi update npm:pi-tmux-orchestrator. Details: /or-dashboard`,
      level: "warning",
    }]);

    events.get("session_start")({}, ctx);
    await drainPromises();
    assert.equal(ctx.calls.notifications.length, 1);

    const aboutCtx = context({ context: { mode: "rpc", hasUI: true } });
    await commands.get("or-dashboard").handler("", aboutCtx);
    assert.equal(fetchCalls, 1);
    assert.equal(aboutCtx.calls.notifications.length, 1);
    assert.match(aboutCtx.calls.notifications[0].message, new RegExp(`About: v${packageJson.version} · update 99\\.0\\.0 available`));
    assert.match(aboutCtx.calls.notifications[0].message, /pi update npm:pi-tmux-orchestrator/);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalDisable === undefined) delete process.env.PI_TMUX_ORCHESTRATOR_DISABLE_UPDATE_NOTICE;
    else process.env.PI_TMUX_ORCHESTRATOR_DISABLE_UPDATE_NOTICE = originalDisable;
    if (originalRole === undefined) delete process.env.PI_TMUX_ORCHESTRATOR_ROLE;
    else process.env.PI_TMUX_ORCHESTRATOR_ROLE = originalRole;
    if (originalController === undefined) delete process.env.PI_TMUX_CONTROLLER;
    else process.env.PI_TMUX_CONTROLLER = originalController;
    updateHooks.reset();
  }
});

test("startup update notices honor opt-out and skip orchestration worker sessions", async () => {
  const originalFetch = globalThis.fetch;
  const originalDisable = process.env.PI_TMUX_ORCHESTRATOR_DISABLE_UPDATE_NOTICE;
  const originalRole = process.env.PI_TMUX_ORCHESTRATOR_ROLE;
  let fetchCalls = 0;
  globalThis.fetch = async () => {
    fetchCalls += 1;
    return { ok: true, async json() { return { version: "99.0.0" }; } };
  };

  try {
    const { events } = harness(async () => ({ code: 0, stdout: "" }));
    process.env.PI_TMUX_ORCHESTRATOR_DISABLE_UPDATE_NOTICE = "1";
    updateHooks.reset();
    const disabledCtx = context();
    events.get("session_start")({}, disabledCtx);
    await drainPromises();
    assert.equal(disabledCtx.calls.notifications.length, 0);

    delete process.env.PI_TMUX_ORCHESTRATOR_DISABLE_UPDATE_NOTICE;
    process.env.PI_TMUX_ORCHESTRATOR_ROLE = "reviewer";
    updateHooks.reset();
    const workerCtx = context();
    events.get("session_start")({}, workerCtx);
    await drainPromises();
    assert.equal(workerCtx.calls.notifications.length, 0);
    assert.equal(fetchCalls, 0);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalDisable === undefined) delete process.env.PI_TMUX_ORCHESTRATOR_DISABLE_UPDATE_NOTICE;
    else process.env.PI_TMUX_ORCHESTRATOR_DISABLE_UPDATE_NOTICE = originalDisable;
    if (originalRole === undefined) delete process.env.PI_TMUX_ORCHESTRATOR_ROLE;
    else process.env.PI_TMUX_ORCHESTRATOR_ROLE = originalRole;
    updateHooks.reset();
  }
});

test("worker report schemas expose only fields valid for each role", () => {
  const implementer = workerHooks.reportParameters("implementer");
  assert.deepEqual(implementer.properties.kind.enum, ["plan", "implementation"]);
  assert.ok(implementer.properties.changed_paths);
  assert.equal(implementer.properties.relevant_paths.maxItems, 12);
  assert.equal(implementer.properties.relevant_symbols.items.maxLength, 300);
  assert.equal(implementer.properties.intended_changes.maxItems, 12);
  assert.equal(implementer.properties.required_checks.maxItems, 12);
  assert.equal(implementer.properties.open_questions.maxItems, 12);
  assert.equal(implementer.properties.verdict, undefined);
  assert.deepEqual(implementer.required, ["kind", "summary"]);

  const reviewer = workerHooks.reportParameters("reviewer");
  assert.deepEqual(reviewer.properties.kind.enum, ["review"]);
  assert.equal(reviewer.properties.changed_paths, undefined);
  assert.deepEqual(reviewer.properties.verdict.enum, ["approved", "changes_requested"]);
  assert.deepEqual(reviewer.required, ["kind", "summary", "verdict"]);
});

test("implementer plan reports and active tools are strictly read-only and bounded", () => {
  const input = {
    kind: "plan",
    summary: "The change surface is bounded.",
    relevant_paths: ["src/service.js", "tests/service.test.js"],
    relevant_symbols: ["runService", "service failure test"],
    intended_changes: ["Guard the transition before committing state."],
    required_checks: ["Run the focused service tests."],
    risks: ["Preserve retry behavior."],
    open_questions: [],
  };
  const report = workerHooks.normalizeReport(input, "plan", "implementer");
  assert.equal(report.kind, "plan");
  assert.deepEqual(report.changed_paths, []);
  assert.deepEqual(report.checks, []);
  assert.deepEqual(report.findings, []);
  assert.equal(report.verdict, null);
  assert.deepEqual(report.relevant_paths, input.relevant_paths);

  for (const invalid of [
    { ...input, changed_paths: ["src/service.js"] },
    { ...input, verdict: "approved" },
    { ...input, checks: [{ name: "unit", status: "passed" }] },
    { ...input, summary: "x".repeat(1001) },
    { ...input, relevant_paths: ["../secret"] },
    { ...input, open_questions: Array(13).fill("question") },
  ]) {
    assert.throws(
      () => workerHooks.normalizeReport(invalid, "plan", "implementer"),
      /invalid_/,
    );
  }
  assert.throws(
    () => workerHooks.normalizeReport(input, "implementation", "implementer"),
    /invalid_report_kind/,
  );
  assert.throws(
    () => workerHooks.normalizeReport(input, "plan", "reviewer"),
    /invalid_report_kind/,
  );

  const normalTools = ["read", "bash", "edit", "write", "grep", "find", "ls", "orchestrator_report"];
  assert.deepEqual(
    workerHooks.assignmentToolNames(normalTools, "implementer", "plan"),
    ["read", "bash", "grep", "find", "ls", "orchestrator_report"],
  );
  assert.deepEqual(
    workerHooks.assignmentToolNames(normalTools, "implementer", "implementation"),
    normalTools,
  );
  assert.equal(workerHooks.planToolDecision({ kind: "plan" }, "implementer", "read"), undefined);
  assert.match(
    workerHooks.planToolDecision({ kind: "plan" }, "implementer", "edit").reason,
    /read-only/,
  );
  assert.equal(
    workerHooks.planToolDecision({ kind: "implementation" }, "implementer", "edit"),
    undefined,
  );

  const restored = workerHooks.restoreWorkerState([{
    type: "custom",
    customType: "pi-tmux-orchestrator-delivery-v1",
    data: {
      delivery_id: "a".repeat(32),
      kind: "assignment",
      assignment_id: "b".repeat(32),
      assignment_kind: "plan",
      round: 2,
    },
  }]);
  assert.equal(restored.activeAssignment.kind, "plan");
  assert.deepEqual(
    workerHooks.assignmentToolNames(normalTools, "implementer", restored.activeAssignment.kind),
    ["read", "bash", "grep", "find", "ls", "orchestrator_report"],
  );
});

test("worker guardrail policy is strict and preserves supported assignment thresholds", () => {
  const policy = workerHooks.parseGuardrailPolicy(JSON.stringify({
    enforcement: "hard",
    warning: { provider_calls: 4, context_percent: 70 },
    hard: { provider_calls: 6, context_percent: 85 },
  }));
  assert.deepEqual(policy, {
    enforcement: "hard",
    warning: { provider_calls: 4, context_percent: 70 },
    hard: { provider_calls: 6, context_percent: 85 },
  });
  assert.equal(workerHooks.parseGuardrailPolicy("{}"), undefined);
  assert.equal(workerHooks.parseGuardrailPolicy(JSON.stringify({
    enforcement: "hard", warning: {}, hard: { cache_read_tokens: 10 },
  })), undefined);
  assert.equal(workerHooks.parseGuardrailPolicy(JSON.stringify({
    enforcement: "hard", warning: { provider_calls: 7 }, hard: { provider_calls: 6 },
  })), undefined);
  assert.deepEqual(workerHooks.hardGuardrailThresholds({
    enforcement: "warn-only", hard: { provider_calls: 6 },
  }), { provider_calls: 6 });
});

test("worker counts provider turns from the assignment boundary and observes context pressure", () => {
  const entries = [
    {
      type: "message",
      message: { role: "assistant", usage: { input: 10, output: 2, cacheRead: 3, cacheWrite: 0, cost: { total: 0.01 } } },
    },
    {
      type: "message",
      message: { role: "assistant", usage: { input: 20, output: 4, cacheRead: 6, cacheWrite: 1, cost: { total: 0.02 } } },
    },
  ];
  const ctx = {
    sessionManager: { getEntries: () => entries },
    getContextUsage: () => ({ tokens: 850, contextWindow: 1_000, percent: 85 }),
  };
  assert.deepEqual(workerHooks.guardrailObservations(ctx, {
    providerCalls: 1,
    input: 10,
    output: 2,
    cacheRead: 3,
    cacheWrite: 0,
    cost: { total: 0.01 },
  }), {
    provider_calls: 1,
    context_tokens: 850,
    context_percent: 85,
  });
  assert.deepEqual(
    workerHooks.firstGuardrailFinding(
      { provider_calls: 2, context_percent: 80 },
      { provider_calls: 1, context_tokens: 850, context_percent: 85 },
    ),
    { metric: "context_percent", observed: 85, threshold: 80 },
  );
});

test("assignment guardrails remain observational for every parallel tool and final report", () => {
  const tools = ["read", "bash", "edit", "write", "unknown_tool", "orchestrator_report"];
  const decisions = tools.map(() => workerHooks.observationalGuardrailDecision());
  assert.deepEqual(decisions, Array(tools.length).fill(undefined));
});

test("assignment guardrail warning is bounded and restart state prevents duplicate warning or hard facts", () => {
  const assignmentId = "d".repeat(32);
  const finding = { metric: "context_percent", observed: 75, threshold: 70 };
  const content = workerHooks.appendGuardrailWarning([{ type: "text", text: "tool output" }], finding);
  assert.equal(content.length, 2);
  assert.match(content[1].text, /Assignment guardrail warning/);
  assert.ok(content[1].text.length < 300);
  const restored = workerHooks.restoreGuardrailState([
    {
      type: "custom",
      customType: "pi-tmux-orchestrator-guardrail-v1",
      data: { assignment_id: assignmentId, level: "warning", status: "triggered", ...finding },
    },
    {
      type: "custom",
      customType: "pi-tmux-orchestrator-guardrail-v1",
      data: { assignment_id: assignmentId, level: "warning", status: "delivered" },
    },
    {
      type: "custom",
      customType: "pi-tmux-orchestrator-guardrail-v1",
      data: {
        assignment_id: assignmentId,
        level: "hard",
        status: "triggered",
        metric: "provider_calls",
        observed: 6,
        threshold: 6,
      },
    },
  ], assignmentId);
  assert.deepEqual(restored, {
    warning: finding,
    hard: { metric: "provider_calls", observed: 6, threshold: 6 },
    warningDelivered: true,
  });
});

test("actual Pi prompt options keep context, explicit skills, and read-only tools while reducing serialized overhead", async () => {
  const checkedIn = JSON.parse(
    await readFile(new URL("fixtures/worker-prompt-baseline.json", import.meta.url), "utf8"),
  );
  const measured = await buildWorkerPromptBaselineIfAvailable();
  if (measured) assert.deepEqual(measured, checkedIn);
  assert.equal(checkedIn.metric_scope, "model-free-built-worker-system-prompt");
  assert.equal(checkedIn.after.skill_discovery, false);
  assert.deepEqual(checkedIn.after.loaded_skills, ["opted"]);
  assert.ok(checkedIn.after.characters < checkedIn.before.characters);
  assert.match(checkedIn.caveat, /not provider tokens, billing, cache efficiency/);
});

test("worker queues baseline context before a triggered assignment turn", () => {
  assert.deepEqual(workerHooks.deliveryOptions(false), {
    triggerTurn: false,
    deliverAs: "followUp",
  });
  assert.deepEqual(workerHooks.deliveryOptions(true), {
    triggerTurn: true,
    deliverAs: "followUp",
  });
});

test("bounded parent context capsules are structured without copying a transcript", () => {
  const capsule = testHooks.renderContextCapsule({
    currentState: "A focused branch already contains reviewed scaffolding.",
    decisions: ["Keep broker-v1 as the only workflow transport."],
    constraints: ["Do not persist task or report bodies in SQLite."],
    acceptanceCriteria: ["Synthetic two-round context shrinks by at least 50%."],
    relevantPaths: ["extensions/orchestrator-worker.js"],
    knownEvidence: ["Current worker sessions exceed the soft context budget."],
    openQuestions: ["None."],
    outOfScope: ["Copying the parent transcript."],
  });
  assert.match(capsule, /### Current state/);
  assert.match(capsule, /### Decisions already made/);
  assert.match(capsule, /at least 50%/);
  assert.doesNotMatch(capsule, /parent transcript\.\n.*parent transcript/s);
  assert.ok(Buffer.byteLength(capsule, "utf8") <= 12 * 1024);
  assert.throws(
    () => testHooks.renderContextCapsule({ currentState: "x".repeat(3001) }),
    /invalid_context_capsule_current_state/,
  );
  assert.throws(
    () => testHooks.renderContextCapsule({ decisions: ["x".repeat(500), "y".repeat(500), ...Array(20).fill("z".repeat(500))] }),
    /invalid_context_capsule_decisions/,
  );
  assert.throws(
    () => testHooks.renderContextCapsule({
      currentState: "s".repeat(3_000),
      decisions: Array(12).fill("d".repeat(500)),
      constraints: Array(12).fill("c".repeat(500)),
    }),
    /context_capsule_too_large/,
  );
  assert.throws(
    () => testHooks.renderContextCapsule({ currentState: "valid", transcript: "not allowed" }),
    /invalid_context_capsule_field/,
  );
});

test("worker read and grep inputs receive orchestration-only defaults and hard caps", () => {
  const readDefault = { toolName: "read", input: { path: "large.txt" } };
  const readOversized = { toolName: "read", input: { path: "large.txt", limit: 20_000 } };
  const grepDefault = { toolName: "grep", input: { pattern: "needle" } };
  const grepOversized = {
    toolName: "grep",
    input: { pattern: "needle", limit: 5_000, context: 500 },
  };

  assert.deepEqual(applyToolInputPolicy(readDefault), {
    input_capped: true,
    requested_limit: null,
    effective_limit: 400,
  });
  assert.equal(readDefault.input.limit, RESULT_POLICY.read.maxLines);
  assert.equal(applyToolInputPolicy(readOversized).requested_limit, 20_000);
  assert.equal(readOversized.input.limit, RESULT_POLICY.read.maxLines);
  assert.deepEqual(applyToolInputPolicy(grepDefault), {
    input_capped: true,
    requested_limit: null,
    effective_limit: 40,
    requested_context: 0,
    effective_context: 0,
  });
  assert.equal(grepDefault.input.limit, RESULT_POLICY.grep.maxMatches);
  const grepPolicy = applyToolInputPolicy(grepOversized);
  assert.equal(grepPolicy.requested_limit, 5_000);
  assert.equal(grepPolicy.requested_context, 500);
  assert.equal(grepOversized.input.limit, RESULT_POLICY.grep.maxMatches);
  assert.equal(grepOversized.input.context, RESULT_POLICY.grep.maxContext);
  assert.equal(applyToolInputPolicy({ toolName: "edit", input: {} }), undefined);
});

test("worker read and grep results are UTF-8 bounded with actionable continuation", async () => {
  const source = Array.from(
    { length: 1_200 }, (_, index) => `${index + 1}: λ😀${"x".repeat(80)}`,
  ).join("\n");
  const readEvent = {
    toolName: "read",
    input: { path: "large.txt", offset: 101, limit: 400 },
    content: [{ type: "text", text: source }],
  };
  const readResult = await applyToolResultPolicy(readEvent, {
    input_capped: true,
    requested_limit: 1_000,
    effective_limit: 400,
  });
  const readText = readResult.content[0].text;
  assert.equal(Buffer.from(readText, "utf8").toString("utf8"), readText);
  assert.ok(Buffer.byteLength(readText, "utf8") <= RESULT_POLICY.read.maxBytes);
  assert.ok(readText.split("\n").length <= RESULT_POLICY.read.maxLines);
  assert.match(readText, /read offset=\d+ and limit<=400/);
  assert.equal(readResult.observation.truncated, true);
  assert.equal(readResult.observation.source_lines, 1_200);
  assert.equal(readResult.observation.input_capped, true);
  assert.deepEqual(Object.keys(readResult.observation), [
    "schema_version", "event", "tool", "truncated", "direction",
    "source_bytes", "source_lines", "emitted_bytes", "emitted_lines",
    "input_capped", "requested_limit", "effective_limit",
    "requested_context", "effective_context",
  ]);
  assert.equal(JSON.stringify(readResult.observation).includes("large.txt"), false);
  assert.equal(JSON.stringify(readResult.observation).includes("λ😀"), false);
  assert.equal(readResult.details.truncation.maxBytes, RESULT_POLICY.read.maxBytes);
  assert.equal(readResult.details.truncation.maxLines, RESULT_POLICY.read.maxLines);
  assert.equal(readResult.details.truncation.content.includes("λ😀"), true);

  const grepResult = await applyToolResultPolicy({
    toolName: "grep",
    input: { pattern: "needle", path: "src", limit: 40, context: 2 },
    content: [{ type: "text", text: source }],
  });
  const grepText = grepResult.content[0].text;
  assert.ok(Buffer.byteLength(grepText, "utf8") <= RESULT_POLICY.grep.maxBytes);
  assert.ok(grepText.split("\n").length <= RESULT_POLICY.grep.maxLines);
  assert.match(grepText, /Refine grep pattern\/path\/glob/);
  assert.match(grepText, /matches are capped at 40 with context<=2/);
});

test("worker bash results retain failures and write mode-0600 full output", async () => {
  const lines = Array.from({ length: 1_000 }, (_, index) => `progress ${index + 1} ${"z".repeat(80)}`);
  lines[500] = "FAILED safety-critical assertion: authorization boundary regressed";
  lines[999] = "test command exited 1";
  const source = lines.join("\n");
  const result = await applyToolResultPolicy({
    toolName: "bash",
    input: { command: "synthetic test" },
    content: [{ type: "text", text: source }],
    details: undefined,
  });
  const output = result.content[0].text;
  const fullOutputPath = result.details.fullOutputPath;
  try {
    assert.ok(Buffer.byteLength(output, "utf8") <= RESULT_POLICY.bash.maxBytes);
    assert.ok(output.split("\n").length <= RESULT_POLICY.bash.maxLines);
    assert.match(output, /FAILED safety-critical assertion/);
    assert.match(output, /test command exited 1/);
    assert.match(output, /bounded beginning, failure diagnostics, and ending/);
    assert.equal(await readFile(fullOutputPath, "utf8"), source);
    assert.equal((await stat(fullOutputPath)).mode & 0o777, 0o600);
  } finally {
    await rm(dirname(fullOutputPath), { recursive: true, force: true });
  }
});

test("worker result metadata records immediate pagination and refined searches without bodies", () => {
  const pagination = immediateFollowupObservation(
    { tool: "read", input: { path: "large.txt", offset: 1 } },
    { toolName: "read", input: { path: "large.txt", offset: 401 } },
  );
  assert.deepEqual(pagination, {
    schema_version: 1,
    event: "immediate_followup",
    previous_tool: "read",
    next_tool: "read",
    same_read_target: true,
    read_pagination: true,
    refined_grep: false,
  });
  const refinement = immediateFollowupObservation(
    { tool: "grep", input: { pattern: "broad", path: "src" } },
    { toolName: "grep", input: { pattern: "specific", path: "src" } },
  );
  assert.equal(refinement.refined_grep, true);
  assert.equal(JSON.stringify(refinement).includes("broad"), false);
  assert.equal(JSON.stringify(refinement).includes("specific"), false);
});

test("completed assignment pruning cuts synthetic two-round provider context by at least half", (t) => {
  const custom = (details, content) => ({
    role: "custom",
    customType: "pi-tmux-orchestrator-message-v1",
    content,
    details,
  });
  const messages = [
    custom({ kind: "context", delivery_kind: "baseline", round: 1 }, "bounded baseline"),
    custom({ kind: "assignment", assignment_id: "a".repeat(32), assignment_kind: "implementation", round: 1 }, "round one"),
    { role: "assistant", content: [{ type: "text", text: "r".repeat(45_000) }] },
    { role: "toolResult", content: [{ type: "text", text: "t".repeat(45_000) }] },
    { role: "user", content: [{ type: "text", text: "direct user steering remains" }] },
    custom({ kind: "context", delivery_kind: "run_state", round: 1 }, "old run state"),
    custom({ kind: "context", delivery_kind: "run_state", round: 2 }, "latest run state"),
    custom({ kind: "assignment", assignment_id: "b".repeat(32), assignment_kind: "implementation", round: 2 }, "round two"),
    { role: "assistant", content: [{ type: "text", text: "c".repeat(8_000) }] },
  ];
  const serializedCharacters = (items) => JSON.stringify(items).length;
  const originalLength = messages.length;
  const filtered = workerHooks.filterWorkerContext(messages, { id: "b".repeat(32) });
  const before = serializedCharacters(messages);
  const after = serializedCharacters(filtered);
  const reduction = 1 - (after / before);
  assert.equal(before, 99_170);
  assert.equal(after, 8_678);
  t.diagnostic(`synthetic serialized context characters: before=${before}, after=${after}, reduction=${(reduction * 100).toFixed(1)}%`);
  assert.ok(reduction >= 0.5, `expected >=50% reduction, before=${before}, after=${after}`);
  assert.equal(filtered.some((item) => item.content === "bounded baseline"), true);
  assert.equal(filtered.some((item) => item.content === "old run state"), false);
  assert.equal(filtered.some((item) => item.content === "latest run state"), true);
  assert.equal(filtered.some((item) => item.content === "round one"), false);
  assert.equal(filtered.some((item) => item.content === "round two"), true);
  assert.equal(filtered.some((item) => item.role === "toolResult"), false);
  assert.equal(filtered.some((item) => item.role === "user"), true);
  assert.equal(filtered.some((item) => (
    item.role === "assistant" && item.content[0].text === "c".repeat(8_000)
  )), true);
  assert.equal(messages.length, originalLength);
  assert.equal(messages.some((item) => item.role === "toolResult"), true);
});

const FIRST_ASSIGNMENT = "a".repeat(32);
const PRIOR_ASSIGNMENT_TURNS = [
  "analysis turn one", "tool result one", "analysis turn two", "tool result two",
  "recovered assignment turn", "accepted report",
];

function workerMessage(details, content) {
  return {
    role: "custom",
    customType: "pi-tmux-orchestrator-message-v1",
    content,
    details,
  };
}

function completedAssignmentHistory() {
  return [
    workerMessage({ kind: "context", delivery_kind: "baseline", round: 1 }, "bounded baseline"),
    workerMessage({ kind: "assignment", assignment_id: FIRST_ASSIGNMENT, round: 1 }, "first assignment"),
    { role: "assistant", content: [{ type: "text", text: "analysis turn one" }] },
    { role: "toolResult", content: [{ type: "text", text: "tool result one" }] },
    { role: "assistant", content: [{ type: "text", text: "analysis turn two" }] },
    { role: "toolResult", content: [{ type: "text", text: "tool result two" }] },
    workerMessage({ kind: "assignment", assignment_id: FIRST_ASSIGNMENT, round: 1 }, "handover replay"),
    { role: "assistant", content: [{ type: "text", text: "recovered assignment turn" }] },
    {
      role: "toolResult",
      toolName: "orchestrator_report",
      details: { assignment_id: FIRST_ASSIGNMENT },
      content: [{ type: "text", text: "accepted report" }],
    },
  ];
}

test("assistant and tool turns accumulate throughout the current assignment", () => {
  const messages = completedAssignmentHistory();
  for (let end = 3; end <= 6; end += 1) {
    const visible = workerHooks.filterWorkerContext(messages.slice(0, end));
    for (const item of messages.slice(2, end)) {
      assert.equal(visible.includes(item), true, `current-assignment ${item.role} turn was pruned`);
    }
  }
  const completed = workerHooks.filterWorkerContext(messages);
  for (const canary of PRIOR_ASSIGNMENT_TURNS) {
    assert.equal(completed.some((item) => item.content?.[0]?.text === canary), true);
  }
});

test("phased same-round assignment keeps the new assignment active and prunes inspection turns", () => {
  const plan = { id: FIRST_ASSIGNMENT, kind: "plan", round: 1 };
  const implementation = { id: "b".repeat(32), kind: "implementation", round: 1 };
  assert.equal(workerHooks.reportedAssignmentRemainsActive(plan, plan), true);
  assert.equal(workerHooks.reportedAssignmentRemainsActive(implementation, plan), false);
  const result = workerHooks.acceptedReportResult({ kind: "plan" }, plan, "implementer");
  assert.equal(result.terminate, true);
  assert.match(result.content[0].text, /End this turn; do not wait or poll/);

  const messages = completedAssignmentHistory();
  messages.push(
    { role: "user", content: [{ type: "text", text: "direct steering remains" }] },
    workerMessage({ kind: "context", delivery_kind: "run_state", round: 1 }, "accepted bounded plan"),
    workerMessage({ kind: "assignment", assignment_id: implementation.id, round: 1 }, "implement from plan"),
  );
  const visible = workerHooks.filterWorkerContext(messages);
  for (const canary of PRIOR_ASSIGNMENT_TURNS) {
    assert.equal(visible.some((item) => item.content?.[0]?.text === canary), false);
  }
  assert.equal(visible.some((item) => item.content?.[0]?.text === "direct steering remains"), true);
  assert.equal(visible.some((item) => item.content === "accepted bounded plan"), true);
  assert.equal(visible.some((item) => item.content === "implement from plan"), true);
});

test("completed turns are pruned only at the next distinct assignment boundary", () => {
  const messages = completedAssignmentHistory();
  messages.push(
    { role: "user", content: [{ type: "text", text: "direct steering remains" }] },
    workerMessage({ kind: "context", delivery_kind: "run_state", round: 2 }, "latest run state"),
    workerMessage({ kind: "assignment", assignment_id: "b".repeat(32), round: 2 }, "second assignment"),
    { role: "assistant", content: [{ type: "text", text: "second assignment turn" }] },
  );
  const visible = workerHooks.filterWorkerContext(messages);
  for (const canary of PRIOR_ASSIGNMENT_TURNS) {
    assert.equal(visible.some((item) => item.content?.[0]?.text === canary), false);
  }
  assert.equal(visible.some((item) => item.content?.[0]?.text === "direct steering remains"), true);
  assert.equal(visible.some((item) => item.content === "latest run state"), true);
  assert.equal(visible.some((item) => item.content === "second assignment"), true);
  assert.equal(visible.some((item) => item.content?.[0]?.text === "second assignment turn"), true);
  assert.equal(messages.length, 13);
});

test("token-efficiency fixtures expose first-assignment growth and boundary reduction", async () => {
  const baseline = buildTokenEfficiencyBaseline();
  const checkedIn = JSON.parse(
    await readFile(new URL("fixtures/token-efficiency-baseline.json", import.meta.url), "utf8"),
  );
  assert.deepEqual(baseline, checkedIn);
  assert.deepEqual(Object.keys(baseline.fixtures), ["simple", "medium", "multi_round"]);
  assert.ok(
    baseline.fixtures.simple.provider_context.total_utf8_bytes
      > baseline.fixtures.simple.provider_context.total_characters,
    "the fixture must distinguish serialized characters from UTF-8 bytes",
  );

  const mediumCalls = baseline.fixtures.medium.provider_context.by_call;
  assert.equal(baseline.fixtures.medium.provider_calls, 5);
  assert.ok(
    mediumCalls.at(-1).characters > mediumCalls[0].characters * 30,
    "the medium fixture must expose within-assignment context growth",
  );
  assert.equal(baseline.fixtures.medium.tool_results.by_tool.read.count, 3);

  const multiRoundCalls = baseline.fixtures.multi_round.provider_context.by_call;
  const firstRoundPeak = Math.max(...multiRoundCalls.slice(0, 3).map((item) => item.characters));
  const secondRoundStart = multiRoundCalls[3].characters;
  assert.ok(
    secondRoundStart < firstRoundPeak / 10,
    `the assignment boundary must cut context by at least 90% (${firstRoundPeak} -> ${secondRoundStart})`,
  );
  assert.equal(baseline.metric_scope, "model-free-provider-context-proxy");
  assert.match(baseline.caveat, /not provider tokens, billing, or production-wire acceptance/);
});

test("result-volume benchmark reports context reduction and pagination calls", async () => {
  const baseline = await buildResultVolumeBaseline();
  const checkedIn = JSON.parse(
    await readFile(new URL("fixtures/result-volume-baseline.json", import.meta.url), "utf8"),
  );
  assert.deepEqual(baseline, checkedIn);
  assert.deepEqual(Object.keys(baseline.scenarios), ["read", "grep", "bash"]);
  assert.equal(baseline.aggregate.additional_provider_calls, 2);
  assert.equal(baseline.aggregate.context_reduction_percent, 37.9);
  assert.equal(baseline.scenarios.read.additional_provider_calls, 1);
  assert.equal(baseline.scenarios.grep.additional_provider_calls, 1);
  assert.equal(baseline.scenarios.bash.additional_provider_calls, 0);
  assert.match(baseline.caveat, /not provider tokens, billing, quality/);
});

test("phased implementation baseline records context proxy reduction without provider claims", async () => {
  const baseline = buildPhasedImplementationBaseline();
  const checkedIn = JSON.parse(
    await readFile(new URL("fixtures/phased-implementation-baseline.json", import.meta.url), "utf8"),
  );
  assert.deepEqual(baseline, checkedIn);
  assert.ok(baseline.reduction.percent >= 90);
  assert.equal(baseline.single.direct_steering_retained, true);
  assert.equal(baseline.phased.direct_steering_retained, true);
  assert.equal(baseline.authoritative_evidence.provider_usage.availability, "unavailable");
  assert.equal(baseline.authoritative_evidence.quality.availability, "unavailable");
  assert.deepEqual(baseline.claims, {
    provider_call_savings: false,
    provider_token_savings: false,
    billing_savings: false,
    quality_equivalence: false,
  });
});

test("execution-profile baseline keeps policy distinct from unavailable evidence", async () => {
  const baseline = buildExecutionProfileBaseline();
  const checkedIn = JSON.parse(
    await readFile(new URL("fixtures/execution-profile-baseline.json", import.meta.url), "utf8"),
  );
  assert.deepEqual(baseline, checkedIn);
  assert.deepEqual(baseline.fixed_cases, ["simple", "medium", "multi-round"]);
  assert.equal(baseline.packaged_default, "thorough");
  assert.equal(baseline.compatibility.preserves_pre_profile_packaged_thinking, true);
  assert.equal(baseline.comparative_evidence.provider_usage.availability, "unavailable");
  assert.equal(baseline.comparative_evidence.quality.availability, "unavailable");
  assert.deepEqual(baseline.claims, {
    provider_token_savings: false,
    billing_savings: false,
    quality_equivalence: false,
    recommended_default: false,
  });
});

test("worker restart restores assignment and dedup state without changing durable usage", () => {
  const assignmentId = "b".repeat(32);
  const entries = [
    {
      type: "custom",
      customType: "pi-tmux-orchestrator-delivery-v1",
      data: { kind: "context", delivery_id: "1".repeat(32) },
    },
    {
      type: "custom",
      customType: "pi-tmux-orchestrator-context-boundary-v1",
      data: {
        assignment_id: "a".repeat(32),
        assignment_kind: "implementation",
        generation: 1,
        round: 1,
      },
    },
    {
      type: "custom",
      customType: "pi-tmux-orchestrator-delivery-v1",
      data: {
        kind: "assignment",
        delivery_id: "2".repeat(32),
        assignment_id: "a".repeat(32),
        assignment_kind: "implementation",
        round: 1,
      },
    },
    {
      type: "custom",
      customType: "pi-tmux-orchestrator-delivery-v1",
      data: { kind: "report", assignment_id: "a".repeat(32) },
    },
    {
      type: "message",
      message: {
        role: "assistant",
        usage: {
          input: 120,
          output: 30,
          cacheRead: 20,
          cacheWrite: 10,
          reasoning: 5,
          cost: { total: 0.25 },
        },
      },
    },
    {
      type: "custom",
      customType: "pi-tmux-orchestrator-context-boundary-v1",
      data: {
        assignment_id: assignmentId,
        assignment_kind: "implementation",
        generation: 1,
        round: 2,
        usage_baseline: {
          providerCalls: 1,
          input: 120,
          output: 30,
          cacheRead: 20,
          cacheWrite: 10,
          reasoning: 5,
          cost: { total: 0.25 },
          contextTokens: 150,
          contextWindow: 1_000,
          contextPercent: 15,
        },
      },
    },
    {
      type: "custom",
      customType: "pi-tmux-orchestrator-delivery-v1",
      data: {
        kind: "assignment",
        delivery_id: "3".repeat(32),
        assignment_id: assignmentId,
        assignment_kind: "implementation",
        round: 2,
      },
    },
    {
      type: "message",
      message: {
        role: "assistant",
        usage: {
          input: 20,
          output: 10,
          cacheRead: 5,
          cacheWrite: 0,
          reasoning: 2,
          totalTokens: 190,
          cost: { total: 0.05 },
        },
      },
    },
  ];

  const restored = workerHooks.restoreWorkerState(entries);
  assert.deepEqual(restored.activeAssignment, {
    id: assignmentId,
    round: 2,
    kind: "implementation",
    usageBaseline: {
      providerCalls: 1,
      input: 120,
      output: 30,
      cacheRead: 20,
      cacheWrite: 10,
      reasoning: 5,
      cost: { total: 0.25 },
      contextTokens: 150,
      contextWindow: 1_000,
      contextPercent: 15,
    },
  });
  assert.deepEqual([...restored.delivered], ["1".repeat(32), "2".repeat(32), "3".repeat(32)]);
  assert.deepEqual([...restored.assignmentIds], ["a".repeat(32), assignmentId]);

  const ctx = {
    sessionManager: { getEntries: () => entries },
    getContextUsage: () => ({ tokens: 190, contextWindow: 1_000, percent: 19 }),
  };
  assert.deepEqual(workerHooks.totalUsage(ctx), {
    providerCalls: 2,
    input: 140,
    output: 40,
    cacheRead: 25,
    cacheWrite: 10,
    reasoning: 7,
    cost: { total: 0.3 },
    contextTokens: 190,
    contextWindow: 1_000,
    contextPercent: 19,
  });
  assert.deepEqual(
    workerHooks.reportUsage(ctx, restored.activeAssignment.usageBaseline).assignment,
    {
      providerCalls: 1,
      input: 20,
      output: 10,
      cacheRead: 5,
      cacheWrite: 0,
      reasoning: 2,
      cost: { total: 0.05 },
      contextTokens: 190,
      contextWindow: 1_000,
      contextPercent: 19,
      peakContextTokens: 190,
    },
  );
});

test("assignment usage is an immutable delta from the accepted boundary", () => {
  const entries = [
    {
      type: "message",
      message: {
        role: "assistant",
        usage: {
          input: 100, output: 20, cacheRead: 30, cacheWrite: 10,
          reasoning: 4, totalTokens: 160, cost: { total: 0.2 },
        },
      },
    },
    {
      type: "message",
      message: {
        role: "assistant",
        usage: {
          input: 40, output: 15, cacheRead: 120, cacheWrite: 5,
          reasoning: 6, totalTokens: 180, cost: { total: 0.15 },
        },
      },
    },
  ];
  const ctx = {
    sessionManager: { getEntries: () => entries },
    getContextUsage: () => ({ tokens: 175, contextWindow: 1_000, percent: 17.5 }),
  };
  const baseline = {
    providerCalls: 1,
    input: 100,
    output: 20,
    cacheRead: 30,
    cacheWrite: 10,
    reasoning: 4,
    cost: { total: 0.2 },
  };

  assert.deepEqual(workerHooks.reportUsage(ctx, baseline), {
    cumulative: {
      providerCalls: 2,
      input: 140,
      output: 35,
      cacheRead: 150,
      cacheWrite: 15,
      reasoning: 10,
      cost: { total: 0.35 },
      contextTokens: 175,
      contextWindow: 1_000,
      contextPercent: 17.5,
    },
    assignment: {
      providerCalls: 1,
      input: 40,
      output: 15,
      cacheRead: 120,
      cacheWrite: 5,
      reasoning: 6,
      cost: { total: 0.15 },
      contextTokens: 175,
      contextWindow: 1_000,
      contextPercent: 17.5,
      peakContextTokens: 180,
    },
  });
  assert.equal(workerHooks.reportUsage(ctx, { providerCalls: -1 }), null);
});

test("model discovery uses bounded available metadata and respects scoped models", async () => {
  let execCalls = 0;
  const { tool, commands } = harness(async () => {
    execCalls += 1;
    return { code: 0, stdout: "" };
  });
  const model = {
    provider: "anthropic",
    id: "claude-user-model",
    name: "Claude User Model",
    reasoning: true,
    thinkingLevelMap: { off: null, xhigh: "xhigh", max: null },
  };
  const ctx = context({
    context: {
      scopedModels: [{ model, thinkingLevel: "high" }],
      modelRegistry: {
        getAvailable: () => [{ provider: "ignored", id: "outside-scope", reasoning: false }],
      },
    },
  });

  const result = await tool.execute("call", { action: "models", query: "claude" }, undefined, undefined, ctx);
  assert.equal(execCalls, 0);
  assert.equal(result.details.command, "models");
  assert.equal(result.details.data.scoped, true);
  assert.deepEqual(result.details.data.models, [{
    provider: "anthropic",
    model: "claude-user-model",
    name: "Claude User Model",
    reasoning: true,
    thinking_levels: ["high"],
  }]);
  assert.match(result.content[0].text, /anthropic\/claude-user-model thinking=high/);
  assert.equal(JSON.stringify(result).includes("outside-scope"), false);

  const slashCtx = context({
    context: {
      scopedModels: ctx.scopedModels,
      modelRegistry: ctx.modelRegistry,
    },
  });
  await commands.get("or-models").handler("claude", slashCtx);
  assert.match(slashCtx.calls.notifications[0].message, /1\/1 available model/);
});

test("natural-language starts can use the parent model with exact per-role overrides", () => {
  const input = testHooks.startInputWithParentModel(
    {
      useParentModel: true,
      modelOverrides: {
        reviewer: {
          provider: "google",
          model: "gemini-user-model",
          thinking: "medium",
        },
      },
    },
    {
      model: { provider: "anthropic", id: "claude-parent-model" },
      thinkingLevel: "high",
    },
  );
  const args = testHooks.buildStartArgs(input, "/project", { task: "/private/task" });
  const value = (flag) => args[args.indexOf(flag) + 1];
  assert.equal(value("--implementer-provider"), "anthropic");
  assert.equal(value("--implementer-model"), "claude-parent-model");
  assert.equal(value("--implementer-thinking"), "high");
  assert.equal(value("--reviewer-provider"), "google");
  assert.equal(value("--reviewer-model"), "gemini-user-model");
  assert.equal(value("--reviewer-thinking"), "medium");
  assert.equal(value("--probe-provider"), "anthropic");
  assert.equal(value("--probe-model"), "claude-parent-model");
});

test("natural-language starts pass the bounded workspace experiment to both worker transports", () => {
  for (const rpcWorkers of [false, true]) {
    const args = testHooks.buildStartArgs(
      {
        workspaceCapsule: true,
        workspaceRelevantPaths: ["src/service.py", "tests/test_service.py"],
        rpcWorkers,
      },
      "/project",
      { task: "/private/task" },
    );
    assert.equal(args.includes("--workspace-capsule"), true);
    assert.deepEqual(
      args.filter((_value, index) => args[index - 1] === "--workspace-relevant-path"),
      ["src/service.py", "tests/test_service.py"],
    );
    assert.equal(args.includes("--rpc-workers"), rpcWorkers);
  }
});

test("workspace relevant paths require explicit model-tool opt in", async () => {
  let calls = 0;
  const { tool } = harness(async () => {
    calls += 1;
    return { code: 0, stdout: "" };
  });
  await assert.rejects(
    tool.execute(
      "call",
      {
        action: "start",
        task: "Synthetic",
        workspaceRelevantPaths: ["src/service.py"],
      },
      undefined,
      undefined,
      context(),
    ),
    /workspace_relevant_paths_require_capsule/,
  );
  assert.equal(calls, 0);
});

test("workspace capsule model-tool starts reject symlinked project inputs", async () => {
  const project = await mkdtemp(join(tmpdir(), "orchestrator-workspace-project-"));
  const linkedProject = `${project}-link`;
  await symlink(project, linkedProject, "dir");
  let calls = 0;
  try {
    const { tool } = harness(async () => {
      calls += 1;
      return { code: 0, stdout: "" };
    });
    await assert.rejects(
      tool.execute(
        "call",
        {
          action: "start",
          project: linkedProject,
          task: "Synthetic cold assignment",
          workspaceCapsule: true,
        },
        undefined,
        undefined,
        context(),
      ),
      /workspace_capsule_project_not_canonical/,
    );
    assert.equal(calls, 0);
  } finally {
    await rm(linkedProject, { force: true });
    await rm(project, { recursive: true, force: true });
  }
});

test("natural-language starts pass deterministic execution profiles", () => {
  const args = testHooks.buildStartArgs(
    { profile: "review-heavy-economy" },
    "/project",
    { task: "/private/task" },
  );
  assert.equal(args[args.indexOf("--profile") + 1], "review-heavy-economy");
  assert.equal(args.includes("--implementer-thinking"), false);
});

test("natural-language starts preserve project defaults unless explicitly overridden", () => {
  const inherited = testHooks.buildStartArgs(
    {},
    "/project",
    { task: "/private/task" },
  );
  for (const flag of [
    "--implementation-flow",
    "--with-probe",
    "--without-probe",
    "--with-playwright",
    "--without-playwright",
    "--with-django-expert",
    "--without-django-expert",
    "--workspace-capsule",
    "--no-workspace-capsule",
  ]) {
    assert.equal(inherited.includes(flag), false, flag);
  }

  const overridden = testHooks.buildStartArgs(
    {
      implementationFlow: "single",
      withProbe: false,
      withPlaywright: true,
      withDjangoExpert: false,
      workspaceCapsule: false,
    },
    "/project",
    { task: "/private/task" },
  );
  assert.equal(overridden[overridden.indexOf("--implementation-flow") + 1], "single");
  assert.equal(overridden.includes("--without-probe"), true);
  assert.equal(overridden.includes("--with-playwright"), true);
  assert.equal(overridden.includes("--without-django-expert"), true);
  assert.equal(overridden.includes("--no-workspace-capsule"), true);
});

test("natural-language starts pass strict native per-run budget overrides", () => {
  const args = testHooks.buildStartArgs(
    {
      budgetOverrides: {
        enforcement: "hard",
        warning: {
          run: { provider_calls: 20, cache_read_tokens: 5000 },
        },
        hard: {
          assignment: { cost_total: 2.5, context_percent: null },
        },
      },
    },
    "/project",
    { task: "/private/task" },
  );
  const pairs = [];
  for (let index = 0; index < args.length; index += 1) {
    if (args[index] === "--budget-override") pairs.push(args[index + 1]);
  }
  assert.equal(args[args.indexOf("--budget-enforcement") + 1], "hard");
  assert.deepEqual(pairs, [
    "warning.run.provider_calls=20",
    "warning.run.cache_read_tokens=5000",
    "hard.assignment.cost_total=2.5",
    "hard.assignment.context_percent=off",
  ]);
  assert.throws(
    () => testHooks.buildStartArgs(
      { budgetOverrides: { hard: { run: { provider_calls: -1 } } } },
      "/project",
      { task: "/private/task" },
    ),
    /invalid_budget_override/,
  );
});

test("authenticated broker observer steers progress and returns structured final reports to the parent Pi", async () => {
  const directory = await mkdtemp(join(tmpdir(), "pi-tmux-parent-observer-test-"));
  await chmod(directory, 0o700);
  const token = "a".repeat(32);
  const socketPath = join(directory, "broker.sock");
  await writeFile(join(directory, "control.token"), `${token}\n`, { mode: 0o600 });
  const session = "pi-parent-observer-test";
  const reportId = "b".repeat(32);
  const assignmentId = "c".repeat(32);
  let server;
  try {
    server = net.createServer((socket) => {
      socket.once("data", (chunk) => {
        const size = chunk.readUInt32BE(0);
        const hello = JSON.parse(chunk.subarray(4, size + 4).toString("utf8"));
        assert.equal(hello.type, "observe");
        assert.equal(hello.token, token);
        socket.write(testHooks.brokerFrame({
          version: 1, type: "response", id: hello.id, success: true, status: "observing",
        }));
        socket.write(testHooks.brokerFrame({
          version: 1,
          type: "snapshot",
          session,
          state: "active",
          round: 2,
          roles: [
            { role: "implementer", state: "idle" },
            { role: "reviewer", state: "active" },
          ],
          report_count: 0,
          report_replay_complete: true,
        }));
        socket.write(testHooks.brokerFrame({
          version: 1,
          type: "lifecycle",
          session,
          role: "reviewer",
          state: "waiting",
        }));
        socket.write(testHooks.brokerFrame({
          version: 1,
          type: "report",
          session,
          id: reportId,
          assignment_id: assignmentId,
          role: "reviewer",
          round: 2,
          report: { kind: "review", summary: "The implementation is ready.", verdict: "approved" },
          usage: {
            providerCalls: 1,
            input: 40,
            output: 15,
            cacheRead: 120,
            cacheWrite: 5,
            reasoning: 6,
            cost: { total: 0.15 },
            contextTokens: 175,
            contextWindow: 1_000,
            contextPercent: 17.5,
            peakContextTokens: 180,
          },
        }));
        socket.write(testHooks.brokerFrame({
          version: 1, type: "workflow", session, state: "ready", round: 2,
        }));
      });
    });
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(socketPath, resolve);
    });
    const observer = { closed: false, socket: undefined, timer: undefined, stop: () => {} };
    let stopped = false;
    const deliveredMessages = [];
    const parentMessage = new Promise((resolve) => {
      void testHooks.attachParentObserver(
        {
          sendMessage: (message, options) => {
            deliveredMessages.push({ message, options });
            if (message.details.state === "ready") resolve({ message, options });
          },
        },
        {
          data: {
            session,
            paths: { coordination: directory, observer_socket: socketPath },
          },
        },
        observer,
        () => { stopped = true; },
        { triggerInitialActionable: false },
      );
    });
    const delivered = await parentMessage;
    assert.equal(delivered.message.customType, "pi-tmux-orchestrator-parent-v1");
    assert.equal(delivered.message.details.state, "ready");
    assert.match(delivered.message.content, /reviewer report \(round 2\)/);
    assert.match(delivered.message.content, /The implementation is ready/);
    assert.deepEqual(delivered.options, { triggerTurn: true, deliverAs: "steer" });
    assert.equal(stopped, true);
    const progress = deliveredMessages.filter(({ message }) => message.details.event);
    assert.deepEqual(progress.map(({ message }) => message.details.event), ["attached", "lifecycle", "report"]);
    assert.ok(progress.every(({ options }) => (
      options.triggerTurn === false && options.deliverAs === "steer"
    )));
    assert.match(progress[0].message.content, /Parent supervision attached/);
    assert.match(progress[1].message.content, /reviewer is now waiting/);
    assert.doesNotMatch(progress[2].message.content, /The implementation is ready/);
  } finally {
    await new Promise((resolve) => server?.close(resolve) ?? resolve());
    await rm(directory, { recursive: true, force: true });
  }
});

test("attach observation does not replay an already-actionable initial outcome", async () => {
  const directory = await mkdtemp(join(tmpdir(), "pi-tmux-existing-outcome-test-"));
  await chmod(directory, 0o700);
  const token = "d".repeat(32);
  const socketPath = join(directory, "broker.sock");
  await writeFile(join(directory, "control.token"), `${token}\n`, { mode: 0o600 });
  const session = "pi-existing-ready-test";
  let server;
  try {
    server = net.createServer((socket) => {
      socket.once("data", (chunk) => {
        const size = chunk.readUInt32BE(0);
        const hello = JSON.parse(chunk.subarray(4, size + 4).toString("utf8"));
        socket.write(testHooks.brokerFrame({
          version: 1, type: "response", id: hello.id, success: true, status: "observing",
        }));
        socket.write(testHooks.brokerFrame({
          version: 1,
          type: "report",
          session,
          id: "e".repeat(32),
          assignment_id: "f".repeat(32),
          role: "reviewer",
          round: 2,
          report: { kind: "review", summary: "HISTORICAL_REPORT_MUST_NOT_TRIGGER", verdict: "approved" },
        }));
        socket.write(testHooks.brokerFrame({
          version: 1,
          type: "snapshot",
          session,
          state: "ready",
          round: 2,
          roles: [
            { role: "implementer", state: "idle" },
            { role: "reviewer", state: "idle" },
          ],
          report_count: 1,
          report_replay_complete: true,
        }));
      });
    });
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(socketPath, resolve);
    });
    const deliveredMessages = [];
    let stopped = false;
    const observer = { closed: false, socket: undefined, timer: undefined, stop: () => {} };
    const attached = await testHooks.attachParentObserver(
      {
        sendMessage: (message, options) => deliveredMessages.push({ message, options }),
      },
      {
        data: {
          session,
          paths: { coordination: directory, observer_socket: socketPath },
        },
      },
      observer,
      () => { stopped = true; },
      { triggerInitialActionable: false },
    );
    await attached.ready;
    await waitFor(() => stopped);
    assert.equal(deliveredMessages.length, 1);
    assert.equal(deliveredMessages[0].message.details.event, "existing_actionable");
    assert.deepEqual(deliveredMessages[0].options, { triggerTurn: false, deliverAs: "steer" });
    assert.match(deliveredMessages[0].message.content, /already ready/);
    assert.match(deliveredMessages[0].message.content, /not replayed as a new task/);
    assert.doesNotMatch(deliveredMessages[0].message.content, /HISTORICAL_REPORT_MUST_NOT_TRIGGER/);
  } finally {
    await new Promise((resolve) => server?.close(resolve) ?? resolve());
    await rm(directory, { recursive: true, force: true });
  }
});

test("observer snapshots require bounded report replay metadata", () => {
  const value = {
    version: 1,
    type: "snapshot",
    session: "pi-test",
    state: "active",
    round: 1,
    roles: [{ role: "implementer", state: "active" }],
    report_count: 2,
    report_replay_complete: false,
  };
  assert.equal(testHooks.validateObserverFrame(value, "pi-test", "a".repeat(32)), value);
  assert.throws(
    () => testHooks.validateObserverFrame({ ...value, report_count: -1 }, "pi-test", "a".repeat(32)),
    /invalid_observer_snapshot/,
  );
  assert.throws(
    () => testHooks.validateObserverFrame({ version: 1, type: "toString", session: "pi-test" }, "pi-test", "a".repeat(32)),
    /unsupported_observer_frame/,
  );
});

test("observer report usage is bounded numeric metadata", () => {
  const value = {
    version: 1,
    type: "report",
    session: "pi-test",
    id: "a".repeat(32),
    assignment_id: "b".repeat(32),
    role: "reviewer",
    round: 1,
    report: { kind: "review", summary: "Ready.", verdict: "approved" },
    usage: {
      providerCalls: 1,
      input: 40,
      output: 15,
      cacheRead: 120,
      cacheWrite: 5,
      cost: { total: 0.15 },
      peakContextTokens: 180,
    },
  };
  assert.equal(testHooks.validateObserverFrame(value, "pi-test", "c".repeat(32)), value);
  assert.throws(
    () => testHooks.validateObserverFrame({
      ...value,
      usage: { ...value.usage, input: -1 },
    }, "pi-test", "c".repeat(32)),
    /invalid_observer_report/,
  );
  assert.throws(
    () => testHooks.validateObserverFrame({
      ...value,
      usage: { ...value.usage, private_body: "not metadata" },
    }, "pi-test", "c".repeat(32)),
    /invalid_observer_report/,
  );
});

test("parent lifecycle progress is bounded and makes completion state legible", () => {
  const content = testHooks.parentProgressContent(
    "pi-test",
    "active",
    3,
    [
      { role: "reviewer", state: "waiting" },
      { role: "implementer", state: "idle" },
    ],
    { kind: "lifecycle", role: "reviewer", workerState: "waiting" },
  );
  assert.match(content, /Workflow: active/);
  assert.match(content, /reviewer is now waiting/);
  assert.match(content, /implementer: idle/);
  assert.ok(content.length <= 8 * 1024);
});

test("parent updates keep only the latest bounded report per role", () => {
  const event = (role, round, summary) => ({ role, round, report: { kind: role === "reviewer" ? "review" : "implementation", summary } });
  const update = testHooks.parentUpdateContent(
    "pi-test",
    "ready",
    2,
    [event("implementer", 1, "old"), event("implementer", 2, "new"), event("reviewer", 2, "approved")],
  );
  assert.doesNotMatch(update.content, /\"summary\": \"old\"/);
  assert.match(update.content, /\"summary\": \"new\"/);
  assert.match(update.content, /\"summary\": \"approved\"/);
  assert.ok(update.content.length <= 192 * 1024);
});

test("session picker lists valid running orchestrations and returns the exact selection", async () => {
  let calls = 0;
  const pi = {
    exec: async (_command, args) => {
      calls += 1;
      assert.deepEqual(args.slice(1), ["--json", "list"]);
      return {
        code: 0,
        stdout: JSON.stringify(success("list", {
          sessions: [
            { session: "pi-one", project: "/work/one", valid: true },
            { session: "pi-invalid", project: null, valid: false },
            { session: "pi-two", project: "/work/two", valid: true },
          ],
        })),
      };
    },
  };
  const ctx = context({ selection: "pi-two · /work/two" });
  assert.equal(await testHooks.requestedSession(pi, "", ctx), "pi-two");
  assert.deepEqual(ctx.calls.selections[0], {
    title: "Select a running orchestration",
    options: ["pi-one · /work/one", "pi-two · /work/two"],
  });
  assert.equal(await testHooks.requestedSession(pi, " pi-exact ", ctx), "pi-exact");
  assert.equal(calls, 1);
});

test("dashboard selection watches and attaches the exact running orchestration", async () => {
  const previousTmux = process.env.TMUX;
  process.env.TMUX = "/tmp/tmux-test";
  try {
    const seen = [];
    let supervised;
    const pi = {
      exec: async (_command, args) => {
        const action = args[2];
        seen.push(args.slice(1));
        if (action === "status") {
          return {
            code: 0,
            stdout: JSON.stringify(success("status", {
              session: "pi-two",
              project: "/work/two",
              paths: { coordination: "/tmp/run", observer_socket: "/tmp/broker.sock" },
              broker: { workflow: { state: "active", round: 1 }, roles: [] },
              roles: [], panes: [], files: [],
            })),
          };
        }
        return {
          code: 0,
          stdout: JSON.stringify(success("attach", {
            session: "pi-two",
            project: "/work/two",
            transport: "tui",
            mode: "switch-client",
            return_hint: "Prefix then L returns.",
          })),
        };
      },
    };
    const handlers = testHooks.createCommandHandlers(
      pi,
      async (envelope, options) => { supervised = { envelope, options }; },
    );
    const ctx = context({ customResult: { type: "attach", session: "pi-two" } });
    await handlers.dashboard("", ctx);
    assert.deepEqual(seen, [
      ["--json", "status", "pi-two"],
      ["--json", "attach", "pi-two"],
    ]);
    assert.equal(supervised.envelope.data.session, "pi-two");
    assert.deepEqual(supervised.options, { triggerInitialActionable: false });
    assert.match(ctx.calls.notifications.at(-1).message, /Switched to pi-two/);
  } finally {
    if (previousTmux === undefined) delete process.env.TMUX;
    else process.env.TMUX = previousTmux;
  }
});

test("session picker handles cancellation and an empty running list without free-form input", async () => {
  const pi = {
    exec: async () => ({
      code: 0,
      stdout: JSON.stringify(success("list", { sessions: [] })),
    }),
  };
  const ctx = context();
  assert.equal(await testHooks.requestedSession(pi, "", ctx), undefined);
  assert.equal(ctx.calls.selections.length, 0);
  assert.equal(ctx.calls.inputs.length, 0);
  assert.equal(ctx.calls.notifications[0].message, "No running orchestrations are available.");
});

test("dashboard failures are bounded and redact raw errors", async () => {
  const canary = "PRIVATE_COMMAND_ERROR_CANARY_12ab";
  const { commands } = harness(async () => {
    throw new Error(canary.repeat(100));
  });
  const ctx = context({ customError: new Error(canary.repeat(100)) });
  await commands.get("or-dashboard").handler("", ctx);
  assert.equal(ctx.calls.notifications.length, 1);
  assert.equal(ctx.calls.notifications[0].message, "Unable to show the orchestration dashboard");
  assert.equal(JSON.stringify(ctx.calls).includes(canary), false);
  assert.ok(ctx.calls.notifications[0].message.length <= 300);
  assert.equal(ctx.calls.statuses.length, 0);
});

test("passes cancellation to pi.exec and consumes only the JSON envelope", async () => {
  const signal = new AbortController().signal;
  const { tool } = harness(async (command, args, options) => {
    assert.equal(command, "python3");
    assert.equal(args[0], testHooks.CLI_PATH);
    assert.deepEqual(args.slice(1, 3), ["--json", "doctor"]);
    assert.equal(options.signal, signal);
    return { code: 0, stdout: JSON.stringify(success("doctor", { commands: [] })) };
  });
  const result = await tool.execute("call", { action: "doctor" }, signal, undefined, context({ signal }));
  assert.match(result.content[0].text, /checks complete/i);
  assert.equal(result.details.command, "doctor");
});

test("watch attaches the parent through an exact metadata-only status lookup", async () => {
  const seen = [];
  let supervised;
  const pi = {
    exec: async (command, args) => {
      seen.push([command, args]);
      return {
        code: 0,
        stdout: JSON.stringify(success("status", {
          session: "pi-existing",
          project: "/tmp/project",
          paths: { coordination: "/tmp/run", observer_socket: "/tmp/broker.sock" },
          broker: {
            workflow: { state: "active", round: 2 },
            roles: [
              { role: "implementer", state: "idle" },
              { role: "reviewer", state: "active" },
            ],
          },
          roles: [{ name: "implementer" }, { name: "reviewer" }],
          panes: [{ id: "%1" }, { id: "%2" }, { id: "%3" }],
          files: [],
        })),
      };
    },
  };
  const result = await testHooks.executeAction(
    pi,
    { action: "watch", session: " pi-existing " },
    undefined,
    context(),
    async (envelope) => { supervised = envelope; },
  );
  assert.deepEqual(seen[0][1].slice(1), ["--json", "status", "pi-existing"]);
  assert.equal(supervised.command, "status");
  assert.equal(result.details.command, "watch");
  assert.match(result.content[0].text, /watching pi-existing/);
  assert.match(result.content[0].text, /active, round 2/);

  await testHooks.executeAction(
    pi,
    { action: "watch" },
    undefined,
    context(),
    async () => {},
  );
  assert.deepEqual(seen[1][1].slice(1), ["--json", "status"]);
});

test("attach switches an in-tmux parent into the exact live worker grid", async () => {
  const previousTmux = process.env.TMUX;
  process.env.TMUX = "/tmp/tmux-test";
  try {
    const seen = [];
    const pi = {
      exec: async (command, args) => {
        seen.push([command, args]);
        const action = args[2];
        if (action === "status") {
          return {
            code: 0,
            stdout: JSON.stringify(success("status", {
              session: "pi-workers",
              project: "/tmp/project",
              paths: { coordination: "/tmp/run", observer_socket: "/tmp/broker.sock" },
              broker: { workflow: { state: "active", round: 1 }, roles: [] },
              roles: [], panes: [], files: [],
            })),
          };
        }
        return {
          code: 0,
          stdout: JSON.stringify(success("attach", {
            session: "pi-workers",
            project: "/tmp/project",
            transport: "tui",
            mode: "switch-client",
            return_hint: "Press the tmux prefix, then L, to return to the parent Pi session.",
          })),
        };
      },
    };
    const ctx = context();
    let supervised;
    const result = await testHooks.executeAction(
      pi,
      { action: "attach", session: "pi-workers" },
      undefined,
      ctx,
      async (envelope) => { supervised = envelope; },
    );
    assert.deepEqual(seen.map(([, args]) => args.slice(1)), [
      ["--json", "status", "pi-workers"],
      ["--json", "attach", "pi-workers"],
    ]);
    assert.equal(supervised.command, "status");
    assert.equal(result.details.command, "attach");
    assert.match(result.content[0].text, /Switched to pi-workers/);
    assert.match(ctx.calls.notifications[0].message, /Prefix then L detaches/);
  } finally {
    if (previousTmux === undefined) delete process.env.TMUX;
    else process.env.TMUX = previousTmux;
  }
});

test("attach fails before execution when the parent Pi is outside tmux", async () => {
  const previousTmux = process.env.TMUX;
  delete process.env.TMUX;
  try {
    let calls = 0;
    const pi = {
      exec: async () => {
        calls += 1;
        return { code: 0, stdout: "" };
      },
    };
    await assert.rejects(
      testHooks.executeAction(
        pi,
        { action: "attach", session: "pi-workers" },
        undefined,
        context(),
        async () => {},
      ),
      /attach_requires_parent_tmux/,
    );
    assert.equal(calls, 0);
  } finally {
    if (previousTmux === undefined) delete process.env.TMUX;
    else process.env.TMUX = previousTmux;
  }
});

test("start keeps the invoking Pi as parent and starts no separate parent session", async () => {
  const actions = [];
  let supervised;
  const pi = {
    exec: async (_command, args) => {
      const action = args[2];
      actions.push(action);
      assert.equal(action, "start");
      const dryRun = args.includes("--dry-run");
      return {
        code: 0,
        stdout: JSON.stringify(success("start", {
          project: process.cwd(),
          session: "pi-project-agents",
          roles: [],
          transport: "tui",
          trust: { child_bypass: false },
          dry_run: dryRun,
          paths: {
            state_root: "/tmp/state",
            coordination: dryRun ? null : "/tmp/state/pi-project-agents/run",
            observer_socket: dryRun ? null : "/tmp/state/pi-project-agents/run/broker.sock",
          },
        })),
      };
    },
  };
  const result = await testHooks.executeAction(
    pi,
    { action: "start", task: "Synthetic parent identity test." },
    undefined,
    context({ confirmations: [true] }),
    async (envelope) => { supervised = envelope; },
  );
  assert.deepEqual(actions, ["start", "start"]);
  assert.equal(supervised.command, "start");
  assert.equal(supervised.data.session, "pi-project-agents");
  assert.equal(result.details.command, "start");
  assert.match(result.content[0].text, /This invoking Pi remains the parent/);
});

test("start previews CLI policy, keeps private text out of argv, and cleans mode-0600 files", async () => {
  const canary = "PRIVATE_TASK_CANARY_49a7";
  const contextCanary = "PRIVATE_CONTEXT_CAPSULE_CANARY_72bf";
  const paths = [];
  let calls = 0;
  const { tool } = harness(async (_command, args, options) => {
    calls += 1;
    assert.equal(args.includes(canary), false);
    assert.equal(args.includes(contextCanary), false);
    assert.ok(args.includes("reviewer=/reviewed/reviewer/SKILL.md"));
    assert.equal(args[args.indexOf("--force-specialist") + 1], "playwright");
    assert.ok(options.signal);
    const taskPath = args[args.indexOf("--task-file") + 1];
    const contextPath = args[args.indexOf("--context-capsule-file") + 1];
    paths.push(taskPath, contextPath);
    assert.equal((await stat(taskPath)).mode & 0o777, 0o600);
    assert.equal((await stat(contextPath)).mode & 0o777, 0o600);
    assert.equal(await readFile(taskPath, "utf8"), canary);
    assert.match(await readFile(contextPath, "utf8"), new RegExp(contextCanary));
    const dryRun = args.includes("--dry-run");
    const data = {
      project: process.cwd(),
      session: "pi-project-agents",
      roles: [
        { name: "implementer", provider: "provider", model: "writer", thinking: "high" },
        { name: "reviewer", provider: "provider", model: "reviewer", thinking: "high" },
      ],
      transport: args.includes("--rpc-workers") ? "rpc" : "tui",
      implementation_flow: "phased",
      forced_specialists: ["playwright"],
      execution_profile: {
        name: args[args.indexOf("--profile") + 1],
        kind: "packaged",
        source: "per-run",
      },
      trust: {
        child_bypass: false,
        policy: args.includes("--rpc-workers")
          ? "saved-or-global-policy"
          : "native-prompts",
      },
      dry_run: dryRun,
      context_capsule: { present: true, chars: contextCanary.length + 18 },
      workspace_capsule: {
        enabled: args.includes("--workspace-capsule"), schema_version: 1,
        instruction_count: 1, marker_count: 1, relevant_path_count: 1,
      },
      budget_policy: {
        enforcement: "warn-only",
        warning: { run: { operational_tokens: 600000 }, role: {}, assignment: {} },
        hard: { run: {}, role: {}, assignment: {} },
      },
      worker_resources: {
        skill_discovery: false,
        skills: { implementer: [], reviewer: ["/reviewed/reviewer/SKILL.md"] },
      },
      paths: { state_root: "/tmp/external-state", coordination: dryRun ? null : "/tmp/external-state/run" },
    };
    return { code: 0, stdout: JSON.stringify(success("start", data)) };
  });
  const signal = new AbortController().signal;
  const ctx = context({ confirmations: [true], signal });
  const result = await tool.execute(
    "call",
    {
      action: "start",
      task: canary,
      profile: "economy",
      implementationFlow: "phased",
      forceSpecialists: ["playwright"],
      withPlaywright: true,
      contextCapsule: { currentState: contextCanary },
      workspaceCapsule: true,
      workspaceRelevantPaths: ["src/service.py"],
      rpcWorkers: true,
      workerSkills: { reviewer: ["/reviewed/reviewer/SKILL.md"] },
    },
    signal,
    undefined,
    ctx,
  );
  assert.equal(calls, 2);
  assert.match(ctx.calls.confirmations[0].message, /ignores project executable resources/);
  assert.match(ctx.calls.confirmations[0].message, /Worker transport: rpc/);
  assert.match(ctx.calls.confirmations[0].message, /Implementation flow: phased/);
  assert.match(ctx.calls.confirmations[0].message, /Forced specialists: playwright/);
  assert.match(ctx.calls.confirmations[0].message, /Execution profile: economy \(packaged, source=per-run\)/);
  assert.match(ctx.calls.confirmations[0].message, /provider\/writer/);
  assert.match(ctx.calls.confirmations[0].message, /warning\.run: operational_tokens=600000/);
  assert.match(ctx.calls.confirmations[0].message, /reviewer: \/reviewed\/reviewer\/SKILL\.md/);
  assert.match(ctx.calls.confirmations[0].message, /Parent context capsule: [0-9]+ characters/);
  assert.match(ctx.calls.confirmations[0].message, /Experimental workspace capsule: validated schema=1; instructions=1; markers=1; relevant=1/);
  assert.equal(JSON.stringify(result).includes(canary), false);
  assert.equal(JSON.stringify(result).includes(contextCanary), false);
  for (const path of paths) await assert.rejects(access(path));
});

test("slash start can inherit exact-project orchestration defaults", async () => {
  let calls = 0;
  const { commands } = harness(async (_command, args) => {
    calls += 1;
    for (const flag of [
      "--implementation-flow",
      "--with-probe",
      "--without-probe",
      "--with-playwright",
      "--without-playwright",
      "--with-django-expert",
      "--without-django-expert",
      "--workspace-capsule",
      "--no-workspace-capsule",
    ]) {
      assert.equal(args.includes(flag), false, flag);
    }
    return {
      code: 0,
      stdout: JSON.stringify(success("start", {
        project: process.cwd(),
        session: "pi-project-defaults",
        roles: [],
        trust: { child_bypass: false },
        dry_run: args.includes("--dry-run"),
        paths: { state_root: "/tmp/state", coordination: null },
      })),
    };
  });
  const ctx = context({ confirmations: [false, true] });
  await commands.get("or-start").handler("synthetic", ctx);
  assert.equal(calls, 2);
  assert.match(ctx.calls.confirmations[0].message, /configured project\/global defaults/);
});

test("controller mode requires and collects an explicit target project", async () => {
  const previous = process.env.PI_TMUX_CONTROLLER;
  process.env.PI_TMUX_CONTROLLER = "1";
  try {
    let calls = 0;
    const { tool, commands } = harness(async (_command, args) => {
      calls += 1;
      assert.equal(args[args.indexOf("--project") + 1], process.cwd());
      return {
        code: 0,
        stdout: JSON.stringify(success("start", {
          project: process.cwd(), session: "pi-controller-test", roles: [],
          trust: { child_bypass: false }, dry_run: args.includes("--dry-run"),
          paths: { state_root: "/tmp/state", coordination: null },
        })),
      };
    });
    await assert.rejects(
      tool.execute(
        "call",
        { action: "start", task: "synthetic" },
        undefined,
        undefined,
        context(),
      ),
      /explicit_project/,
    );
    assert.equal(calls, 0);

    const ctx = context({
      input: process.cwd(),
      confirmations: [true, false, false, false, false, false, true],
    });
    await commands.get("or-start").handler("synthetic", ctx);
    assert.equal(calls, 2);
    assert.equal(ctx.calls.inputs[0].title, "Target project directory");
  } finally {
    if (previous === undefined) delete process.env.PI_TMUX_CONTROLLER;
    else process.env.PI_TMUX_CONTROLLER = previous;
  }
});

test("controller lifecycle blocks switching without persistent extension chrome", async () => {
  const previous = process.env.PI_TMUX_CONTROLLER;
  process.env.PI_TMUX_CONTROLLER = "1";
  try {
    let calls = 0;
    const { events } = harness(async () => {
      calls += 1;
      return { code: 0, stdout: "" };
    });
    const ctx = context();
    assert.equal(calls, 0);
    assert.equal(events.has("session_start"), true);
    events.get("session_start")({}, ctx);
    await drainPromises();
    assert.equal(ctx.calls.notifications.length, 0);
    assert.equal(events.has("session_shutdown"), true);
    assert.deepEqual(await events.get("session_before_switch")({}, ctx), { cancel: true });
    assert.deepEqual(await events.get("session_before_fork")({}, ctx), { cancel: true });
    assert.equal(ctx.calls.statuses.length, 0);
    assert.equal(ctx.calls.widgets.length, 0);
    assert.equal(ctx.calls.titles.length, 0);
    assert.match(ctx.calls.notifications.at(-2).message, /fixed persistent Pi session/);
    assert.match(ctx.calls.notifications.at(-1).message, /disabled/);
  } finally {
    if (previous === undefined) delete process.env.PI_TMUX_CONTROLLER;
    else process.env.PI_TMUX_CONTROLLER = previous;
  }
});

test("start cancellation propagates and still cleans its private file", async () => {
  const signal = new AbortController().signal;
  let path;
  const { tool } = harness(async (_command, args, options) => {
    path = args[args.indexOf("--task-file") + 1];
    assert.equal(options.signal, signal);
    assert.equal(await readFile(path, "utf8"), "synthetic cancellation task");
    throw new Error("cancelled");
  });
  await assert.rejects(
    tool.execute(
      "call",
      { action: "start", task: "synthetic cancellation task" },
      signal,
      undefined,
      context({ signal }),
    ),
    /cancelled/,
  );
  await assert.rejects(access(path));
});

test("start rejects no-UI mode and untrusted child approval before execution", async () => {
  let calls = 0;
  const { tool } = harness(async () => { calls += 1; return { code: 0, stdout: "" }; });
  await assert.rejects(
    tool.execute("call", { action: "start", task: "synthetic" }, undefined, undefined, context({ context: { mode: "json", hasUI: false } })),
    /interactive_tui/,
  );
  await assert.rejects(
    tool.execute("call", { action: "start", task: "synthetic", approveProject: true }, undefined, undefined, context({ trusted: false })),
    /trusted_parent/,
  );
  assert.equal(calls, 0);
});

test("trusted approval requires explicit bypass and start confirmations", async () => {
  const argvs = [];
  const { tool } = harness(async (_command, args) => {
    argvs.push(args);
    const dryRun = args.includes("--dry-run");
    return {
      code: 0,
      stdout: JSON.stringify(success("start", {
        project: process.cwd(), session: "pi-project-agents", roles: [],
        trust: { child_bypass: true }, dry_run: dryRun,
        paths: { state_root: "/tmp/state", coordination: null },
      })),
    };
  });
  const ctx = context({ trusted: true, confirmations: [true, true] });
  await tool.execute(
    "call",
    { action: "start", task: "synthetic", approveProject: true },
    undefined,
    undefined,
    ctx,
  );
  assert.equal(ctx.calls.confirmations.length, 2);
  assert.ok(argvs.every((argv) => argv.includes("--approve-project")));
  assert.match(ctx.calls.confirmations[0].message, /does not automatically apply/);
});

test("short start command reuses private preview and explicit confirmation flow", async () => {
  const task = "PRIVATE_SLASH_START_TASK_51ce";
  const paths = [];
  let execCalls = 0;
  const { commands } = harness(async (_command, args) => {
    execCalls += 1;
    assert.equal(args.includes(task), false);
    assert.equal(args.includes("--rpc-workers"), false);
    assert.equal(args.includes("--workspace-capsule"), true);
    assert.deepEqual(
      args.filter((_value, index) => args[index - 1] === "--workspace-relevant-path"),
      ["src/service.py", "tests/test_service.py"],
    );
    assert.equal(args[args.indexOf("--implementation-flow") + 1], "phased");
    const taskPath = args[args.indexOf("--task-file") + 1];
    paths.push(taskPath);
    assert.equal((await stat(taskPath)).mode & 0o777, 0o600);
    assert.equal(await readFile(taskPath, "utf8"), task);
    const dryRun = args.includes("--dry-run");
    return {
      code: 0,
      stdout: JSON.stringify(success("start", {
        project: process.cwd(),
        session: "pi-project-agents",
        roles: [],
        trust: { child_bypass: false },
        workspace_capsule: {
          enabled: true, schema_version: 1, instruction_count: 1,
          marker_count: 1, relevant_path_count: 2,
        },
        dry_run: dryRun,
        paths: { state_root: "/tmp/state", coordination: dryRun ? null : "/tmp/state/run" },
      })),
    };
  });
  const ctx = context({
    confirmations: [true, true, false, false, false, true, true],
    editors: ["src/service.py\ntests/test_service.py"],
  });
  await commands.get("or-start").handler(task, ctx);
  assert.equal(execCalls, 2);
  assert.equal(ctx.calls.confirmations.at(-1).title, "Start tmux orchestration?");
  assert.equal(ctx.calls.editors[0].title, "Workspace capsule relevant paths");
  assert.match(ctx.calls.confirmations.at(-1).message, /Experimental workspace capsule: validated schema=1/);
  assert.equal(JSON.stringify(ctx.calls).includes(task), false);
  for (const path of paths) await assert.rejects(access(path));
});

test("start command rejects non-TUI use and cannot start after confirmation decline", async () => {
  let execCalls = 0;
  const { commands } = harness(async (_command, args) => {
    execCalls += 1;
    return {
      code: 0,
      stdout: JSON.stringify(success("start", {
        project: process.cwd(), session: "pi-project-agents", roles: [],
        trust: { child_bypass: false }, dry_run: args.includes("--dry-run"),
        paths: { state_root: "/tmp/state", coordination: null },
      })),
    };
  });
  const rpcCtx = context({ context: { mode: "rpc", hasUI: true } });
  await commands.get("or-start").handler("synthetic", rpcCtx);
  assert.equal(execCalls, 0);
  assert.match(rpcCtx.calls.notifications[0].message, /interactive TUI/);

  const declinedCtx = context({ confirmations: [false, false, false, false] });
  await commands.get("or-start").handler("synthetic", declinedCtx);
  assert.equal(execCalls, 1);
  assert.equal(declinedCtx.calls.notifications.at(-1).message, "Unable to start orchestration");
});

test("probe and specialist bodies also use unique private files and file-only argv", async () => {
  const bodies = {
    task: "PRIVATE_TASK_BODY",
    probe: "PRIVATE_PROBE_BODY",
    playwright: "PRIVATE_PLAYWRIGHT_BODY",
    django: "PRIVATE_DJANGO_BODY",
  };
  const created = [];
  await testHooks.withPrivateFiles(bodies, async (paths) => {
    created.push(...Object.values(paths));
    assert.equal(new Set(created).size, 4);
    for (const [name, path] of Object.entries(paths)) {
      assert.equal((await stat(path)).mode & 0o777, 0o600);
      assert.equal(await readFile(path, "utf8"), bodies[name]);
    }
    const argv = testHooks.buildStartArgs(
      {
        withProbe: true,
        withPlaywright: true,
        withDjangoExpert: true,
        rpcWorkers: true,
        workerSkills: {
          reviewer: ["/reviewed/reviewer-skill.md"],
          probe: ["/reviewed/probe-skill.md"],
        },
      },
      process.cwd(),
      paths,
    );
    for (const body of Object.values(bodies)) assert.equal(argv.includes(body), false);
    for (const path of created) assert.ok(argv.includes(path));
    assert.ok(argv.includes("--rpc-workers"));
    assert.deepEqual(
      argv.filter((_value, index) => argv[index - 1] === "--worker-skill"),
      ["reviewer=/reviewed/reviewer-skill.md", "probe=/reviewed/probe-skill.md"],
    );
  });
  for (const path of created) await assert.rejects(access(path));
});

test("send transfers message through a private file and cleans it", async () => {
  const canary = "PRIVATE_MESSAGE_CANARY_8cb2";
  let path;
  const { tool } = harness(async (_command, args) => {
    assert.equal(args.includes(canary), false);
    path = args[args.indexOf("--message-file") + 1];
    assert.equal((await stat(path)).mode & 0o777, 0o600);
    assert.equal(await readFile(path, "utf8"), canary);
    return { code: 0, stdout: JSON.stringify(success("send", { session: "pi-test", role: "reviewer", sent: true })) };
  });
  const result = await tool.execute(
    "call",
    { action: "send", session: "pi-test", role: "reviewer", message: canary },
    undefined,
    undefined,
    context(),
  );
  assert.equal(JSON.stringify(result).includes(canary), false);
  await assert.rejects(access(path));
});

test("interactive send cancels safely at TUI, session, role, or message boundaries", async () => {
  let execCalls = 0;
  const { commands } = harness(async (_command, args) => {
    execCalls += 1;
    assert.equal(args[2], "list");
    return { code: 0, stdout: JSON.stringify(success("list", { sessions: [] })) };
  });
  await commands.get("or-send").handler(
    "pi-test",
    context({ context: { mode: "rpc", hasUI: true } }),
  );
  await commands.get("or-send").handler("", context());
  await commands.get("or-send").handler("pi-test", context());
  await commands.get("or-send").handler(
    "pi-test",
    context({ selection: "reviewer", editor: "   " }),
  );
  assert.equal(execCalls, 1);
});

test("interactive send selects an exact session/role and redacts the private file payload", async () => {
  const canary = "PRIVATE_SLASH_MESSAGE_CANARY_7ad1";
  let path;
  const { commands } = harness(async (_command, args) => {
    assert.equal(args.includes(canary), false);
    if (args[2] === "list") {
      return {
        code: 0,
        stdout: JSON.stringify(success("list", {
          sessions: [{ session: "pi-test", project: "/tmp/project", valid: true }],
        })),
      };
    }
    path = args[args.indexOf("--message-file") + 1];
    assert.equal((await stat(path)).mode & 0o777, 0o600);
    assert.equal(await readFile(path, "utf8"), canary);
    return {
      code: 0,
      stdout: JSON.stringify(success("send", { session: "pi-test", role: "reviewer", sent: true })),
    };
  });
  const ctx = context({
    selections: ["pi-test · /tmp/project", "reviewer"],
    editor: canary,
  });
  await commands.get("or-send").handler("", ctx);
  assert.deepEqual(ctx.calls.selections[0].options, ["pi-test · /tmp/project"]);
  assert.deepEqual(ctx.calls.selections[1].options, ["implementer", "reviewer", "probe", "playwright", "django"]);
  assert.equal(ctx.calls.notifications.at(-1).message, "Sent to pi-test/reviewer");
  assert.equal(JSON.stringify(ctx.calls).includes(canary), false);
  await assert.rejects(access(path));
});

test("interactive send cleans private files and bounds errors when delegation fails", async () => {
  const message = "PRIVATE_FAILED_SLASH_MESSAGE_d231";
  let path;
  const { commands } = harness(async (_command, args) => {
    path = args[args.indexOf("--message-file") + 1];
    assert.equal(await readFile(path, "utf8"), message);
    throw new Error(`${message}:${"x".repeat(20_000)}`);
  });
  const ctx = context({ selection: "implementer", editor: message });
  await commands.get("or-send").handler("pi-test", ctx);
  assert.equal(ctx.calls.notifications.at(-1).message, "Unable to send orchestration message");
  assert.equal(JSON.stringify(ctx.calls).includes(message), false);
  await assert.rejects(access(path));
});

test("stop selects an exact session and requires explicit UI confirmation before --yes", async () => {
  const argvs = [];
  const { commands } = harness(async (_command, args) => {
    argvs.push(args);
    const action = args[2];
    const data = action === "list"
      ? { sessions: [{ session: "pi-test", project: "/tmp/project", valid: true }] }
      : { session: "pi-test", stopped: true };
    return { code: 0, stdout: JSON.stringify(success(action, data)) };
  });
  const ctx = context({
    selection: "pi-test · /tmp/project",
    confirmations: [true],
  });
  await commands.get("or-stop").handler("", ctx);
  assert.deepEqual(argvs.map((args) => args.slice(1)), [
    ["--json", "list"],
    ["--json", "stop", "pi-test", "--yes"],
  ]);
  assert.equal(ctx.calls.inputs.length, 0);
  assert.match(ctx.calls.confirmations[0].message, /retained/);

  const dashboardCtx = context({
    customResult: { type: "stop", session: "pi-dashboard" },
    confirmations: [true],
  });
  await commands.get("or-dashboard").handler("", dashboardCtx);
  assert.deepEqual(argvs.at(-1).slice(1), ["--json", "stop", "pi-dashboard", "--yes"]);
  assert.match(dashboardCtx.calls.confirmations[0].message, /pi-dashboard/);

  const declinedCtx = context({ confirmations: [false] });
  await commands.get("or-stop").handler("pi-other", declinedCtx);
  assert.equal(argvs.length, 3);
});
