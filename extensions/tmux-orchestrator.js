import { chmod, mkdtemp, realpath, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";

const CLI_PATH = fileURLToPath(new URL("../scripts/pi-tmux-agents.py", import.meta.url));
const STATUS_KEY = "tmux-orchestrator";
const MAX_VISIBLE_CHARS = 12_000;
const ACTIONS = ["doctor", "list", "status", "start", "send"];
const ROLES = ["implementer", "reviewer", "probe", "playwright", "django"];
const COMMAND_OVERVIEW = [
  "/orchestrator-help — show this bounded command overview",
  "/orchestrator-doctor — check local prerequisites without a provider request",
  "/orchestrator-start [task] — confirm and start an orchestration",
  "/orchestrator-list — list orchestrations and refresh the metadata-only widget",
  "/orchestrator-status [session] — show metadata-only status",
  "/orchestrator-send [session] — privately send a message to one role",
  "/orchestrator-stop [session] — confirm and stop one exact session",
  "/orchestrate — backward-compatible alias for /orchestrator-start",
  "/orchestrations — backward-compatible alias for /orchestrator-list",
  "Attach and restart remain terminal-only: attach takes over the terminal; restart requires explicit CLI confirmation and configuration.",
].join("\n");

const parameters = {
  type: "object",
  additionalProperties: false,
  required: ["action"],
  properties: {
    action: { type: "string", enum: ACTIONS },
    project: { type: "string", description: "Project path for start; defaults to the current project" },
    session: { type: "string", description: "Exact orchestration session for status or send" },
    role: {
      type: "string",
      enum: ROLES,
      description: "Target role for send",
    },
    task: { type: "string", maxLength: 65536, description: "Start task; transferred through a private file" },
    message: { type: "string", maxLength: 65536, description: "Send message; transferred through a private file" },
    withProbe: { type: "boolean" },
    probeTask: { type: "string", maxLength: 65536 },
    withPlaywright: { type: "boolean" },
    playwrightTask: { type: "string", maxLength: 65536 },
    withDjangoExpert: { type: "boolean" },
    djangoTask: { type: "string", maxLength: 65536 },
    approveProject: {
      type: "boolean",
      description: "Request child --approve; allowed only after parent trust and explicit per-run confirmation",
    },
  },
};

function bounded(value, limit = MAX_VISIBLE_CHARS) {
  const text = String(value ?? "").replace(/[\u0000-\u001f\u007f]+/g, " ").replace(/\s+/g, " ").trim();
  return text.length <= limit ? text : `${text.slice(0, Math.max(0, limit - 1)).trimEnd()}…`;
}

function isControllerMode() {
  return process.env.PI_TMUX_CONTROLLER === "1";
}

function oneLineJson(stdout) {
  if (Buffer.byteLength(stdout, "utf8") > 256 * 1024) {
    throw new Error("orchestrator_output_too_large");
  }
  let envelope;
  try {
    envelope = JSON.parse(stdout);
  } catch {
    throw new Error("invalid_orchestrator_json");
  }
  if (
    !envelope ||
    typeof envelope !== "object" ||
    envelope.schema_version !== "1" ||
    typeof envelope.command !== "string" ||
    typeof envelope.success !== "boolean" ||
    !("data" in envelope) ||
    !("error" in envelope)
  ) {
    throw new Error("invalid_orchestrator_envelope");
  }
  return envelope;
}

async function runCli(pi, action, args = [], signal) {
  const result = await pi.exec("python3", [CLI_PATH, "--json", action, ...args], {
    signal,
    timeout: 30_000,
  });
  const envelope = oneLineJson(result.stdout || "");
  if (envelope.command !== action || (result.code === 0) !== envelope.success) {
    throw new Error("orchestrator_result_mismatch");
  }
  return envelope;
}

async function withPrivateFiles(values, callback) {
  const directory = await mkdtemp(join(tmpdir(), "pi-tmux-orchestrator-"));
  await chmod(directory, 0o700);
  const paths = {};
  try {
    for (const [name, value] of Object.entries(values)) {
      if (value === undefined || value === null) continue;
      const path = join(directory, `${name}-${randomUUID()}.txt`);
      await writeFile(path, String(value), { encoding: "utf8", mode: 0o600, flag: "wx" });
      paths[name] = path;
    }
    return await callback(paths);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

async function canonicalProject(project, cwd) {
  const candidate = resolve(cwd, project || cwd);
  const metadata = await stat(candidate);
  if (!metadata.isDirectory()) throw new Error("project_not_directory");
  return realpath(candidate);
}

function startFileValues(input) {
  return {
    task: input.task,
    probe: input.probeTask,
    playwright: input.playwrightTask,
    django: input.djangoTask,
  };
}

function buildStartArgs(input, project, paths, { dryRun = false } = {}) {
  const args = ["--project", project, "--task-file", paths.task];
  if (input.withProbe) args.push("--with-probe");
  if (paths.probe) args.push("--probe-task-file", paths.probe);
  if (input.withPlaywright) args.push("--with-playwright");
  if (paths.playwright) args.push("--playwright-task-file", paths.playwright);
  if (input.withDjangoExpert) args.push("--with-django-expert");
  if (paths.django) args.push("--django-task-file", paths.django);
  if (input.approveProject) args.push("--approve-project");
  if (dryRun) args.push("--dry-run", "--skip-model-check");
  return args;
}

function startConfirmation(preview) {
  const roles = (preview.data?.roles || [])
    .map((role) => `${role.name}: ${role.provider}/${role.model} (${role.thinking})`)
    .join("\n");
  const trust = preview.data?.trust?.child_bypass
    ? "Child --approve requested after a separate confirmation"
    : "Native child trust prompts (parent trust is not inherited)";
  return [
    `Project: ${preview.data?.project}`,
    `Session: ${preview.data?.session}`,
    `Roles/models (CLI policy):\n${roles}`,
    `External state: ${preview.data?.paths?.state_root}`,
    "Coordination state is retained when the tmux session stops.",
    `Trust: ${trust}`,
  ].join("\n\n");
}

async function runStart(pi, input, signal, ctx) {
  if (ctx.mode !== "tui" || !ctx.hasUI) {
    throw new Error("start_requires_interactive_tui_confirmation");
  }
  if (!input.task || !String(input.task).trim()) throw new Error("start_requires_task");
  if (input.probeTask && !input.withProbe) throw new Error("probe_task_requires_role");
  if (input.playwrightTask && !input.withPlaywright) throw new Error("playwright_task_requires_role");
  if (input.djangoTask && !input.withDjangoExpert) throw new Error("django_task_requires_role");
  if (isControllerMode() && !String(input.project || "").trim()) {
    throw new Error("controller_start_requires_explicit_project");
  }

  const project = await canonicalProject(input.project, ctx.cwd);
  if (input.approveProject) {
    if (!ctx.isProjectTrusted()) throw new Error("approve_requires_trusted_parent_project");
    const bypassConfirmed = await ctx.ui.confirm(
      "Child project trust bypass",
      "The parent trust decision does not automatically apply to child Pi sessions. Pass --approve to every child for this run?",
    );
    if (!bypassConfirmed) throw new Error("approve_confirmation_declined");
  }

  return withPrivateFiles(startFileValues(input), async (paths) => {
    const preview = await runCli(pi, "start", buildStartArgs(input, project, paths, { dryRun: true }), signal);
    if (!preview.success) return preview;
    const confirmed = await ctx.ui.confirm("Start tmux orchestration?", startConfirmation(preview));
    if (!confirmed) throw new Error("start_confirmation_declined");
    return runCli(pi, "start", buildStartArgs(input, project, paths), signal);
  });
}

async function runSend(pi, input, signal) {
  if (!input.session || !input.role || !input.message || !String(input.message).trim()) {
    throw new Error("send_requires_session_role_message");
  }
  return withPrivateFiles({ message: input.message }, (paths) =>
    runCli(
      pi,
      "send",
      [input.session, "--role", input.role, "--message-file", paths.message],
      signal,
    ),
  );
}

const successSummaries = {
  list(data) {
    const sessions = data.sessions || [];
    return sessions.length
      ? `${sessions.length} orchestration(s): ${sessions.map((item) => item.session).join(", ")}`
      : "No running orchestrations.";
  },
  status(data) {
    return `${data.session}: ${data.roles?.length || 0} roles, ${data.panes?.length || 0} panes, ${data.files?.length || 0} status files`;
  },
  start(data) {
    return data.dry_run ? `Validated ${data.session}` : `Started ${data.session}`;
  },
  send(data) {
    return `Sent to ${data.session}/${data.role}`;
  },
  doctor(data) {
    const failed = (data.commands || []).filter((item) => item.status === "fail").length;
    return failed ? `${failed} prerequisite check(s) failed` : "Prerequisite checks complete";
  },
};

function compactSummary(envelope) {
  if (!envelope.success) {
    return `Failed (${bounded(envelope.error?.code, 80)}): ${bounded(envelope.error?.message, 400)}`;
  }
  const summarize = successSummaries[envelope.command];
  return summarize ? summarize(envelope.data || {}) : `${envelope.command} complete`;
}

function safeDetails(envelope) {
  const serialized = JSON.stringify(envelope);
  if (serialized.length <= MAX_VISIBLE_CHARS) return envelope;
  return {
    schema_version: envelope.schema_version,
    command: envelope.command,
    success: envelope.success,
    truncated: true,
  };
}

function notifyEnvelope(ctx, envelope) {
  const message = bounded(compactSummary(envelope), 800);
  ctx.ui.notify(message, envelope.success ? "info" : "error");
  ctx.ui.setStatus(STATUS_KEY, envelope.success ? `tmux: ${envelope.command}` : "tmux: error");
  return message;
}

async function executeAction(pi, input, signal, ctx) {
  ctx.ui.setStatus(STATUS_KEY, `tmux: ${input.action}…`);
  let envelope;
  try {
    switch (input.action) {
      case "doctor":
      case "list":
        envelope = await runCli(pi, input.action, [], signal);
        break;
      case "status":
        envelope = await runCli(pi, "status", input.session ? [input.session] : [], signal);
        break;
      case "start":
        envelope = await runStart(pi, input, signal, ctx);
        break;
      case "send":
        envelope = await runSend(pi, input, signal);
        break;
      default:
        throw new Error("unsupported_action");
    }
    const summary = notifyEnvelope(ctx, envelope);
    return {
      content: [{ type: "text", text: bounded(summary, 800) }],
      details: safeDetails(envelope),
    };
  } catch (error) {
    ctx.ui.setStatus(STATUS_KEY, "tmux: error");
    throw new Error(bounded(error instanceof Error ? error.message : "orchestrator_error", 200));
  }
}

function requireInteractiveTui(ctx, command) {
  if (ctx.mode === "tui" && ctx.hasUI) return true;
  ctx.ui.notify(`/${command} requires the interactive TUI`, "error");
  return false;
}

function notifyCommandFailure(ctx, action) {
  const labels = {
    doctor: "run orchestrator doctor",
    list: "list orchestrations",
    status: "show orchestration status",
    start: "start orchestration",
    send: "send orchestration message",
    stop: "stop orchestration",
  };
  ctx.ui.setStatus(STATUS_KEY, "tmux: error");
  ctx.ui.notify(`Unable to ${labels[action] || "run orchestrator command"}`, "error");
}

async function runCommandCli(pi, action, args, ctx) {
  try {
    const envelope = await runCli(pi, action, args, ctx.signal);
    notifyEnvelope(ctx, envelope);
    return envelope;
  } catch {
    notifyCommandFailure(ctx, action);
    return undefined;
  }
}

function refreshListWidget(ctx, envelope) {
  const sessions = Array.isArray(envelope?.data?.sessions) ? envelope.data.sessions : [];
  ctx.ui.setWidget(
    STATUS_KEY,
    sessions.length
      ? sessions.slice(0, 8).map((item) => bounded(`${item.session} · ${item.project || "invalid"}`, 240))
      : [bounded(compactSummary(envelope), 800)],
  );
}

function createCommandHandlers(pi) {
  const help = async (_args, ctx) => {
    ctx.ui.notify(bounded(COMMAND_OVERVIEW, 2400), "info");
  };

  const doctor = async (_args, ctx) => {
    await runCommandCli(pi, "doctor", [], ctx);
  };

  const start = async (args, ctx) => {
    if (!requireInteractiveTui(ctx, "orchestrator-start")) return;
    const task = String(args || "").trim() || await ctx.ui.editor("Orchestration task", "");
    if (!task?.trim()) return;
    let project;
    if (isControllerMode()) {
      project = await ctx.ui.input("Target project directory", "/absolute/path/to/project");
      if (!String(project || "").trim()) return;
    }
    const withProbe = await ctx.ui.confirm("Optional role", "Add the independent technical probe?");
    const withPlaywright = await ctx.ui.confirm("Optional role", "Add the read-only Playwright tester?");
    const withDjangoExpert = await ctx.ui.confirm("Optional role", "Add the read-only Django expert?");
    let approveProject = false;
    if (ctx.isProjectTrusted()) {
      approveProject = await ctx.ui.confirm(
        "Child trust policy",
        "Request a separately confirmed --approve bypass for child Pi sessions? No keeps native child trust prompts.",
      );
    }
    try {
      const envelope = await runStart(
        pi,
        { task, project, withProbe, withPlaywright, withDjangoExpert, approveProject },
        ctx.signal,
        ctx,
      );
      notifyEnvelope(ctx, envelope);
    } catch {
      notifyCommandFailure(ctx, "start");
    }
  };

  const list = async (_args, ctx) => {
    const envelope = await runCommandCli(pi, "list", [], ctx);
    if (envelope) refreshListWidget(ctx, envelope);
  };

  const status = async (args, ctx) => {
    const session = String(args || "").trim();
    await runCommandCli(pi, "status", session ? [session] : [], ctx);
  };

  const send = async (args, ctx) => {
    if (!requireInteractiveTui(ctx, "orchestrator-send")) return;
    const suppliedSession = String(args || "").trim();
    const enteredSession = suppliedSession || await ctx.ui.input("Exact orchestration session", "pi-project-agents");
    const session = String(enteredSession || "").trim();
    if (!session) return;
    const role = await ctx.ui.select("Target role", [...ROLES]);
    if (!ROLES.includes(role)) return;
    const message = await ctx.ui.editor(`Message to ${session}/${role}`, "");
    if (!message?.trim()) return;
    try {
      const envelope = await runSend(pi, { session, role, message }, ctx.signal);
      notifyEnvelope(ctx, envelope);
    } catch {
      notifyCommandFailure(ctx, "send");
    }
  };

  const stop = async (args, ctx) => {
    if (!requireInteractiveTui(ctx, "orchestrator-stop")) return;
    const suppliedSession = String(args || "").trim();
    const enteredSession = suppliedSession || await ctx.ui.input("Exact orchestration session", "pi-project-agents");
    const session = String(enteredSession || "").trim();
    if (!session) return;
    const confirmed = await ctx.ui.confirm(
      "Stop tmux orchestration?",
      `Kill only ${bounded(session, 160)}? External coordination state and child session records are retained.`,
    );
    if (!confirmed) return;
    await runCommandCli(pi, "stop", [session, "--yes"], ctx);
  };

  return { help, doctor, start, list, status, send, stop };
}

export default function tmuxOrchestratorExtension(pi) {
  pi.registerTool({
    name: "tmux_orchestrator",
    label: "Tmux Orchestrator",
    description: "Delegate bounded doctor, list, status, start, or send actions to the bundled Python tmux orchestrator. Start always requires interactive confirmation. Output is metadata-only and bounded.",
    promptSnippet: "Inspect or operate local Pi tmux orchestrations through the authoritative Python CLI",
    promptGuidelines: [
      "Use tmux_orchestrator instead of rebuilding tmux orchestration state; never claim parent project trust automatically applies to child Pi sessions.",
    ],
    parameters,
    execute(_toolCallId, input, signal, _onUpdate, ctx) {
      return executeAction(pi, input, signal, ctx);
    },
  });

  const commandHandlers = createCommandHandlers(pi);
  pi.registerCommand("orchestrator-help", {
    description: "Show the bounded tmux orchestrator command overview",
    handler: commandHandlers.help,
  });
  pi.registerCommand("orchestrator-doctor", {
    description: "Check local tmux orchestrator prerequisites",
    handler: commandHandlers.doctor,
  });
  pi.registerCommand("orchestrator-start", {
    description: "Confirm and start a tmux orchestration",
    handler: commandHandlers.start,
  });
  pi.registerCommand("orchestrator-list", {
    description: "List running tmux orchestrations and refresh the metadata widget",
    handler: commandHandlers.list,
  });
  pi.registerCommand("orchestrator-status", {
    description: "Show metadata-only orchestration status for an optional exact session",
    handler: commandHandlers.status,
  });
  pi.registerCommand("orchestrator-send", {
    description: "Privately send a message to one role in an exact orchestration session",
    handler: commandHandlers.send,
  });
  pi.registerCommand("orchestrator-stop", {
    description: "Confirm and stop one exact tmux orchestration session",
    handler: commandHandlers.stop,
  });
  pi.registerCommand("orchestrate", {
    description: "Alias for /orchestrator-start",
    handler: commandHandlers.start,
  });
  pi.registerCommand("orchestrations", {
    description: "Alias for /orchestrator-list",
    handler: commandHandlers.list,
  });

  pi.on("session_start", (_event, ctx) => {
    if (isControllerMode()) {
      ctx.ui.setTitle?.("Pi Tmux Orchestrator Controller");
      ctx.ui.setStatus(STATUS_KEY, "tmux: controller");
      ctx.ui.setWidget(STATUS_KEY, [
        "Dedicated orchestrator controller",
        "Use /orchestrator-list or /orchestrator-start. Target projects must be explicit.",
      ]);
      return;
    }
    ctx.ui.setStatus(STATUS_KEY, "tmux: ready");
  });
  pi.on("session_before_switch", async (_event, ctx) => {
    if (!isControllerMode()) return undefined;
    ctx.ui.notify("The controller uses one fixed persistent Pi session; stop it from the terminal to leave.", "warning");
    return { cancel: true };
  });
  pi.on("session_before_fork", async (_event, ctx) => {
    if (!isControllerMode()) return undefined;
    ctx.ui.notify("Fork and clone are disabled in the fixed controller session.", "warning");
    return { cancel: true };
  });
  pi.on("session_shutdown", (_event, ctx) => {
    ctx.ui.setStatus(STATUS_KEY, undefined);
    ctx.ui.setWidget(STATUS_KEY, undefined);
  });
}

export const testHooks = {
  CLI_PATH,
  buildStartArgs,
  canonicalProject,
  executeAction,
  isControllerMode,
  oneLineJson,
  runCli,
  runStart,
  withPrivateFiles,
};
