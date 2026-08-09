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
      theme: { fg: (_color, text) => text },
    },
    ...overrides.context,
  };
}

test("registers one bounded model tool and the exact canonical/alias command surface", () => {
  const { tool, tools, commands, events } = harness(async () => ({ code: 0, stdout: "" }));
  assert.equal(tools.length, 1);
  assert.equal(tool.name, "tmux_orchestrator");
  assert.deepEqual(tool.parameters.properties.action.enum, ["doctor", "list", "status", "start", "send"]);
  assert.equal(tool.parameters.properties.action.enum.includes("restart"), false);
  assert.equal(tool.parameters.properties.action.enum.includes("stop"), false);
  assert.equal(tool.renderCall, undefined);
  assert.equal(tool.renderResult, undefined);
  assert.deepEqual(
    [...commands.keys()],
    [
      "orchestrator-help",
      "orchestrator-doctor",
      "orchestrator-start",
      "orchestrator-list",
      "orchestrator-status",
      "orchestrator-send",
      "orchestrator-stop",
      "orchestrate",
      "orchestrations",
    ],
  );
  assert.equal(commands.has("orchestrator-attach"), false);
  assert.equal(commands.has("orchestrator-restart"), false);
  assert.equal(commands.get("orchestrator-start").handler, commands.get("orchestrate").handler);
  assert.equal(commands.get("orchestrator-list").handler, commands.get("orchestrations").handler);
  assert.ok(events.has("session_start"));
  assert.ok(events.has("session_before_switch"));
  assert.ok(events.has("session_before_fork"));
  assert.ok(events.has("session_shutdown"));
});

test("help is bounded, subprocess-free, and documents terminal-only operations", async () => {
  let execCalls = 0;
  const { commands } = harness(async () => {
    execCalls += 1;
    return { code: 0, stdout: "" };
  });
  const ctx = context();
  await commands.get("orchestrator-help").handler("PRIVATE_HELP_ARGUMENT", ctx);
  assert.equal(execCalls, 0);
  assert.equal(ctx.calls.notifications.length, 1);
  const message = ctx.calls.notifications[0].message;
  assert.ok(message.length <= 2400);
  assert.match(message, /\/orchestrator-start/);
  assert.match(message, /\/orchestrator-send/);
  assert.match(message, /Attach and restart remain terminal-only/);
  assert.equal(message.includes("PRIVATE_HELP_ARGUMENT"), false);
});

test("doctor, list alias, and status commands delegate exact bounded JSON CLI actions", async () => {
  const seen = [];
  const { commands } = harness(async (command, args) => {
    assert.equal(command, "python3");
    assert.equal(args[0], testHooks.CLI_PATH);
    seen.push(args.slice(1));
    const action = args[2];
    const data = action === "list"
      ? { sessions: [{ session: "pi-one", project: "/tmp/project" }] }
      : action === "status"
        ? { session: args[3] || "pi-current", roles: [], panes: [], files: [] }
        : { commands: [] };
    return { code: 0, stdout: JSON.stringify(success(action, data)) };
  });
  const ctx = context();
  await commands.get("orchestrator-doctor").handler("", ctx);
  await commands.get("orchestrations").handler("", ctx);
  await commands.get("orchestrator-status").handler("  pi-exact  ", ctx);
  await commands.get("orchestrator-status").handler("", ctx);
  assert.deepEqual(seen, [
    ["--json", "doctor"],
    ["--json", "list"],
    ["--json", "status", "pi-exact"],
    ["--json", "status"],
  ]);
  assert.deepEqual(ctx.calls.widgets.at(-1).value, ["pi-one · /tmp/project"]);
  assert.ok(ctx.calls.notifications.every(({ message }) => message.length <= 800));
});

test("slash-command failures are bounded and redact raw subprocess errors", async () => {
  const canary = "PRIVATE_COMMAND_ERROR_CANARY_12ab";
  const { commands } = harness(async () => {
    throw new Error(canary.repeat(100));
  });
  const ctx = context();
  await commands.get("orchestrator-status").handler("pi-test", ctx);
  assert.equal(ctx.calls.notifications.length, 1);
  assert.equal(ctx.calls.notifications[0].message, "Unable to show orchestration status");
  assert.equal(JSON.stringify(ctx.calls).includes(canary), false);
  assert.ok(ctx.calls.notifications[0].message.length <= 300);
  assert.deepEqual(ctx.calls.statuses.at(-1), { key: "tmux-orchestrator", value: "tmux: error" });
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
      confirmations: [false, false, false, true],
    });
    await commands.get("orchestrator-start").handler("synthetic", ctx);
    assert.equal(calls, 2);
    assert.equal(ctx.calls.inputs[0].title, "Target project directory");
  } finally {
    if (previous === undefined) delete process.env.PI_TMUX_CONTROLLER;
    else process.env.PI_TMUX_CONTROLLER = previous;
  }
});

