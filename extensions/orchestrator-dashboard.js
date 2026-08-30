import { basename } from "node:path";

const MAX_DOCTOR_LINES = 7;
const MAX_VISIBLE_SESSIONS = 12;
const DASHBOARD_STATIC_ROWS = 15;
const CLOSE_KEYS = new Set(["q", "Q"]);

const ansi = (code, text) => `\x1b[38;5;${code}m${text}\x1b[39m`;
const purple = (text) => ansi(141, text);
const violet = (text) => ansi(99, text);
const cyan = (text) => ansi(81, text);
const amber = (text) => ansi(215, text);

export async function showOrchestrationDashboard(ctx, loadList, loadDoctor, open) {
  if (ctx.mode !== "tui" || !ctx.hasUI) {
    return showPlainDashboard(ctx, loadList, loadDoctor);
  }
  const selection = await ctx.ui.custom(
    (tui, theme, keybindings, done) => {
      const visibleRows = resolveVisibleRows(tui.terminal?.rows);
      const overlay = new OrchestrationDashboardOverlay(
        theme,
        done,
        () => tui.requestRender(),
        loadList,
        loadDoctor,
        visibleRows,
        keybindings,
      );
      void overlay.refresh();
      return overlay;
    },
    {
      overlay: true,
      overlayOptions: {
        anchor: "center",
        width: "92%",
        minWidth: 58,
        maxHeight: "92%",
        margin: 1,
      },
    },
  );

  await openDashboardSelection(selection, open);
}

async function showPlainDashboard(ctx, loadList, loadDoctor) {
  const [list, doctor] = await Promise.all([loadList(), loadDoctor()]);
  const lines = dashboardPlainLines({ list, doctor });
  if (ctx.hasUI) ctx.ui.notify(lines.slice(0, 20).join("\n"), "info");
  else console.log(lines.join("\n"));
}

async function openDashboardSelection(selection, open) {
  if (selection?.type === "attach") await open(selection.session);
}

export class OrchestrationDashboardOverlay {
  constructor(
    theme,
    done,
    requestRender,
    loadList,
    loadDoctor,
    visibleRows = 8,
    keybindings,
  ) {
    this.theme = theme;
    this.done = done;
    this.requestRender = requestRender;
    this.loadList = loadList;
    this.loadDoctor = loadDoctor;
    this.visibleRows = visibleRows;
    this.keybindings = keybindings;
    this.sessions = [];
    this.doctor = checkingDoctor();
    this.selected = 0;
    this.scrollStart = 0;
    this.loading = true;
    this.doctorLoading = true;
    this.error = undefined;
    this.showDoctor = true;
    this.generation = 0;
    this.disposed = false;
  }

  async refresh() {
    if ((this.loading || this.doctorLoading) && this.generation > 0) return;
    const generation = ++this.generation;
    const selectedSession = this.selectedSessionName();
    this.loading = true;
    this.doctorLoading = true;
    this.doctor = checkingDoctor();
    this.error = undefined;
    this.changed();

    const doctorResult = this.startDoctorLoad();
    await this.refreshSessions(generation, selectedSession);
    this.finishDoctorLoad(generation, await doctorResult);
  }

  startDoctorLoad() {
    return Promise.resolve()
      .then(() => this.loadDoctor())
      .then((value) => ({ value }), () => ({ failed: true }));
  }

  async refreshSessions(generation, selectedSession) {
    try {
      const display = listDisplay(await this.loadList());
      this.commit(generation, () => this.applyList(display, selectedSession));
    } catch {
      this.commit(generation, () => this.applyListFailure());
    } finally {
      this.commit(generation, () => {
        this.loading = false;
        this.changed();
      });
    }
  }

  finishDoctorLoad(generation, result) {
    this.commit(generation, () => {
      this.doctor = result.failed
        ? warningDoctor("Doctor unavailable", [])
        : doctorDisplay(result.value);
      this.doctorLoading = false;
      this.changed();
    });
  }

