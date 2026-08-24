import { chmod, mkdtemp, realpath, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import {
  attachParentObserver,
  brokerFrame,
  PARENT_MESSAGE_TYPE,
  parentProgressContent,
  parentUpdateContent,
  validateObserverFrame,
} from "./orchestrator-parent.js";
import { contextCapsuleParameters, renderContextCapsule } from "./orchestrator-context.js";
import {
  appendBudgetArgs,
  budgetConfirmation,
  budgetOverrideParameters,
} from "./orchestrator-budgets.js";
import {
  appendModelArgs,
  availableThinkingLevels,
  MODEL_ROLES,
  modelCatalogContent,
  modelCatalogEnvelope,
  modelOverrideParameters,
  ROLES,
  startInputWithParentModel,
} from "./orchestrator-models.js";
import {
  scheduleOrchestratorUpdateNotice,
  showOrchestratorAbout,
} from "./orchestrator-update.js";

const CLI_PATH = fileURLToPath(new URL("../bin/pi-tmux-agents", import.meta.url));
const MAX_VISIBLE_CHARS = 12_000;
const ACTIONS = ["doctor", "models", "list", "status", "watch", "attach", "start", "send"];
const COMMAND_OVERVIEW = [
  "/orchestrator-help — show this bounded command overview",
  "/orchestrator-about — show installed/latest versions, update command, and project links",
  "/orchestrator-doctor — check local prerequisites and configured worker models without a provider request",
  "/orchestrator-models [query] — list bounded available Pi model metadata without a provider request",
  "/orchestrator-start [task] — confirm and start an orchestration",
  "/orchestrator-list — list running orchestrations",
  "/orchestrator-status [session] — show metadata-only status",
  "/orchestrator-watch [session] — subscribe this parent Pi to lifecycle and final-report updates",
  "/orchestrator-attach [session] — switch this tmux client into the live worker grid",
  "/orchestrator-send [session] — privately send a message to one role",
  "/orchestrator-stop [session] — confirm and stop one exact session",
  "Short aliases: /or-help, /or-about, /or-doctor, /or-models, /or-start, /or-list, /or-status, /or-watch, /or-attach, /or-send, /or-stop",
  "/orchestrate — backward-compatible alias for /orchestrator-start",
  "/orchestrations — backward-compatible alias for /orchestrator-list",
  "Omit [session] on status, watch, attach, send, or stop to choose from the running orchestration list.",
  "The invoking Pi is the parent supervisor; start creates no separate parent Pi, parent window, or controller.",
  "Tmux panes show live worker activity; lifecycle and structured ready/attention reports return to the invoking Pi watching the run.",
  "Attach requires the invoking Pi to run inside tmux. Use normal pane navigation; prefix then L detaches back to the same Pi without stopping workers. Native TUI panes accept direct steering, while plain RPC panes remain headless/display-only.",
  "Supervisor API reads, RPC events/abort, and restart remain terminal-only.",
].join("\n");

const parameters = {
  type: "object",
  additionalProperties: false,
  required: ["action"],
  properties: {
    action: { type: "string", enum: ACTIONS },
    query: { type: "string", maxLength: 200, description: "Optional provider/model filter for the models action" },
    project: { type: "string", description: "Project path for start; defaults to the current project" },
    session: { type: "string", description: "Exact orchestration session for status, watch, attach, or send" },
    role: {
      type: "string",
      enum: ROLES,
      description: "Target role for send",
    },
    task: { type: "string", maxLength: 65536, description: "Self-contained start objective; transferred through a private file" },
    contextCapsule: contextCapsuleParameters,
    message: { type: "string", maxLength: 65536, description: "Send message; transferred through a private file" },
    withProbe: { type: "boolean" },
    probeTask: { type: "string", maxLength: 65536 },
    withPlaywright: { type: "boolean" },
    playwrightTask: { type: "string", maxLength: 65536 },
    withDjangoExpert: { type: "boolean" },
    djangoTask: { type: "string", maxLength: 65536 },
    rpcWorkers: {
      type: "boolean",
      description: "Use plain headless RPC panes only when explicitly requested; native Pi TUI workers are the interactive default",
    },
    useParentModel: {
      type: "boolean",
      description: "For start, use this Pi session's exact current provider/model/thinking as the default for every worker role",
    },
    modelOverrides: {
      type: "object",
      additionalProperties: false,
      description: "For start, explicit user-requested all-role or per-role provider/model/thinking overrides; omitted fields retain configured defaults",
      properties: Object.fromEntries(MODEL_ROLES.map((role) => [role, modelOverrideParameters])),
    },
    budgetOverrides: budgetOverrideParameters,
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
    contextCapsule: renderContextCapsule(input.contextCapsule),
    probe: input.probeTask,
    playwright: input.playwrightTask,
    django: input.djangoTask,
  };
}

function buildStartArgs(input, project, paths, { dryRun = false } = {}) {
  const args = ["--project", project, "--task-file", paths.task];
  if (paths.contextCapsule) args.push("--context-capsule-file", paths.contextCapsule);
  if (input.withProbe) args.push("--with-probe");
  if (paths.probe) args.push("--probe-task-file", paths.probe);
  if (input.withPlaywright) args.push("--with-playwright");
  if (paths.playwright) args.push("--playwright-task-file", paths.playwright);
  if (input.withDjangoExpert) args.push("--with-django-expert");
  if (paths.django) args.push("--django-task-file", paths.django);
  if (input.approveProject) args.push("--approve-project");
  if (input.rpcWorkers) args.push("--rpc-workers");
  appendModelArgs(args, input);
  appendBudgetArgs(args, input);
  if (dryRun) args.push("--dry-run", "--skip-model-check");
  return args;
}

function startConfirmation(preview) {
  const roles = (preview.data?.roles || [])
    .map((role) => `${role.name}: ${role.provider}/${role.model} (${role.thinking})`)
    .join("\n");
  const trustPolicy = preview.data?.trust?.policy;
  const trust = preview.data?.trust?.child_bypass
    ? "Child --approve requested after a separate confirmation"
    : trustPolicy === "saved-or-global-policy"
      ? "RPC workers use saved trust or global defaultProjectTrust; ask/never ignores project executable resources without a prompt"
      : "Native child trust prompts (parent trust is not inherited)";
  return [
    `Project: ${preview.data?.project}`,
    `Session: ${preview.data?.session}`,
    `Worker transport: ${preview.data?.transport || "tui"}`,
    `Roles/models (CLI policy):\n${roles}`,
    `Effective provider-usage budget policy:\n${budgetConfirmation(preview.data?.budget_policy)}`,
    `External state: ${preview.data?.paths?.state_root}`,
    `Parent context capsule: ${preview.data?.context_capsule?.present ? `${preview.data.context_capsule.chars} characters` : "not supplied"}`,
    "Metadata-only broker state and Pi sessions are retained when tmux stops; workflow payloads are not stored in coordination files.",
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

  const startInput = startInputWithParentModel(input, ctx);
  const project = await canonicalProject(startInput.project, ctx.cwd);
  if (startInput.approveProject) {
    if (!ctx.isProjectTrusted()) throw new Error("approve_requires_trusted_parent_project");
    const bypassConfirmed = await ctx.ui.confirm(
      "Child project trust bypass",
      "The parent trust decision does not automatically apply to child Pi sessions. Pass --approve to every child for this run?",
    );
    if (!bypassConfirmed) throw new Error("approve_confirmation_declined");
  }

  return withPrivateFiles(startFileValues(startInput), async (paths) => {
    const preview = await runCli(pi, "start", buildStartArgs(startInput, project, paths, { dryRun: true }), signal);
    if (!preview.success) return preview;
    const confirmed = await ctx.ui.confirm("Start tmux orchestration?", startConfirmation(preview));
    if (!confirmed) throw new Error("start_confirmation_declined");
    return runCli(pi, "start", buildStartArgs(startInput, project, paths), signal);
  });
}

function requireAttachContext(ctx) {
  if (ctx.mode !== "tui" || !ctx.hasUI) {
    throw new Error("attach_requires_interactive_tui");
  }
  if (!process.env.TMUX) throw new Error("attach_requires_parent_tmux");
}

async function runAttach(pi, input, signal, ctx) {
  requireAttachContext(ctx);
  const session = String(input.session || "").trim();
  ctx.ui.notify(
    "Switching this client to the worker grid. Prefix then L detaches back to this Pi without stopping the orchestration.",
    "info",
  );
  return runCli(pi, "attach", session ? [session] : [], signal);
}

async function attachAndSupervise(pi, input, signal, ctx, superviseStart) {
  requireAttachContext(ctx);
  const session = String(input.session || "").trim();
  const statusEnvelope = await runCli(pi, "status", session ? [session] : [], signal);
  if (!statusEnvelope.success) return statusEnvelope;
  await superviseStart(statusEnvelope);
  return runAttach(pi, { session: statusEnvelope.data?.session }, signal, ctx);
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
  models(data) {
    return `${data.shown}/${data.total} available model(s)${data.query ? ` matching ${data.query}` : ""}${data.truncated ? "; refine the query for more" : ""}`;
  },
  list(data) {
    const sessions = data.sessions || [];
    return sessions.length
      ? `${sessions.length} orchestration(s): ${sessions.map((item) => item.session).join(", ")}`
      : "No running orchestrations.";
  },
  status(data) {
    const workflow = data.broker?.workflow;
    const roleStates = (data.broker?.roles || [])
      .map((item) => `${item.role}=${item.state}`)
      .join(", ");
    const state = workflow
      ? `workflow=${workflow.state} round=${workflow.round}${roleStates ? `; ${roleStates}` : ""}`
      : `${data.files?.length || 0} legacy status files`;
    return `${data.session}: ${state}; ${data.roles?.length || 0} roles, ${data.panes?.length || 0} panes`;
  },
  watch(data) {
    const workflow = data.broker?.workflow;
    const state = workflow ? ` Current workflow: ${workflow.state}, round ${workflow.round}.` : "";
    return `This invoking Pi is watching ${data.session} for lifecycle and final-report updates.${state}`;
  },
  attach(data) {
    return `Switched to ${data.session}. ${data.return_hint || "Use tmux session navigation to return."}`;
  },
  start(data) {
    return data.dry_run
      ? `Validated ${data.session}`
      : `Started detached ${data.session} with ${data.transport === "rpc" ? "headless RPC" : "native Pi TUI"} workers. This invoking Pi remains the parent; use /orchestrator-attach ${data.session} to enter the grid.`;
  },
  send(data) {
    return data.acknowledged
      ? `Acknowledged by ${data.session}/${data.role}`
      : `Sent to ${data.session}/${data.role}`;
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
  return message;
}

async function executeAction(pi, input, signal, ctx, superviseStart = () => {}) {
  let envelope;
  try {
    switch (input.action) {
      case "models":
        envelope = modelCatalogEnvelope(ctx, input.query);
        break;
      case "doctor":
      case "list":
        envelope = await runCli(pi, input.action, [], signal);
        break;
      case "status":
        envelope = await runCli(pi, "status", input.session ? [input.session] : [], signal);
        break;
      case "watch": {
        const session = String(input.session || "").trim();
        const statusEnvelope = await runCli(pi, "status", session ? [session] : [], signal);
        if (!statusEnvelope.success) {
          envelope = statusEnvelope;
          break;
        }
        await superviseStart(statusEnvelope);
        envelope = { ...statusEnvelope, command: "watch" };
        break;
      }
      case "attach":
        envelope = await attachAndSupervise(
          pi,
          input,
          signal,
          ctx,
          superviseStart,
        );
        break;
      case "start":
        envelope = await runStart(pi, input, signal, ctx);
        if (envelope.success && !envelope.data?.dry_run) {
          void Promise.resolve(superviseStart(envelope)).catch(() => {});
        }
        break;
      case "send":
        envelope = await runSend(pi, input, signal);
        break;
      default:
        throw new Error("unsupported_action");
    }
    const summary = notifyEnvelope(ctx, envelope);
    return {
      content: [{
        type: "text",
        text: input.action === "models" ? modelCatalogContent(envelope.data) : bounded(summary, 800),
      }],
      details: safeDetails(envelope),
    };
  } catch (error) {
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
    watch: "watch orchestration updates",
    attach: "attach to the orchestration grid",
    start: "start orchestration",
    send: "send orchestration message",
    stop: "stop orchestration",
  };
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

async function requestedSession(pi, args, ctx) {
  const supplied = String(args || "").trim();
  if (supplied) return supplied;

  const envelope = await runCli(pi, "list", [], ctx.signal);
  if (!envelope.success) {
    notifyEnvelope(ctx, envelope);
    return undefined;
  }
  if (!Array.isArray(envelope.data?.sessions)) throw new Error("invalid_orchestrator_list");
  const sessions = envelope.data.sessions.filter(
    (item) => item?.valid === true && typeof item.session === "string" && item.session,
  );
  if (!sessions.length) {
    ctx.ui.notify("No running orchestrations are available.", "info");
    return undefined;
  }

  const choices = sessions.map((item) => {
    const project = typeof item.project === "string" ? bounded(item.project, 160) : "unknown project";
    return `${item.session} · ${project}`;
  });
  const selected = await ctx.ui.select("Select a running orchestration", choices);
  const index = choices.indexOf(selected);
  return index >= 0 ? sessions[index].session : undefined;
}

function createCommandHandlers(pi, superviseStart = () => {}) {
  const help = async (_args, ctx) => {
    ctx.ui.notify(bounded(COMMAND_OVERVIEW, 2400), "info");
  };

  const about = async (_args, ctx) => {
    if (!requireInteractiveTui(ctx, "orchestrator-about")) return;
    await showOrchestratorAbout(ctx);
  };

  const doctor = async (_args, ctx) => {
    await runCommandCli(pi, "doctor", [], ctx);
  };

  const models = async (args, ctx) => {
    notifyEnvelope(ctx, modelCatalogEnvelope(ctx, args));
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
    const rpcWorkers = false;
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
        {
          task,
          project,
          withProbe,
          withPlaywright,
          withDjangoExpert,
          rpcWorkers,
          approveProject,
        },
        ctx.signal,
        ctx,
      );
      if (envelope.success && !envelope.data?.dry_run) {
        void Promise.resolve(superviseStart(envelope)).catch(() => {});
      }
      notifyEnvelope(ctx, envelope);
    } catch {
      notifyCommandFailure(ctx, "start");
    }
  };

  const list = async (_args, ctx) => {
    await runCommandCli(pi, "list", [], ctx);
  };

  const status = async (args, ctx) => {
    if (!requireInteractiveTui(ctx, "orchestrator-status")) return;
    try {
      const session = await requestedSession(pi, args, ctx);
      if (!session) return;
      await runCommandCli(pi, "status", [session], ctx);
    } catch {
      notifyCommandFailure(ctx, "status");
    }
  };

  const watch = async (args, ctx) => {
    if (!requireInteractiveTui(ctx, "orchestrator-watch")) return;
    let session;
    try {
      session = await requestedSession(pi, args, ctx);
    } catch {
      notifyCommandFailure(ctx, "watch");
      return;
    }
    if (!session) return;
    try {
      const statusEnvelope = await runCli(pi, "status", [session], ctx.signal);
      if (!statusEnvelope.success) {
        notifyEnvelope(ctx, statusEnvelope);
        return;
      }
      await superviseStart(statusEnvelope);
      notifyEnvelope(ctx, { ...statusEnvelope, command: "watch" });
    } catch {
      notifyCommandFailure(ctx, "watch");
    }
  };

  const attach = async (args, ctx) => {
    if (!requireInteractiveTui(ctx, "orchestrator-attach")) return;
    try {
      const session = await requestedSession(pi, args, ctx);
      if (!session) return;
      const envelope = await attachAndSupervise(
        pi,
        { session },
        ctx.signal,
        ctx,
        superviseStart,
      );
      notifyEnvelope(ctx, envelope);
    } catch {
      notifyCommandFailure(ctx, "attach");
    }
  };

  const send = async (args, ctx) => {
    if (!requireInteractiveTui(ctx, "orchestrator-send")) return;
    let session;
    try {
      session = await requestedSession(pi, args, ctx);
    } catch {
      notifyCommandFailure(ctx, "send");
      return;
    }
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
    let session;
    try {
      session = await requestedSession(pi, args, ctx);
    } catch {
      notifyCommandFailure(ctx, "stop");
      return;
    }
    if (!session) return;
    const confirmed = await ctx.ui.confirm(
      "Stop tmux orchestration?",
      `Kill only ${bounded(session, 160)}? External coordination state and child session records are retained.`,
    );
    if (!confirmed) return;
    await runCommandCli(pi, "stop", [session, "--yes"], ctx);
  };

  return { help, about, doctor, models, start, list, status, watch, attach, send, stop };
}

export default function tmuxOrchestratorExtension(pi) {
  const observers = new Map();
  let shuttingDown = false;

  async function superviseStart(envelope) {
    const coordination = envelope.data?.paths?.coordination;
    const session = envelope.data?.session;
    if (typeof coordination !== "string" || !coordination || typeof session !== "string" || !session) {
      throw new Error("observer_paths_unavailable");
    }
    if (shuttingDown) throw new Error("observer_session_shutting_down");
    if (observers.has(coordination)) return { session, status: "already_watching" };
    const observer = { closed: false, socket: undefined, timer: undefined, stop: undefined };
    observer.stop = () => {
      if (observer.closed) return;
      observer.closed = true;
      if (observer.timer) clearTimeout(observer.timer);
      if (observer.socket) observer.socket.destroy();
      observers.delete(coordination);
    };
    observers.set(coordination, observer);
    try {
      const attached = await attachParentObserver(
        pi,
        envelope,
        observer,
        () => observers.delete(coordination),
      );
      await attached.ready;
      return { session, status: "watching" };
    } catch (error) {
      observers.delete(coordination);
      if (shuttingDown || observer.closed) throw error;
      observer.closed = true;
      const update = parentUpdateContent(session, "uncertain", null, []);
      try {
        pi.sendMessage(
          {
            customType: PARENT_MESSAGE_TYPE,
            content: update.content,
            display: true,
            details: {
              session,
              state: "uncertain",
              round: null,
              report_roles: [],
              omitted_reports: 0,
            },
          },
          { triggerTurn: true, deliverAs: "steer" },
        );
      } catch {
        // The parent session may already be shutting down.
      }
      throw error;
    }
  }

  pi.registerTool({
    name: "tmux_orchestrator",
    label: "Tmux Orchestrator",
    description: "Supervise bounded doctor, available-model discovery, list, status, watch, attach, start, or send actions through the Pi runtime and bundled Python tmux orchestrator. Start may use user-configured defaults, this parent Pi's current model, exact user-requested per-role provider/model/thinking overrides, and strict per-run budget overrides. The invoking Pi remains the parent; normal starts create no separate parent Pi or controller. Watch subscribes this Pi to lifecycle and final-report updates. Attach ensures watching, then switches its existing tmux client into native Pi worker panes; prefix then L returns without stopping workers. New runs are watched automatically. Start always requires interactive confirmation.",
    promptSnippet: "Inspect or operate local Pi tmux orchestrations through the authoritative Python CLI",
    promptGuidelines: [
      "Use tmux_orchestrator instead of rebuilding tmux orchestration state; before a start, synthesize a bounded contextCapsule from the current conversation when prior decisions or work matter; include only task-relevant state, constraints, acceptance criteria, paths, evidence, and open questions, never the full transcript. After starting or resuming an existing run, ensure the invoking Pi is watching it for lifecycle and final reports. Once watching, end the turn and rely on broker updates: never run sleep commands or repeatedly poll status/tmux while waiting for a watched orchestration. Honor explicit user model/provider/thinking requests through useParentModel or modelOverrides. Use the models action to resolve available exact identifiers when needed; never invent a provider/model identifier or read provider credentials. Omitted overrides use the user's global orchestrator model configuration, then packaged defaults. Honor explicit per-run budget requests through budgetOverrides; omitted values use the strict user-global budget policy and packaged warn-only defaults, and never infer hard thresholds. When the user asks to enter, navigate, or directly steer the live workers, use attach rather than watch; attach requires the invoking Pi to be inside tmux. Prefer native Pi TUI workers and use rpcWorkers only after an explicit request for headless panes. The invoking Pi remains responsible for interpreting reports and deciding follow-up. Never create file handoffs, poll coordination state, claim parent project trust applies to child Pi sessions, or equate command acknowledgement with task completion.",
    ],
    parameters,
    execute(_toolCallId, input, signal, _onUpdate, ctx) {
      return executeAction(pi, input, signal, ctx, superviseStart);
    },
  });

  const commandHandlers = createCommandHandlers(pi, superviseStart);
  pi.registerCommand("orchestrator-help", {
    description: "Show the bounded tmux orchestrator command overview",
    handler: commandHandlers.help,
  });
  pi.registerCommand("orchestrator-about", {
    description: "Show installed and latest versions, update guidance, and project links",
    handler: commandHandlers.about,
  });
  pi.registerCommand("orchestrator-doctor", {
    description: "Check local tmux orchestrator prerequisites and configured models",
    handler: commandHandlers.doctor,
  });
  pi.registerCommand("orchestrator-models", {
    description: "List bounded available Pi model metadata with an optional query",
    handler: commandHandlers.models,
  });
  pi.registerCommand("orchestrator-start", {
    description: "Confirm and start a tmux orchestration",
    handler: commandHandlers.start,
  });
  pi.registerCommand("orchestrator-list", {
    description: "List running tmux orchestrations",
    handler: commandHandlers.list,
  });
  pi.registerCommand("orchestrator-status", {
    description: "Show metadata-only orchestration status for an optional exact session",
    handler: commandHandlers.status,
  });
  pi.registerCommand("orchestrator-watch", {
    description: "Subscribe this invoking Pi to lifecycle and final-report updates for an orchestration",
    handler: commandHandlers.watch,
  });
  pi.registerCommand("orchestrator-attach", {
    description: "Switch this tmux client into the live worker grid for navigation and steering",
    handler: commandHandlers.attach,
  });
  pi.registerCommand("orchestrator-send", {
    description: "Privately send a message to one role in an exact orchestration session",
    handler: commandHandlers.send,
  });
  pi.registerCommand("orchestrator-stop", {
    description: "Confirm and stop one exact tmux orchestration session",
    handler: commandHandlers.stop,
  });
  const shortAliases = {
    "or-help": ["Show the tmux orchestrator command overview", commandHandlers.help],
    "or-about": ["Show version and update details", commandHandlers.about],
    "or-doctor": ["Check prerequisites and configured models", commandHandlers.doctor],
    "or-models": ["List available Pi model metadata", commandHandlers.models],
    "or-start": ["Confirm and start a tmux orchestration", commandHandlers.start],
    "or-list": ["List running tmux orchestrations", commandHandlers.list],
    "or-status": ["Show metadata-only orchestration status", commandHandlers.status],
    "or-watch": ["Subscribe this Pi to orchestration updates", commandHandlers.watch],
    "or-attach": ["Enter a live worker grid", commandHandlers.attach],
    "or-send": ["Send a private message to one orchestration role", commandHandlers.send],
    "or-stop": ["Confirm and stop one orchestration", commandHandlers.stop],
  };
  for (const [name, [description, handler]] of Object.entries(shortAliases)) {
    pi.registerCommand(name, { description, handler });
  }
  pi.registerCommand("orchestrate", {
    description: "Alias for /orchestrator-start",
    handler: commandHandlers.start,
  });
  pi.registerCommand("orchestrations", {
    description: "Alias for /orchestrator-list",
    handler: commandHandlers.list,
  });

  pi.on("session_start", (_event, ctx) => {
    scheduleOrchestratorUpdateNotice(ctx);
  });
  pi.on("session_shutdown", () => {
    shuttingDown = true;
    for (const observer of observers.values()) observer.stop();
    observers.clear();
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
}

export const testHooks = {
  CLI_PATH,
  availableThinkingLevels,
  buildStartArgs,
  canonicalProject,
  createCommandHandlers,
  executeAction,
  isControllerMode,
  modelCatalogEnvelope,
  oneLineJson,
  runCli,
  requestedSession,
  renderContextCapsule,
  attachAndSupervise,
  attachParentObserver,
  brokerFrame,
  parentProgressContent,
  parentUpdateContent,
  runAttach,
  runStart,
  startInputWithParentModel,
  validateObserverFrame,
  withPrivateFiles,
};
