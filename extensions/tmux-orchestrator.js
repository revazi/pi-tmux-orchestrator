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
  getOrchestratorAboutSummary,
  scheduleOrchestratorUpdateNotice,
} from "./orchestrator-update.js";
import { showOrchestrationDashboard } from "./orchestrator-dashboard.js";

const CLI_PATH = fileURLToPath(new URL("../bin/pi-tmux-agents", import.meta.url));
const MAX_VISIBLE_CHARS = 12_000;
const ACTIONS = ["doctor", "models", "list", "status", "watch", "attach", "start", "send"];

const parameters = {
  type: "object",
  additionalProperties: false,
  required: ["action"],
  properties: {
    action: { type: "string", enum: ACTIONS },
    query: { type: "string", maxLength: 200, description: "Optional provider/model filter for the models action" },
    project: { type: "string", description: "Project path for start or doctor; defaults to the current project" },
    implementationFlow: {
      type: "string",
      enum: ["single", "phased"],
      description: "Use single for simple/compatibility work or phased for a bounded inspect/plan boundary before implementation",
    },
    profile: {
      type: "string",
      pattern: "^[a-z][a-z0-9-]{0,31}$",
      description: "Packaged or strict user-global execution profile for start",
    },
    session: { type: "string", description: "Exact orchestration session for status, watch, attach, or send" },
    role: {
      type: "string",
      enum: ROLES,
      description: "Target role for send",
    },
    task: { type: "string", maxLength: 65536, description: "Self-contained start objective; transferred through a private file" },
    contextCapsule: contextCapsuleParameters,
    workspaceCapsule: {
      type: "boolean",
      description: "Opt in to the ephemeral experimental cold-assignment workspace capsule; disabled by default",
    },
    workspaceRelevantPaths: {
      type: "array",
      maxItems: 16,
      uniqueItems: true,
      items: { type: "string", minLength: 1, maxLength: 256 },
      description: "Existing project-relative paths supplied by the parent for the experimental workspace capsule; never a repository tree",
    },
    message: { type: "string", maxLength: 65536, description: "Send message; transferred through a private file" },
    forceSpecialists: {
      type: "array",
      maxItems: 3,
      uniqueItems: true,
      items: { type: "string", enum: ["probe", "playwright", "django"] },
      description: "Enabled specialists that must run whenever applicable instead of using deterministic gates",
    },
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
    workerSkills: {
      type: "object",
      additionalProperties: false,
      description: "Explicitly reviewed Markdown skill paths to load for individual worker roles; skill discovery stays disabled",
      properties: Object.fromEntries(ROLES.map((role) => [role, {
        type: "array",
        maxItems: 8,
        items: { type: "string", minLength: 1, maxLength: 1024 },
      }])),
    },
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

function requestedProjectPath(project, cwd) {
  return resolve(cwd, project || cwd);
}

function requireCanonicalWorkspaceProject(candidate, canonical, required) {
  if (required && canonical !== candidate) {
    throw new Error("workspace_capsule_project_not_canonical");
  }
}

async function canonicalProject(project, cwd, { requireCanonical = false } = {}) {
  const candidate = requestedProjectPath(project, cwd);
  const metadata = await stat(candidate);
  if (!metadata.isDirectory()) throw new Error("project_not_directory");
  const canonical = await realpath(candidate);
  requireCanonicalWorkspaceProject(candidate, canonical, requireCanonical);
  return canonical;
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

function appendForcedSpecialistArgs(args, values = []) {
  for (const role of values) args.push("--force-specialist", role);
}

function appendWorkspaceCapsuleArgs(args, input) {
  const selection = new Map([
    [true, "--workspace-capsule"],
    [false, "--no-workspace-capsule"],
  ]).get(input.workspaceCapsule);
  if (selection) args.push(selection);
  args.push(...(input.workspaceRelevantPaths ?? []).flatMap((path) => ["--workspace-relevant-path", path]));
}

function appendSpecialistSelection(args, input, field, enabledFlag, disabledFlag) {
  if (input[field] === true) args.push(enabledFlag);
  if (input[field] === false) args.push(disabledFlag);
}

function buildStartArgs(input, project, paths, { dryRun = false } = {}) {
  const args = ["--project", project, "--task-file", paths.task];
  if (paths.contextCapsule) args.push("--context-capsule-file", paths.contextCapsule);
  appendSpecialistSelection(args, input, "withProbe", "--with-probe", "--without-probe");
  if (paths.probe) args.push("--probe-task-file", paths.probe);
  appendSpecialistSelection(args, input, "withPlaywright", "--with-playwright", "--without-playwright");
  if (paths.playwright) args.push("--playwright-task-file", paths.playwright);
  appendSpecialistSelection(args, input, "withDjangoExpert", "--with-django-expert", "--without-django-expert");
  if (paths.django) args.push("--django-task-file", paths.django);
  if (input.approveProject) args.push("--approve-project");
  if (input.rpcWorkers) args.push("--rpc-workers");
  appendWorkspaceCapsuleArgs(args, input);
  if (input.implementationFlow) args.push("--implementation-flow", input.implementationFlow);
  appendForcedSpecialistArgs(args, input.forceSpecialists);
  if (input.profile) args.push("--profile", input.profile);
  appendModelArgs(args, input);
  appendBudgetArgs(args, input);
  for (const role of ROLES) {
    for (const path of input.workerSkills?.[role] || []) {
      args.push("--worker-skill", `${role}=${path}`);
    }
  }
  if (dryRun) args.push("--dry-run", "--skip-model-check");
  return args;
}

function executionProfileConfirmation(data) {
  const profile = data?.execution_profile;
  if (!profile) return "unavailable (unknown, source=unknown)";
  return `${profile.name} (${profile.kind}, source=${profile.source})`;
}

function forcedSpecialistsConfirmation(data) {
  if (!Array.isArray(data?.forced_specialists)) return "none";
  if (!data.forced_specialists.length) return "none";
  return data.forced_specialists.join(", ");
}

function workspaceCapsuleConfirmation(data) {
  const capsule = data?.workspace_capsule;
  if (!capsule?.enabled) return "disabled";
  return `validated schema=${capsule.schema_version}; instructions=${capsule.instruction_count}; markers=${capsule.marker_count}; relevant=${capsule.relevant_path_count}`;
}

function orchestrationConfigPath(config) {
  if (typeof config?.path !== "string") return "unavailable";
  return config.path;
}

function projectMappingLabel(config) {
  if (config?.matched !== true) return "none";
  return `matched ${config.directory}`;
}

function startConfirmation(preview) {
  const data = preview.data ?? {};
  const roles = (data.roles || [])
    .map((role) => `${role.name}: ${role.provider}/${role.model} (${role.thinking})`)
    .join("\n");
  const trustPolicy = data.trust?.policy;
  const trust = data.trust?.child_bypass
    ? "Child --approve requested after a separate confirmation"
    : trustPolicy === "saved-or-global-policy"
      ? "RPC workers use saved trust or global defaultProjectTrust; ask/never ignores project executable resources without a prompt"
      : "Native child trust prompts (parent trust is not inherited)";
  const configPath = orchestrationConfigPath(data.orchestration_config);
  const projectMapping = projectMappingLabel(data.project_config);
  return [
    `Project: ${data.project}`,
    `Session: ${data.session}`,
    `Worker transport: ${data.transport || "tui"}`,
    `Implementation flow: ${data.implementation_flow || "single"}`,
    `Forced specialists: ${forcedSpecialistsConfirmation(data)}`,
    `Execution profile: ${executionProfileConfirmation(data)}`,
    `Orchestration config: ${configPath}`,
    `Project mapping: ${projectMapping}`,
    `Roles/models (CLI policy):\n${roles}`,
    `Effective provider-usage budget policy:\n${budgetConfirmation(data.budget_policy)}`,
    `Worker skills (automatic discovery disabled):\n${Object.entries(data.worker_resources?.skills || {}).map(([role, paths]) => `${role}: ${paths.length ? paths.join(", ") : "none"}`).join("\n")}`,
    `External state: ${data.paths?.state_root}`,
    `Parent context capsule: ${data.context_capsule?.present ? `${data.context_capsule.chars} characters` : "not supplied"}`,
    `Experimental workspace capsule: ${workspaceCapsuleConfirmation(data)}`,
    "Metadata-only broker state and Pi sessions are retained when tmux stops; workflow payloads are not stored in coordination files.",
    `Trust: ${trust}`,
  ].join("\n\n");
}

async function selectForcedSpecialists(ctx, enabled) {
  const selected = [];
  for (const role of enabled) {
    const force = await ctx.ui.confirm(
      "Specialist activation",
      `Force ${role} to run whenever applicable instead of using deterministic activation gates?`,
    );
    if (force) selected.push(role);
  }
  return selected;
}

async function selectWorkspaceCapsule(ctx) {
  const enabled = await ctx.ui.confirm(
    "Experimental workspace capsule",
    "Opt in to the ephemeral cold-assignment workspace discovery experiment? It remains disabled by default and does not replace reading project instructions.",
  );
  if (!enabled) return { workspaceCapsule: false, workspaceRelevantPaths: [] };
  const suppliedPaths = await ctx.ui.editor("Workspace capsule relevant paths", "");
  const workspaceRelevantPaths = String(suppliedPaths || "")
    .split(/\r?\n/)
    .map((value) => value.trim())
    .filter(Boolean);
  return { workspaceCapsule: true, workspaceRelevantPaths };
}

async function selectRunOverrides(ctx) {
  const overrideProjectDefaults = await ctx.ui.confirm(
    "Orchestration defaults",
    "Override flow, specialist, and workspace-capsule defaults from the exact project mapping for this run? No uses the configured project/global defaults.",
  );
  if (!overrideProjectDefaults) return {};
  const phased = await ctx.ui.confirm(
    "Implementation flow",
    "Use a read-only inspect/plan phase before implementation for this complex task? No keeps the single-assignment path.",
  );
  const withProbe = await ctx.ui.confirm("Optional role", "Add the independent technical probe?");
  const withPlaywright = await ctx.ui.confirm("Optional role", "Add the read-only Playwright tester?");
  const withDjangoExpert = await ctx.ui.confirm("Optional role", "Add the read-only Django expert?");
  const enabledSpecialists = Object.entries({
    probe: withProbe,
    playwright: withPlaywright,
    django: withDjangoExpert,
  }).filter(([, enabled]) => enabled).map(([role]) => role);
  const forceSpecialists = await selectForcedSpecialists(ctx, enabledSpecialists);
  const workspace = await selectWorkspaceCapsule(ctx);
  return {
    implementationFlow: phased ? "phased" : "single",
    withProbe,
    withPlaywright,
    withDjangoExpert,
    forceSpecialists,
    ...workspace,
  };
}

function validateWorkspaceCapsuleSelection(input) {
  if (input.workspaceRelevantPaths?.length && !input.workspaceCapsule) {
    throw new Error("workspace_relevant_paths_require_capsule");
  }
}

async function runStart(pi, input, signal, ctx) {
  if (ctx.mode !== "tui" || !ctx.hasUI) {
    throw new Error("start_requires_interactive_tui_confirmation");
  }
  if (!input.task || !String(input.task).trim()) throw new Error("start_requires_task");
  if (input.probeTask && input.withProbe === false) throw new Error("probe_task_requires_role");
  if (input.playwrightTask && input.withPlaywright === false) throw new Error("playwright_task_requires_role");
  if (input.djangoTask && input.withDjangoExpert === false) throw new Error("django_task_requires_role");
  validateWorkspaceCapsuleSelection(input);
  if (isControllerMode() && !String(input.project || "").trim()) {
    throw new Error("controller_start_requires_explicit_project");
  }

  const startInput = startInputWithParentModel(input, ctx);
  const project = await canonicalProject(startInput.project, ctx.cwd, {
    requireCanonical: Boolean(startInput.workspaceCapsule),
  });
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
    return `${data.session}: profile=${executionProfileConfirmation(data)}; flow=${workflow?.implementation_flow || "single"}; ${state}; ${data.roles?.length || 0} roles, ${data.panes?.length || 0} panes`;
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
      ? `Validated ${data.session} with ${data.implementation_flow || "single"} implementation flow`
      : `Started detached ${data.session} with ${data.transport === "rpc" ? "headless RPC" : "native Pi TUI"} workers and ${data.implementation_flow || "single"} implementation flow. This invoking Pi remains the parent; use /or-dashboard and Enter to attach.`;
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
      case "doctor": {
        const project = await canonicalProject(input.project, ctx.cwd);
        envelope = await runCli(pi, "doctor", ["--project", project], signal);
        break;
      }
      case "list":
        envelope = await runCli(pi, "list", [], signal);
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
    dashboard: "show the orchestration dashboard",
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
  const models = async (args, ctx) => {
    notifyEnvelope(ctx, modelCatalogEnvelope(ctx, args));
  };

  const dashboard = async (_args, ctx) => {
    try {
      await showOrchestrationDashboard(
        ctx,
        () => runCli(pi, "list", [], ctx.signal),
        () => runCli(pi, "doctor", ["--project", ctx.cwd], ctx.signal),
        () => getOrchestratorAboutSummary(),
        async (selection) => {
          if (selection.type === "stop") {
            await stopSession(selection.session, ctx);
            return;
          }
          if (selection.type !== "attach") return;
          const envelope = await attachAndSupervise(
            pi,
            { session: selection.session },
            ctx.signal,
            ctx,
            superviseStart,
          );
          notifyEnvelope(ctx, envelope);
        },
      );
    } catch {
      notifyCommandFailure(ctx, "dashboard");
    }
  };

  const start = async (args, ctx) => {
    if (!requireInteractiveTui(ctx, "or-start")) return;
    const task = String(args || "").trim() || await ctx.ui.editor("Orchestration task", "");
    if (!task?.trim()) return;
    let project;
    if (isControllerMode()) {
      project = await ctx.ui.input("Target project directory", "/absolute/path/to/project");
      if (!String(project || "").trim()) return;
    }
    const runOverrides = await selectRunOverrides(ctx);
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
          ...runOverrides,
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

  const send = async (args, ctx) => {
    if (!requireInteractiveTui(ctx, "or-send")) return;
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

  const stopSession = async (session, ctx) => {
    const confirmed = await ctx.ui.confirm(
      "Stop tmux orchestration?",
      `Kill only ${bounded(session, 160)}? External coordination state and child session records are retained.`,
    );
    if (!confirmed) return;
    await runCommandCli(pi, "stop", [session, "--yes"], ctx);
  };

  const stop = async (args, ctx) => {
    if (!requireInteractiveTui(ctx, "or-stop")) return;
    let session;
    try {
      session = await requestedSession(pi, args, ctx);
    } catch {
      notifyCommandFailure(ctx, "stop");
      return;
    }
    if (session) await stopSession(session, ctx);
  };

  return { models, dashboard, start, send, stop };
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
    description: "Supervise bounded doctor, available-model discovery, list, status, watch, attach, start, or send actions through the Pi runtime and bundled Python tmux orchestrator. Start resolves strict user-global exact-project defaults for profile/models, single or phased flow, enabled specialists, and the workspace capsule; explicit per-run values win. It may also select deterministic or forced specialist activation, this parent Pi's current model, exact user-requested per-role provider/model/thinking overrides, and strict per-run budget overrides. The invoking Pi remains the parent; normal starts create no separate parent Pi or controller. Watch subscribes this Pi to lifecycle and final-report updates. Attach ensures watching, then switches its existing tmux client into native Pi worker panes; prefix then L returns without stopping workers. New runs are watched automatically. Start always requires interactive confirmation.",
    promptSnippet: "Inspect or operate local Pi tmux orchestrations through the authoritative Python CLI",
    promptGuidelines: [
      "Use tmux_orchestrator instead of rebuilding tmux orchestration state; before a start, synthesize a bounded contextCapsule from the current conversation when prior decisions or work matter; include only task-relevant state, constraints, acceptance criteria, paths, evidence, and open questions, never the full transcript. Enable workspaceCapsule only for an explicit cold-assignment experiment and supply only bounded existing project-relative workspaceRelevantPaths, never a repository tree; it supplements discovery and never replaces reading governing instructions. Do not claim workspace-capsule savings or correctness without authoritative provider and review evidence. Use implementationFlow=phased for complex work that benefits from read-only discovery before editing; use single for simple work or compatibility, without an extra classifier model call. Configured specialists use conservative deterministic activation gates; pass forceSpecialists only when the user explicitly requires those enabled roles to run regardless of a skip predicate. After starting or resuming an existing run, ensure the invoking Pi is watching it for lifecycle and final reports. Once watching, end the turn and rely on broker updates: never run sleep commands or repeatedly poll status/tmux while waiting for a watched orchestration. Honor an explicit economy, balanced, thorough, or user-configured profile request through profile. Honor explicit user model/provider/thinking requests through useParentModel or modelOverrides; those overrides win over profile values. Use the models action to resolve available exact identifiers when needed; never invent a provider/model identifier or read provider credentials. Omitted overrides use the exact canonical project mapping, then the user's global orchestrator model configuration, selected/default profile, and packaged defaults. Honor explicit per-run budget requests through budgetOverrides; omitted values use the strict user-global budget policy and packaged warn-only defaults, and never infer hard thresholds. Worker skill discovery is disabled; pass workerSkills only for exact Markdown paths the user explicitly reviewed, never infer skills. When the user asks to enter, navigate, or directly steer the live workers, use attach rather than watch; attach requires the invoking Pi to be inside tmux. Prefer native Pi TUI workers and use rpcWorkers only after an explicit request for headless panes. The invoking Pi remains responsible for interpreting reports and deciding follow-up. Never create file handoffs, poll coordination state, claim parent project trust applies to child Pi sessions, or equate command acknowledgement with task completion.",
    ],
    parameters,
    execute(_toolCallId, input, signal, _onUpdate, ctx) {
      return executeAction(pi, input, signal, ctx, superviseStart);
    },
  });

  const commandHandlers = createCommandHandlers(pi, superviseStart);
  const commands = {
    "or-models": ["List available Pi model metadata", commandHandlers.models],
    "or-dashboard": ["Open the orchestration dashboard with doctor, attach/watch, and confirmed stop", commandHandlers.dashboard],
    "or-start": ["Confirm and start a tmux orchestration", commandHandlers.start],
    "or-send": ["Send a private message to one orchestration role", commandHandlers.send],
    "or-stop": ["Confirm and stop one orchestration", commandHandlers.stop],
  };
  for (const [name, [description, handler]] of Object.entries(commands)) {
    pi.registerCommand(name, { description, handler });
  }
  pi.registerShortcut("ctrl+shift+g", {
    description: "Open the orchestration dashboard overlay",
    handler: async (ctx) => commandHandlers.dashboard("", ctx),
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