  selectedSessionName() {
    const selected = this.sessions[this.selected];
    return selected ? selected.session : undefined;
  }

  commit(generation, action) {
    if (!this.disposed && generation === this.generation) action();
  }

  applyList(display, selectedSession) {
    this.sessions = display.sessions;
    this.error = display.error;
    this.selected = selectedIndex(this.sessions, selectedSession);
    this.ensureVisible();
  }

  applyListFailure() {
    this.sessions = [];
    this.error = "Running orchestration list unavailable.";
    this.selected = 0;
    this.scrollStart = 0;
  }

  handleInput(data) {
    const binding = this.inputBindings(data).find((item) => item.active);
    if (binding) binding.action();
  }

  inputBindings(data) {
    return [
      {
        active: CLOSE_KEYS.has(data) || this.matches(data, "tui.select.cancel", ["\x1b", "\x03"]),
        action: () => this.done(null),
      },
      {
        active: data === "k" || this.matches(data, "tui.select.up", ["\x1b[A"]),
        action: () => this.move(-1),
      },
      {
        active: data === "j" || this.matches(data, "tui.select.down", ["\x1b[B"]),
        action: () => this.move(1),
      },
      { active: matchesFallback(data, ["\x1b[H", "\x1b[1~"]), action: () => this.select(0) },
      { active: matchesFallback(data, ["\x1b[F", "\x1b[4~"]), action: () => this.select(this.sessions.length - 1) },
      { active: this.matches(data, "tui.select.pageUp", ["\x1b[5~"]), action: () => this.move(-this.visibleRows) },
      { active: this.matches(data, "tui.select.pageDown", ["\x1b[6~"]), action: () => this.move(this.visibleRows) },
      { active: this.matches(data, "tui.select.confirm", ["\r", "\n"]), action: () => this.attachSelected() },
      { active: ["r", "R"].includes(data), action: () => void this.refresh() },
      { active: ["d", "D"].includes(data), action: () => this.toggleDoctor() },
    ];
  }

  toggleDoctor() {
    this.showDoctor = !this.showDoctor;
    this.changed();
  }

  matches(data, binding, fallbacks) {
    return this.keybindings?.matches?.(data, binding) === true || matchesFallback(data, fallbacks);
  }

  render(width) {
    if (width < 4) return [truncateToWidth("Orchestrations", width)];
    const rows = new DashboardRows(width, this.theme);
    rows.topBorder(this.header());
    rows.frame(this.helpLine());
    rows.separator();
    this.renderSessions(rows);
    if (this.showDoctor) {
      rows.separator();
      this.renderDoctor(rows);
    }
    rows.separator();
    rows.frame(this.profileFooter());
    rows.bottomBorder();
    return rows.lines;
  }

  invalidate() {}

  dispose() {
    this.disposed = true;
    this.generation += 1;
  }

  header() {
    const count = this.sessions.length;
    const state = this.loading ? pill("refreshing", amber) : pill(`${count} running`, count ? cyan : violet);
    return `${purple(" ✦ ")}${this.theme.fg("accent", this.theme.bold("Orchestration Dashboard"))}${this.theme.fg("dim", " · ")}${state} `;
  }

  helpLine() {
    return [
      `${pill("↑↓/jk", violet)} ${this.theme.fg("muted", "navigate")}`,
      `${pill("Enter", violet)} ${this.theme.fg("muted", "attach/watch")}`,
      `${pill("r", violet)} ${this.theme.fg("muted", "refresh")}`,
      `${pill("d", violet)} ${this.theme.fg("muted", "doctor")}`,
      `${pill("q", violet)} ${this.theme.fg("muted", "close")}`,
    ].join("  ");
  }

  renderSessions(rows) {
    if (this.renderLoading(rows)) return;
    this.renderSessionError(rows);
    if (this.renderEmpty(rows)) return;
    this.renderSessionWindow(rows);
  }

