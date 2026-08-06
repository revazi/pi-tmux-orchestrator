import assert from "node:assert/strict";
import { access, readFile, stat } from "node:fs/promises";
import { test } from "node:test";
import extension, { testHooks } from "../extensions/tmux-orchestrator.js";

function success(command, data = {}) {
  return { schema_version: "1", command, success: true, data, error: null };
}

function harness(exec) {
  const tools = [];
  const commands = new Map();
  const events = new Map();
  const pi = {
    exec,
    registerTool(tool) { tools.push(tool); },
    registerCommand(name, command) { commands.set(name, command); },
    on(name, handler) { events.set(name, handler); },
  };
  extension(pi);
  return { pi, tool: tools[0], tools, commands, events };
}

function context(overrides = {}) {
  const confirmations = [...(overrides.confirmations || [])];
  const calls = { confirmations: [], notifications: [], statuses: [], widgets: [] };
  return {
    calls,
    mode: "tui",
    hasUI: true,
    cwd: process.cwd(),
    signal: overrides.signal,
    isProjectTrusted: () => overrides.trusted ?? false,
    ui: {
      confirm: async (title, message) => {
        calls.confirmations.push({ title, message });
        return confirmations.shift() ?? false;
      },
      notify: (message, level) => calls.notifications.push({ message, level }),
      setStatus: (key, value) => calls.statuses.push({ key, value }),
      setWidget: (key, value) => calls.widgets.push({ key, value }),
      editor: async () => overrides.editor,
      input: async () => overrides.input,
      theme: { fg: (_color, text) => text },
    },
    ...overrides.context,
  };
}

test("registers one bounded model tool and three interactive commands", () => {
  const { tool, tools, commands, events } = harness(async () => ({ code: 0, stdout: "" }));
  assert.equal(tools.length, 1);
  assert.equal(tool.name, "tmux_orchestrator");
  assert.deepEqual(tool.parameters.properties.action.enum, ["doctor", "list", "status", "start", "send"]);
  assert.equal(tool.parameters.properties.action.enum.includes("restart"), false);
  assert.equal(tool.parameters.properties.action.enum.includes("stop"), false);
  assert.equal(tool.renderCall, undefined);
  assert.equal(tool.renderResult, undefined);
  assert.deepEqual([...commands.keys()], ["orchestrate", "orchestrations", "orchestrator-stop"]);
  assert.ok(events.has("session_start"));
  assert.ok(events.has("session_shutdown"));
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

test("start previews CLI policy, keeps private text out of argv, and cleans mode-0600 files", async () => {
  const canary = "PRIVATE_TASK_CANARY_49a7";
  const paths = [];
  let calls = 0;
  const { tool } = harness(async (_command, args, options) => {
    calls += 1;
    assert.equal(args.includes(canary), false);
    assert.ok(options.signal);
    const taskPath = args[args.indexOf("--task-file") + 1];
    paths.push(taskPath);
    assert.equal((await stat(taskPath)).mode & 0o777, 0o600);
    assert.equal(await readFile(taskPath, "utf8"), canary);
    const dryRun = args.includes("--dry-run");
    const data = {
      project: process.cwd(),
      session: "pi-project-agents",
      roles: [
        { name: "implementer", provider: "provider", model: "writer", thinking: "high" },
        { name: "reviewer", provider: "provider", model: "reviewer", thinking: "high" },
      ],
      trust: { child_bypass: false, policy: "native-prompts" },
      dry_run: dryRun,
      paths: { state_root: "/tmp/external-state", coordination: dryRun ? null : "/tmp/external-state/run" },
    };
    return { code: 0, stdout: JSON.stringify(success("start", data)) };
  });
  const signal = new AbortController().signal;
  const ctx = context({ confirmations: [true], signal });
  const result = await tool.execute("call", { action: "start", task: canary }, signal, undefined, ctx);
  assert.equal(calls, 2);
  assert.match(ctx.calls.confirmations[0].message, /Native child trust prompts/);
  assert.match(ctx.calls.confirmations[0].message, /provider\/writer/);
  assert.equal(JSON.stringify(result).includes(canary), false);
  for (const path of paths) await assert.rejects(access(path));
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
      { withProbe: true, withPlaywright: true, withDjangoExpert: true },
      process.cwd(),
      paths,
    );
    for (const body of Object.values(bodies)) assert.equal(argv.includes(body), false);
    for (const path of created) assert.ok(argv.includes(path));
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

test("stop is command-only and requires explicit UI confirmation", async () => {
  let argv;
  const { commands } = harness(async (_command, args) => {
    argv = args;
    return { code: 0, stdout: JSON.stringify(success("stop", { session: "pi-test", stopped: true })) };
  });
  const ctx = context({ confirmations: [true] });
  await commands.get("orchestrator-stop").handler("pi-test", ctx);
  assert.deepEqual(argv.slice(1), ["--json", "stop", "pi-test", "--yes"]);
  assert.match(ctx.calls.confirmations[0].message, /retained/);
});
