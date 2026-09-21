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
import { controlRoleParameters, publicRoleContracts, validControlRole } from "./orchestrator-role-metadata.js";
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
  previewModelsAreAvailable,
  ROLES,
  startInputWithParentModel,
} from "./orchestrator-models.js";
import {
  decisionModelConfirmation,
  plannerPlanConfirmation,
  runPreflightPlanner,
  selectDecisionModel,
  staticPlannerFallbackConfirmation,
  validateDecisionModelPolicy,
} from "./orchestrator-planner.js";
import {
  getOrchestratorAboutSummary,
  scheduleOrchestratorUpdateNotice,
} from "./orchestrator-update.js";
import { showOrchestrationDashboard } from "./orchestrator-dashboard.js";
import {
  appendWorkerContextArgs,
  selectWorkerContext,
  validateWorkerContext,
  validateWorkerContextPreview,
  workerContextConfirmation,
  workerContextParameters,
} from "./orchestrator-context-policy.js";

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
    maxRepairRounds: {
      type: "integer",
      minimum: 0,
      maximum: 1_000_000,
      description: "Explicit per-run cap on additional implementation rounds for start; omitted disables it, 0 pauses before the first repair. Not an active-assignment token budget",
    },
    profile: {
      type: "string",
      pattern: "^[a-z][a-z0-9-]{0,31}$",
      description: "Packaged or strict user-global execution profile for start",
    },
    session: { type: "string", description: "Exact orchestration session for status, watch, attach, or send" },
    role: controlRoleParameters,
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
    projectCustomRoles: {
      type: "boolean",
      description: "Include exact-project custom read-only specialists from validated user-global configuration. false omits them for this run. IDs come only from that mapping; never invent custom role identifiers",
    },
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
    dynamicPlan: {
      type: "boolean",
      description: "Before start preview, make one separately confirmed provider call that chooses the bounded built-in worker roster and exact per-role model/thinking settings. No tmux session or worker starts before the decision and final confirmation",
    },
    decisionModel: {
      type: "object",
      additionalProperties: false,
      required: ["provider", "model"],
      description: "Exact available provider/model override for dynamic planning. It wins over the strict user-global preferred identity and ordered fallbacks. Use it for Jev only when the operator supplies the canonical identity; the runtime never guesses or fuzzy-matches Jev. Planning thinking is capped at medium",
      properties: {
        provider: { type: "string", minLength: 1, maxLength: 256 },
        model: { type: "string", minLength: 1, maxLength: 256 },
        thinking: { type: "string", enum: ["off", "minimal", "low", "medium"] },
      },
    },
    modelOverrides: {
      type: "object",
      additionalProperties: false,
      description: "For start, explicit user-requested all-role or per-role provider/model/thinking overrides; omitted fields retain configured defaults",
      properties: Object.fromEntries(MODEL_ROLES.map((role) => [role, modelOverrideParameters])),
    },
    budgetOverrides: budgetOverrideParameters,
    workerContext: workerContextParameters,
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

function validateRepairLimit(value) {
  if (value !== undefined && (!Number.isInteger(value) || value < 0 || value > 1_000_000)) {
    throw new Error("maxRepairRounds must be an integer from 0 to 1000000");
  }
}

function appendRepairLimitArgs(args, input) {
  if (input.maxRepairRounds !== undefined) {
    args.push("--max-repair-rounds", String(input.maxRepairRounds));
  }
}

async function selectRepairLimit(ctx) {
  const value = await ctx.ui.input(
    "Maximum additional repair rounds (blank disables; 0 pauses before first repair)",
    "0–1000000; not an active-assignment token budget",
  );
  if (value === undefined || value === null) return null;
  const text = value.trim();
  if (!text) return {};
  if (!/^[0-9]{1,7}$/.test(text)) throw new Error("invalid_repair_limit_input");
  const maxRepairRounds = Number(text);
  validateRepairLimit(maxRepairRounds);
  return { maxRepairRounds };
}