  renderLoading(rows) {
    if (!this.loading || this.sessions.length) return false;
    rows.frame(this.theme.fg("dim", "  Loading running orchestrations…"));
    return true;
  }

  renderSessionError(rows) {
    if (this.error) rows.frame(`  ${this.theme.fg("warning", `⚠ ${this.error}`)}`);
  }

  renderEmpty(rows) {
    if (this.sessions.length) return false;
    rows.frame(this.theme.fg("muted", "  No running orchestrations."));
    return true;
  }

  renderSessionWindow(rows) {
    this.ensureVisible();
    const end = Math.min(this.sessions.length, this.scrollStart + this.visibleRows);
    this.renderEarlierCount(rows);
    for (let index = this.scrollStart; index < end; index += 1) {
      rows.frame(this.sessionLine(this.sessions[index], index));
    }
    this.renderLaterCount(rows, end);
  }

  renderEarlierCount(rows) {
    if (this.scrollStart > 0) {
      rows.frame(this.theme.fg("dim", `  … ${this.scrollStart} earlier`));
    }
  }

  renderLaterCount(rows, end) {
    if (end < this.sessions.length) {
      rows.frame(this.theme.fg("dim", `  … ${this.sessions.length - end} later`));
    }
  }

  sessionLine(session, index) {
    const selected = index === this.selected;
    const marker = selected ? purple("❯") : " ";
    const identity = `${session.session} · ${basename(session.project)}`;
    const separator = this.theme.fg("dim", "│");
    const raw = `  ${marker} ${identity}  ${separator} ${sessionWorkflow(session)}  ${separator} ${sessionProfile(session)}  ${separator} ${sessionUsage(session)}  ${separator} ${sessionRoles(session)}`;
    return selected ? this.theme.bg("selectedBg", raw) : raw;
  }

  renderDoctor(rows) {
    const { color, icon } = doctorStyle(this.doctor.status);
    rows.frame(`  ${violet("●")} ${this.theme.fg("accent", this.theme.bold("Doctor"))} ${this.theme.fg(color, `${icon} ${this.doctor.headline}`)}`);
    for (const line of this.doctor.lines.slice(0, MAX_DOCTOR_LINES)) {
      rows.frame(`    ${this.theme.fg("dim", line)}`);
    }
  }

  profileFooter() {
    const mapping = this.doctor.projectMapping ?? "project mapping unavailable";
    return `${cyan("Profiles")} ${this.theme.fg("muted", `${mapping} · read-only in this version; dashboard editing is planned`)}`;
  }

  move(delta) {
    this.select(this.selected + delta);
  }

  select(index) {
    this.selected = clamp(index, this.sessions.length);
    this.ensureVisible();
    this.changed();
  }

  ensureVisible() {
    if (this.selected < this.scrollStart) this.scrollStart = this.selected;
    if (this.selected >= this.scrollStart + this.visibleRows) {
      this.scrollStart = this.selected - this.visibleRows + 1;
    }
    this.scrollStart = Math.max(
      0,
      Math.min(this.scrollStart, Math.max(0, this.sessions.length - this.visibleRows)),
    );
  }

  attachSelected() {
    const session = this.sessions[this.selected];
    if (session) this.done({ type: "attach", session: session.session });
  }

  changed() {
    this.invalidate();
    this.requestRender();
  }
}

class DashboardRows {
  constructor(width, theme) {
    this.width = width;
    this.theme = theme;
    this.innerWidth = Math.max(0, width - 4);
    this.lines = [];
  }

  topBorder(title) {
    const safeTitle = truncateToWidth(title, Math.max(0, this.width - 2));
    const fill = Math.max(0, this.width - visibleWidth(safeTitle) - 2);
    this.lines.push(`${purple("╭")}${safeTitle}${purple(`${"─".repeat(fill)}╮`)}`);
  }

  separator() {
    this.lines.push(purple(`├${"─".repeat(Math.max(0, this.width - 2))}┤`));
  }

  bottomBorder() {
    this.lines.push(purple(`╰${"─".repeat(Math.max(0, this.width - 2))}╯`));
  }