test("controller session lifecycle advertises its dedicated identity without a subprocess", async () => {
  const previous = process.env.PI_TMUX_CONTROLLER;
  process.env.PI_TMUX_CONTROLLER = "1";
  try {
    let calls = 0;
    const { events } = harness(async () => {
      calls += 1;
      return { code: 0, stdout: "" };
    });
    const ctx = context();
    await events.get("session_start")({}, ctx);
    assert.equal(calls, 0);
    assert.deepEqual(ctx.calls.titles, ["Pi Tmux Orchestrator Controller"]);
    assert.deepEqual(ctx.calls.statuses.at(-1), {
      key: "tmux-orchestrator",
      value: "tmux: controller",
    });
    assert.match(ctx.calls.widgets.at(-1).value.join("\n"), /Target projects must be explicit/);
    assert.deepEqual(await events.get("session_before_switch")({}, ctx), { cancel: true });
    assert.deepEqual(await events.get("session_before_fork")({}, ctx), { cancel: true });
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

test("canonical start command reuses private preview and explicit confirmation flow", async () => {
  const task = "PRIVATE_SLASH_START_TASK_51ce";
  const paths = [];
  let execCalls = 0;
  const { commands } = harness(async (_command, args) => {
    execCalls += 1;
    assert.equal(args.includes(task), false);
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
        dry_run: dryRun,
        paths: { state_root: "/tmp/state", coordination: dryRun ? null : "/tmp/state/run" },
      })),
    };
  });
  const ctx = context({ confirmations: [false, false, false, true] });
  await commands.get("orchestrator-start").handler(task, ctx);
  assert.equal(execCalls, 2);
  assert.equal(ctx.calls.confirmations.at(-1).title, "Start tmux orchestration?");
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
  await commands.get("orchestrator-start").handler("synthetic", rpcCtx);
  assert.equal(execCalls, 0);
  assert.match(rpcCtx.calls.notifications[0].message, /interactive TUI/);

  const declinedCtx = context({ confirmations: [false, false, false, false] });
  await commands.get("orchestrate").handler("synthetic", declinedCtx);
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

test("interactive send cancels safely at TUI, session, role, or message boundaries", async () => {
  let execCalls = 0;
  const { commands } = harness(async () => {
    execCalls += 1;
    return { code: 0, stdout: "" };
  });
  await commands.get("orchestrator-send").handler(
    "pi-test",
    context({ context: { mode: "rpc", hasUI: true } }),
  );
  await commands.get("orchestrator-send").handler("", context());
  await commands.get("orchestrator-send").handler("", context({ input: "pi-test" }));
  await commands.get("orchestrator-send").handler(
    "",
    context({ input: "pi-test", selection: "reviewer", editor: "   " }),
  );
  assert.equal(execCalls, 0);
});

test("interactive send obtains exact session/role/message and redacts the private file payload", async () => {
  const canary = "PRIVATE_SLASH_MESSAGE_CANARY_7ad1";
  let path;
  const { commands } = harness(async (_command, args) => {
    assert.equal(args.includes(canary), false);
    path = args[args.indexOf("--message-file") + 1];
    assert.equal((await stat(path)).mode & 0o777, 0o600);
    assert.equal(await readFile(path, "utf8"), canary);
    return {
      code: 0,
      stdout: JSON.stringify(success("send", { session: "pi-test", role: "reviewer", sent: true })),
    };
  });
  const ctx = context({ input: "pi-test", selection: "reviewer", editor: canary });
  await commands.get("orchestrator-send").handler("", ctx);
  assert.deepEqual(ctx.calls.selections[0].options, ["implementer", "reviewer", "probe", "playwright", "django"]);
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
  await commands.get("orchestrator-send").handler("pi-test", ctx);
  assert.equal(ctx.calls.notifications.at(-1).message, "Unable to send orchestration message");
  assert.equal(JSON.stringify(ctx.calls).includes(message), false);
  await assert.rejects(access(path));
});

test("stop obtains an exact session and requires explicit UI confirmation before --yes", async () => {
  const argvs = [];
  const { commands } = harness(async (_command, args) => {
    argvs.push(args);
    return { code: 0, stdout: JSON.stringify(success("stop", { session: "pi-test", stopped: true })) };
  });
  const ctx = context({ input: "pi-test", confirmations: [true] });
  await commands.get("orchestrator-stop").handler("", ctx);
  assert.deepEqual(argvs[0].slice(1), ["--json", "stop", "pi-test", "--yes"]);
  assert.equal(ctx.calls.inputs.length, 1);
  assert.match(ctx.calls.confirmations[0].message, /retained/);

  const declinedCtx = context({ confirmations: [false] });
  await commands.get("orchestrator-stop").handler("pi-other", declinedCtx);
  assert.equal(argvs.length, 1);
});