async function selectStartControls(ctx) {
  const repairLimit = await selectRepairLimit(ctx);
  if (repairLimit === null) return null;
  const workerContext = await selectWorkerContext(ctx);
  if (workerContext === null) return null;
  return { ...repairLimit, workerContext };
}

function repairLimitConfirmation(data) {
  const policy = data?.continuation_policy;
  if (policy?.version !== 1 || policy.max_repair_rounds === undefined) return "unavailable";
  if (policy.max_repair_rounds === null) return "disabled";
  validateRepairLimit(policy.max_repair_rounds);
  return `${policy.max_repair_rounds} additional implementation rounds; pauses incomplete at the cap (not an active-assignment token budget)`;
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

function appendProjectCustomRoleArgs(args, input) {
  if (input.projectCustomRoles === false) args.push("--no-project-custom-roles");
}

function buildStartArgs(
  input,
  project,
  paths,
  { dryRun = false, skipModelCheck = false } = {},
) {
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
  appendProjectCustomRoleArgs(args, input);
  appendForcedSpecialistArgs(args, input.forceSpecialists);
  if (input.profile) args.push("--profile", input.profile);
  appendModelArgs(args, input);
  appendBudgetArgs(args, input);
  appendRepairLimitArgs(args, input);
  appendWorkerContextArgs(args, input.workerContext);
  for (const role of ROLES) {
    for (const path of input.workerSkills?.[role] || []) {
      args.push("--worker-skill", `${role}=${path}`);
    }
  }
  if (dryRun) args.push("--dry-run");
  if (dryRun || skipModelCheck) args.push("--skip-model-check");
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

function customRoleLine(role) {
  const base = `${role.name}: ${role.provider}/${role.model} (${role.thinking})`;
  if (typeof role.specialist_contract !== "string") return base;
  return `${base}; contract=${role.specialist_contract}; selection=${role.selection_source}; thinking-source=${role.thinking_source}; activation=${role.activation_source}`;
}

function startConfirmation(preview, plannerPlan) {
  const data = preview.data ?? {};
  const roles = (data.roles || []).map(customRoleLine).join("\n");
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
    `Preflight decision:\n${plannerPlanConfirmation(plannerPlan)}`,
    `Roles/models (CLI policy):\n${roles}`,
    `Effective provider-usage budget policy:\n${budgetConfirmation(data.budget_policy)}`,
    `Repair-round continuation cap: ${repairLimitConfirmation(data)}`,
    `Worker provider context (CLI policy): ${workerContextConfirmation(data)}`,
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
    "Override flow, specialist, custom-specialist, and workspace-capsule defaults from the exact project mapping for this run? No uses the configured project/global defaults.",
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
  const projectCustomRoles = await ctx.ui.confirm(
    "Project custom specialists",
    "Keep exact-project custom read-only specialists for this run? No omits them.",
  );
  const workspace = await selectWorkspaceCapsule(ctx);
  return {
    implementationFlow: phased ? "phased" : "single",
    withProbe,
    withPlaywright,
    withDjangoExpert,
    forceSpecialists,
    projectCustomRoles,
    ...workspace,
  };
}

function validateWorkspaceCapsuleSelection(input) {
  if (input.workspaceRelevantPaths?.length && !input.workspaceCapsule) {
    throw new Error("workspace_relevant_paths_require_capsule");
  }
}

function validateStartRequest(input, ctx) {
  if (ctx.mode !== "tui" || !ctx.hasUI) throw new Error("start_requires_interactive_tui_confirmation");
  if (!input.task || !String(input.task).trim()) throw new Error("start_requires_task");
  if (input.probeTask && input.withProbe === false) throw new Error("probe_task_requires_role");
  if (input.playwrightTask && input.withPlaywright === false) throw new Error("playwright_task_requires_role");
  if (input.djangoTask && input.withDjangoExpert === false) throw new Error("django_task_requires_role");
  validateWorkspaceCapsuleSelection(input);
  validateRepairLimit(input.maxRepairRounds);
  validateWorkerContext(input.workerContext);
  if (isControllerMode() && !String(input.project || "").trim()) {
    throw new Error("controller_start_requires_explicit_project");
  }
  if (input.decisionModel !== undefined && input.dynamicPlan !== true) {
    throw new Error("decision_model_requires_dynamic_plan");
  }
}

function plannerPolicyFromEnvelope(envelope) {
  if (!envelope?.success) throw new Error("planner_policy_unavailable");
  const data = envelope.data;
  if (!data || typeof data !== "object" || Array.isArray(data)
      || typeof data.config_path !== "string"
      || typeof data.configured !== "boolean") {
    throw new Error("invalid_planner_policy_envelope");
  }
  return validateDecisionModelPolicy(data.policy);
}

async function applyDynamicPlan(pi, ctx, input, project, signal) {
  if (input.dynamicPlan !== true) return { input, plan: undefined };
  const envelope = await runCli(pi, "planner-policy", ["--project", project], signal);
  const policy = plannerPolicyFromEnvelope(envelope);
  const selection = selectDecisionModel(ctx, input.decisionModel, {
    version: policy.version,
    preferred: policy.preferred,
    fallbacks: policy.fallbacks,
    no_eligible: policy.noEligible,
  });
  if (selection.kind === "static") {
    const confirmed = await ctx.ui.confirm(
      "Use static/manual start instead?",
      staticPlannerFallbackConfirmation(selection),
    );
    if (!confirmed) throw new Error("dynamic_planning_static_fallback_declined");
    return { input: { ...input, dynamicPlan: false }, plan: undefined };
  }
  const planningConfirmed = await ctx.ui.confirm(
    "Authorize preflight decision call?",
    decisionModelConfirmation(selection),
  );
  if (!planningConfirmed) throw new Error("dynamic_planning_confirmation_declined");
  return runPreflightPlanner(ctx, input, project, selection, signal);
}

async function confirmChildApproval(ctx, input) {
  if (!input.approveProject) return;
  if (!ctx.isProjectTrusted()) throw new Error("approve_requires_trusted_parent_project");
  const confirmed = await ctx.ui.confirm(
    "Child project trust bypass",
    "The parent trust decision does not automatically apply to child Pi sessions. Pass --approve to every child for this run?",
  );
  if (!confirmed) throw new Error("approve_confirmation_declined");
}

function validatePlannerPreview(data, plannerPlan) {
  if (!plannerPlan) return;
  if (!Array.isArray(data?.roles) || data.roles.length !== plannerPlan.roles.length) {
    throw new Error("dynamic_planning_preview_mismatch");
  }
  const actual = new Map(data.roles.map((role) => [role?.name, role]));
  if (actual.size !== data.roles.length) throw new Error("dynamic_planning_preview_mismatch");
  for (const expected of plannerPlan.roles) {
    const role = actual.get(expected.role);
    if (role?.provider !== expected.provider
        || role?.model !== expected.model
        || role?.thinking !== expected.thinking) {
      throw new Error("dynamic_planning_preview_mismatch");
    }
  }
}

async function launchConfirmedStart(pi, input, project, plannerPlan, signal, ctx) {
  return withPrivateFiles(startFileValues(input), async (paths) => {
    const preview = await runCli(pi, "start", buildStartArgs(input, project, paths, { dryRun: true }), signal);
    if (plannerPlan?.usage) preview.planner_usage = plannerPlan.usage;
    if (!preview.success) return preview;
    validateWorkerContextPreview(preview.data, input.workerContext);
    validatePlannerPreview(preview.data, plannerPlan);
    const confirmed = await ctx.ui.confirm(
      "Start tmux orchestration?",
      startConfirmation(preview, plannerPlan),
    );
    if (!confirmed) throw new Error("start_confirmation_declined");
    const skipModelCheck = previewModelsAreAvailable(ctx, preview.data?.roles);
    const envelope = await runCli(pi, "start", buildStartArgs(input, project, paths, { skipModelCheck }), signal);
    if (plannerPlan?.usage) envelope.planner_usage = plannerPlan.usage;
    return envelope;
  });
}

async function runStart(pi, input, signal, ctx) {
  validateStartRequest(input, ctx);
  const initialInput = startInputWithParentModel(input, ctx);
  const project = await canonicalProject(initialInput.project, ctx.cwd, {
    requireCanonical: Boolean(initialInput.workspaceCapsule),
  });
  const planned = await applyDynamicPlan(pi, ctx, initialInput, project, signal);
  await confirmChildApproval(ctx, planned.input);
  return launchConfirmedStart(pi, planned.input, project, planned.plan, signal, ctx);
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
  await superviseStart(statusEnvelope, { triggerInitialActionable: false });
  return runAttach(pi, { session: statusEnvelope.data?.session }, signal, ctx);
}

async function runSend(pi, input, signal) {
  if (!input.session || !input.role || !input.message || !String(input.message).trim()) {
    throw new Error("send_requires_session_role_message");
  }
  if (!validControlRole(input.role)) throw new Error("invalid_send_role");
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

async function watchAction(pi, input, signal, superviseStart) {
  const session = String(input.session || "").trim();
  const envelope = await runCli(pi, "status", session ? [session] : [], signal);
  if (!envelope.success) return envelope;
  await superviseStart(envelope);
  return { ...envelope, command: "watch" };
}

async function startAction(pi, input, signal, ctx, superviseStart) {
  const envelope = await runStart(pi, input, signal, ctx);
  if (envelope.success && !envelope.data?.dry_run) {
    void Promise.resolve(superviseStart(envelope)).catch(() => {});
  }
  return envelope;
}

async function actionEnvelope(pi, input, signal, ctx, superviseStart) {
  switch (input.action) {
    case "models":
      return modelCatalogEnvelope(ctx, input.query);
    case "doctor": {
      const project = await canonicalProject(input.project, ctx.cwd);
      return runCli(pi, "doctor", ["--project", project], signal);
    }
    case "list":
      return runCli(pi, "list", [], signal);
    case "status":
      return runCli(pi, "status", input.session ? [input.session] : [], signal);
    case "watch":
      return watchAction(pi, input, signal, superviseStart);
    case "attach":
      return attachAndSupervise(pi, input, signal, ctx, superviseStart);
    case "start":
      return startAction(pi, input, signal, ctx, superviseStart);
    case "send":
      return runSend(pi, input, signal);
    default:
      throw new Error("unsupported_action");
  }
}

async function executeAction(pi, input, signal, ctx, superviseStart = () => {}) {
  try {
    const envelope = await actionEnvelope(pi, input, signal, ctx, superviseStart);
    const summary = notifyEnvelope(ctx, envelope);
    return {
      content: [{
        type: "text",
        text: input.action === "models" ? modelCatalogContent(envelope.data) : bounded(summary, 800),
      }],
      details: safeDetails(envelope),
      ...(envelope.planner_usage ? { usage: envelope.planner_usage } : {}),
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

async function requestedRole(pi, session, ctx) {
  const envelope = await runCli(pi, "status", [session], ctx.signal);
  if (!envelope.success) {
    notifyEnvelope(ctx, envelope);
    return undefined;
  }
  const roles = [...publicRoleContracts(envelope.data?.roles).keys()];
  const selected = await ctx.ui.select("Target role", roles);
  return roles.includes(selected) ? selected : undefined;
}

async function commandTargetProject(ctx) {
  if (!isControllerMode()) return undefined;
  const project = await ctx.ui.input("Target project directory", "/absolute/path/to/project");
  return String(project || "").trim() ? project : null;
}

async function commandChildApproval(ctx) {
  if (!ctx.isProjectTrusted()) return false;
  return ctx.ui.confirm(
    "Child trust policy",
    "Request a separately confirmed --approve bypass for child Pi sessions? No keeps native child trust prompts.",
  );
}

async function interactiveStartRequest(args, ctx) {
  if (!requireInteractiveTui(ctx, "or-start")) return undefined;
  const supplied = String(args || "").trim();
  const dynamicPlan = supplied === "--plan" || supplied.startsWith("--plan ");
  const suppliedTask = dynamicPlan ? supplied.slice("--plan".length).trim() : supplied;
  const task = suppliedTask || await ctx.ui.editor("Orchestration task", "");
  if (!task?.trim()) return undefined;
  const project = await commandTargetProject(ctx);
  if (project === null) return undefined;
  const runOverrides = dynamicPlan ? {} : await selectRunOverrides(ctx);
  const approveProject = await commandChildApproval(ctx);
  const startControls = await selectStartControls(ctx);
  if (startControls === null) return undefined;
  return {
    task,
    project,
    dynamicPlan,
    ...runOverrides,
    ...startControls,
    rpcWorkers: false,
    approveProject,
  };
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
    try {
      const request = await interactiveStartRequest(args, ctx);
      if (!request) return;
      const envelope = await runStart(pi, request, ctx.signal, ctx);
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
    let role;
    try {
      session = await requestedSession(pi, args, ctx);
      if (!session) return;
      role = await requestedRole(pi, session, ctx);
    } catch {
      notifyCommandFailure(ctx, "send");
      return;
    }
    if (!role) return;
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

  async function superviseStart(envelope, observerOptions = {}) {
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
        observerOptions,
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
    description: "Supervise bounded doctor, available-model discovery, list, status, watch, attach, start, or send actions through the Pi runtime and bundled Python tmux orchestrator. Start resolves strict user-global exact-project defaults for profile/models, single or phased flow, enabled specialists, exact-project custom read-only specialists, and the workspace capsule; explicit per-run values win. With dynamicPlan=true it makes one separately confirmed preflight provider call before preview to choose the bounded built-in worker roster and exact per-role model/thinking settings; an exact operator-supplied decisionModel wins, otherwise it uses the strict user-global exact preferred identity and ordered cross-provider fallbacks, with configured cancel or explicitly confirmed static/manual behavior when none are eligible, and caps planning/worker thinking at medium. It never guesses or fuzzy-matches a Jev identity. It may also omit project custom specialists with projectCustomRoles=false, select deterministic or forced specialist activation, this parent Pi's current model, exact user-requested per-role provider/model/thinking overrides, strict per-run budget overrides, an opt-in additional repair-round cap, and explicit per-role retain/prune worker context. Never invent custom role IDs. The invoking Pi remains the parent; normal starts create no separate parent Pi or controller. Watch subscribes this Pi to lifecycle and final-report updates. Attach watches future transitions and switches its existing tmux client into native Pi worker panes without replaying an already-actionable initial outcome as a new parent task; prefix then L returns without stopping workers or changing this Pi's project context. New runs are watched automatically. Start always requires interactive confirmation.",
    promptSnippet: "Inspect or operate local Pi tmux orchestrations through the authoritative Python CLI",
    promptGuidelines: [
      "Use tmux_orchestrator instead of rebuilding tmux orchestration state; before a start, synthesize a bounded contextCapsule from the current conversation when prior decisions or work matter; include only task-relevant state, constraints, acceptance criteria, paths, evidence, and open questions, never the full transcript. Prefer dynamicPlan=true when the user asks the orchestrator to decide worker count, roles, models, or thinking before launch; that mode requires separate approval for one preflight provider call and another confirmation for launch. Use an exact decisionModel only when the user supplied it, including any canonical Jev identity; never guess or fuzzy-match Jev. Otherwise the runtime uses the strict user-global exact preferred identity and ordered cross-provider fallbacks, then its explicit cancel/static policy. Dynamic planning currently selects built-in roles only and omits project custom roles. Enable workspaceCapsule only for an explicit cold-assignment experiment and supply only bounded existing project-relative workspaceRelevantPaths, never a repository tree; it supplements discovery and never replaces reading governing instructions. Do not claim workspace-capsule savings or correctness without authoritative provider and review evidence. Do not claim dynamic-planning savings or correctness without separately reviewed provider and outcome evidence. Use implementationFlow=phased for complex work that benefits from read-only discovery before editing; use single for simple work or compatibility. Configured specialists use conservative deterministic activation gates after launch; pass forceSpecialists only when the user explicitly requires that enabled role to run regardless of a skip predicate. Exact-project customRoles from validated user-global configuration are included unless projectCustomRoles is false; never invent custom role identifiers, providers, models, tools, or contracts. After starting or explicitly watching a run, ensure the invoking Pi is watching it for lifecycle and final reports. Once watching, end the turn and rely on broker updates: never run sleep commands or repeatedly poll status/tmux while waiting for a watched orchestration. Attaching to an existing run watches future transitions but does not replay an already-actionable initial outcome into the current Pi; returning with tmux prefix then L does not change the current Pi's project context. Honor an explicit economy, balanced, thorough, or user-configured profile request through profile. Honor explicit user model/provider/thinking requests through useParentModel or modelOverrides; those overrides win over profile values. Use the models action to resolve available exact identifiers when needed; never invent a provider/model identifier or read provider credentials. Omitted overrides use the exact canonical project mapping, then the user's global orchestrator model configuration, selected/default profile, and packaged defaults. Honor explicit per-run budget requests through budgetOverrides; omitted values use the strict user-global budget policy and packaged warn-only defaults, and never infer hard thresholds. Honor explicit repair-round cap requests through maxRepairRounds; omission disables the cap and 0 pauses before the first repair. This is separate from observational budgets and does not cap active-assignment tokens. Continuation approval remains operator-only through the confirmed terminal CLI; never approve your own continuation. Honor explicit retain/prune requests through workerContext for enabled roles; omitted roles keep prune. Retention may increase cost, reuse hints are advisory metadata observations, and historical checks or approval never replace required verification and review. Do not infer retention, switch live policies, or request unsupported compact/fresh modes. Worker skill discovery is disabled; pass workerSkills only for exact Markdown paths the user explicitly reviewed, never infer skills. When the user asks to enter, navigate, or directly steer the live workers, use attach rather than watch; attach requires the invoking Pi to be inside tmux. Prefer native Pi TUI workers and use rpcWorkers only after an explicit request for headless panes. The invoking Pi remains responsible for interpreting reports and deciding follow-up. When a workflow needs attention, send only to a waiting role that owns the active assignment; never trigger an idle role or reviewer without a broker assignment. Never create file handoffs, poll coordination state, claim parent project trust applies to child Pi sessions, or equate command acknowledgement with task completion.",
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
    "or-start": ["Confirm and start a tmux orchestration; prefix the task with --plan for model-guided role/model/thinking selection", commandHandlers.start],
    "or-send": ["Send a private message to one orchestration role", commandHandlers.send],
    "or-stop": ["Confirm and stop one orchestration", commandHandlers.stop],
  };
  for (const [name, [description, handler]] of Object.entries(commands)) {
    pi.registerCommand(name, { description, handler });
  }
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
  plannerPolicyFromEnvelope,
  previewModelsAreAvailable,
  runAttach,
  runPreflightPlanner,
  selectDecisionModel,
  runStart,
  startInputWithParentModel,
  validateObserverFrame,
  withPrivateFiles,
};