  frame(content = "") {
    const text = truncateToWidth(content, this.innerWidth);
    const padding = " ".repeat(Math.max(0, this.innerWidth - visibleWidth(text)));
    this.lines.push(purple("│ ") + text + padding + purple(" │"));
  }

}

function sessionWorkflow(session) {
  if (session.dashboard?.available !== true) return "state unavailable";
  return `${session.dashboard.workflow.state} r${session.dashboard.workflow.round}`;
}

function sessionProfile(session) {
  return session.execution_profile?.name ?? "profile unavailable";
}

function sessionUsage(session) {
  if (session.dashboard?.available !== true) return "usage unavailable";
  return usageText(session.dashboard.usage);
}

function sessionRoles(session) {
  if (session.dashboard?.available === true) {
    return `${session.dashboard.roles.connected}/${session.dashboard.roles.total} linked`;
  }
  return `${arrayValue(session.roles).length} roles`;
}

function dashboardDisplay(snapshot) {
  const value = objectValue(snapshot);
  const list = listDisplay(value.list);
  const doctor = objectValue(value.doctor);
  const errors = [
    list.error,
    doctor.success === true
      ? undefined
      : "Doctor reported unavailable prerequisites or policy.",
  ].filter(Boolean);
  return {
    sessions: list.sessions,
    doctor: doctorDisplay(doctor),
    error: errors.length ? errors.join(" ") : undefined,
  };
}

function listDisplay(envelope) {
  const list = objectValue(envelope);
  return {
    sessions: sessionsFromList(list),
    error: list.success === true
      ? undefined
      : "Running orchestration list unavailable.",
  };
}

function sessionsFromList(list) {
  if (list.success !== true) return [];
  return arrayValue(objectValue(list.data).sessions)
    .filter(validDashboardSession)
    .slice(0, 100);
}

function doctorDisplay(envelope) {
  if (!isObject(envelope.data)) return warningDoctor("Doctor unavailable", []);
  const data = envelope.data;
  const commands = arrayValue(data.commands);
  const failedCommands = commands.filter((item) => item.status !== "ok");
  const modelChecks = arrayValue(data.model_checks);
  const unavailableModels = modelChecks.filter((item) => item.available !== true);
  const modelPolicy = objectValue(data.model_policy);
  const profile = objectValue(modelPolicy.execution_profile);
  const mapping = doctorProjectMapping(objectValue(modelPolicy.project_config));
  const warning = [
    envelope.success !== true,
    failedCommands.length > 0,
    unavailableModels.length > 0,
  ].includes(true);
  const presentation = warning
    ? { status: "warning", headline: "Doctor found issues" }
    : { status: "success", headline: "Doctor passed" };
  return {
    ...presentation,
    lines: doctorLines(data, modelPolicy, profile, mapping, commands, modelChecks, unavailableModels),
    projectMapping: mapping,
  };
}

function doctorLines(data, modelPolicy, profile, mapping, commands, modelChecks, unavailableModels) {
  const tmux = objectValue(data.tmux);
  const budget = objectValue(objectValue(data.budget_policy).effective);
  return [
    `Commands: ${commandsText(commands)}`,
    `tmux: ${stringValue(tmux.version)}`,
    `Profile: ${profileText(profile)}`,
    `Project: ${mapping}`,
    `Models: ${modelChecks.length - unavailableModels.length}/${modelChecks.length} available`,
    `Budget: ${stringValue(budget.enforcement)} (observational)`,
    `Config: ${stringValue(modelPolicy.config_path)}`,
  ];
}

function doctorProjectMapping(project) {
  if (project.matched !== true) return "no exact project mapping";
  return `matched ${project.directory}`;
}

function commandsText(commands) {
  const text = commands.map((item) => `${item.name}=${item.status}`).join(" · ");
  return text || "unavailable";
}

function profileText(profile) {
  if (typeof profile.name !== "string") return "unavailable";
  return `${profile.name} (${stringValue(profile.source)})`;
}

function dashboardPlainLines(snapshot) {
  const display = dashboardDisplay(snapshot);
  const lines = [`Orchestrations: ${display.sessions.length}`];
  for (const session of display.sessions) {
    lines.push(`  ${session.session} · ${basename(session.project)} · ${sessionUsage(session)}`);
  }
  if (!display.sessions.length) lines.push("  none running");
  lines.push(`Doctor: ${display.doctor.headline}`);
  lines.push(...display.doctor.lines.map((line) => `  ${line}`));
  if (display.error) lines.push(`Warning: ${display.error}`);
  return lines.slice(0, 40);
}

function validDashboardSession(value) {
  return Boolean(
    value
    && value.valid === true
    && typeof value.session === "string"
    && typeof value.project === "string",
  );
}

function usageText(usage) {
  const value = objectValue(usage);
  const calls = integerText(value.provider_calls);
  const tokens = compactNumber(value.operational_tokens);
  const cost = moneyText(value.cost_total);
  const context = percentText(value.context_percent);
  return `${calls} calls · ${tokens} tok · ${cost} · ctx ${context}`;
}

function integerText(value) {
  return Number.isInteger(value) && value >= 0 ? String(value) : "—";
}

function compactNumber(value) {
  if (!isNonnegativeNumber(value)) return "—";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}m`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(Math.round(value));
}

function moneyText(value) {
  return isNonnegativeNumber(value) ? `$${value.toFixed(3)}` : "$—";
}

function percentText(value) {
  return isNonnegativeNumber(value) ? `${value.toFixed(1)}%` : "—";
}

function isNonnegativeNumber(value) {
  return Number.isFinite(value) && value >= 0;
}

function doctorStyle(status) {
  return {
    success: { color: "success", icon: "✓" },
    warning: { color: "warning", icon: "⚠" },
    checking: { color: "dim", icon: "…" },
  }[status] ?? { color: "dim", icon: "…" };
}

function objectValue(value) {
  return isObject(value) ? value : {};
}

function arrayValue(value) {
  return Array.isArray(value) ? value : [];
}

function stringValue(value) {
  return typeof value === "string" && value ? value : "unavailable";
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function checkingDoctor() {
  return {
    status: "checking",
    headline: "Running doctor…",
    lines: [],
    projectMapping: "checking project mapping",
  };
}

function warningDoctor(headline, lines) {
  return {
    status: "warning",
    headline,
    lines,
    projectMapping: "project mapping unavailable",
  };
}

function selectedIndex(sessions, previous) {
  if (!sessions.length) return 0;
  const index = sessions.findIndex((item) => item.session === previous);
  return index >= 0 ? index : 0;
}

function clamp(index, count) {
  return Math.max(0, Math.min(Math.max(0, count - 1), index));
}

function resolveVisibleRows(terminalRows) {
  if (!Number.isFinite(terminalRows)) return 8;
  const overlayRows = Math.floor(terminalRows * 0.92);
  return Math.max(3, Math.min(MAX_VISIBLE_SESSIONS, overlayRows - DASHBOARD_STATIC_ROWS));
}

function matchesFallback(data, values) {
  return values.includes(data);
}

function visibleWidth(value) {
  return [...stripAnsi(value)].length;
}

function stripAnsi(value) {
  return String(value)
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "")
    .replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, "");
}

function truncateToWidth(value, width) {
  if (width <= 0) return "";
  if (visibleWidth(value) <= width) return value;
  const characters = [...stripAnsi(value)];
  return `${characters.slice(0, Math.max(0, width - 1)).join("")}…\x1b[0m`;
}

function pill(text, color) {
  return color(` ${text} `);
}

export const testHooks = {
  compactNumber,
  dashboardDisplay,
  dashboardPlainLines,
  doctorDisplay,
  moneyText,
  percentText,
  resolveVisibleRows,
  usageText,
  visibleWidth,
};
