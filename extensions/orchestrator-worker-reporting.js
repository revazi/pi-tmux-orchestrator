import { WORKER_PROTOCOL_VERSION } from "./orchestrator-worker-protocol.js";

const PLAN_REPORT_FIELDS = [
  "kind", "summary", "relevant_paths", "relevant_symbols", "intended_changes",
  "required_checks", "risks", "open_questions",
];
const PLAN_ONLY_REPORT_FIELDS = [
  "relevant_paths", "relevant_symbols", "intended_changes", "required_checks", "open_questions",
];
const PLAN_EMPTY_COMMON_FIELDS = ["changed_paths", "checks", "findings", "limitations", "verdict"];
const STANDARD_REPORT_FIELDS = [
  "kind", "summary", "changed_paths", "checks", "findings", "risks", "limitations", "verdict",
];
const PLAN_REPORT_MAX_ITEMS = 12;
const PLAN_REPORT_MAX_ITEM_CHARS = 300;
const PLAN_REPORT_MAX_SUMMARY_CHARS = 1000;
const PLAN_READ_ONLY_TOOLS = new Set(["read", "bash", "grep", "find", "ls", "orchestrator_report"]);
const PROGRESS_INTERVAL_MS = 500;

function text(value, limit) {
  if (typeof value !== "string" || !value.trim() || value.length > limit || value.includes("\0")) {
    throw new Error("invalid_report_text");
  }
  return value;
}

function textArray(value, label, maximumItems = 50, maximumChars = 500) {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > maximumItems) throw new Error(`invalid_${label}`);
  return value.map((item) => text(item, maximumChars));
}

function relativePaths(value, label, maximumItems = 50, maximumChars = 500) {
  const paths = textArray(value, label, maximumItems, maximumChars);
  if (paths.some((path) => path.startsWith("/") || [".", ".."].includes(path) || path.split("/").includes("..") || /[\u0000-\u001f\u007f]/.test(path))) {
    throw new Error(`invalid_${label}`);
  }
  return paths;
}

function planArrayProperty() {
  return {
    type: "array",
    maxItems: PLAN_REPORT_MAX_ITEMS,
    items: { type: "string", maxLength: PLAN_REPORT_MAX_ITEM_CHARS },
  };
}

export function reportParameters(role) {
  const kinds = {
    implementer: ["plan", "implementation"],
    reviewer: ["review"],
    probe: ["probe"],
    playwright: ["playwright"],
    django: ["django"],
  }[role];
  const verdicts = {
    reviewer: ["approved", "changes_requested"],
    playwright: ["pass", "fail"],
    django: ["advisory_approved", "issues_found"],
  }[role];
  const properties = {
    kind: { type: "string", enum: kinds },
    summary: { type: "string", maxLength: 2000 },
    checks: {
      type: "array", maxItems: 50,
      items: {
        type: "object", additionalProperties: false, required: ["name", "status"],
        properties: { name: { type: "string", maxLength: 500 }, status: { type: "string", enum: ["passed", "failed", "skipped", "unknown"] } },
      },
    },
    findings: {
      type: "array", maxItems: 50,
      items: {
        type: "object", additionalProperties: false, required: ["severity", "summary"],
        properties: {
          severity: { type: "string", enum: ["critical", "high", "medium", "low", "info"] },
          path: { type: "string", maxLength: 500 }, line: { type: "integer", minimum: 1 },
          summary: { type: "string", maxLength: 500 }, acceptance: { type: "string", maxLength: 500 },
        },
      },
    },
    risks: { type: "array", maxItems: 50, items: { type: "string", maxLength: 500 } },
    limitations: { type: "array", maxItems: 50, items: { type: "string", maxLength: 500 } },
  };
  const required = ["kind", "summary"];
  if (role === "implementer") {
    properties.changed_paths = { type: "array", maxItems: 50, items: { type: "string", maxLength: 500 } };
    properties.relevant_paths = planArrayProperty();
    properties.relevant_symbols = planArrayProperty();
    properties.intended_changes = planArrayProperty();
    properties.required_checks = planArrayProperty();
    properties.open_questions = planArrayProperty();
  }
  if (verdicts) {
    properties.verdict = { type: "string", enum: verdicts };
    required.push("verdict");
  }
  return { type: "object", additionalProperties: false, required, properties };
}

function emptyReportClaim(value) {
  return value == null || (Array.isArray(value) && value.length === 0);
}

function reportArgumentObject(value) {
  if (!value) return undefined;
  if (typeof value !== "object") return undefined;
  if (Array.isArray(value)) return undefined;
  return { ...value };
}

function deleteReportFields(input, fields) {
  for (const field of fields) delete input[field];
}

function deleteEmptyReportFields(input, fields) {
  for (const field of fields) {
    if (emptyReportClaim(input[field])) delete input[field];
  }
}

const REPORT_ARGUMENT_PREPARERS = {
  implementation: (input) => deleteReportFields(input, PLAN_ONLY_REPORT_FIELDS),
  plan: (input) => deleteEmptyReportFields(input, PLAN_EMPTY_COMMON_FIELDS),
};

export function prepareReportArguments(value) {
  const input = reportArgumentObject(value);
  if (!input) return value;
  const prepare = REPORT_ARGUMENT_PREPARERS[input.kind];
  if (prepare) prepare(input);
  return input;
}

function unsupportedReportFields(input, allowedFields) {
  return Object.keys(input).filter((field) => !allowedFields.includes(field));
}

function normalizedPlanReport(input) {
  if (unsupportedReportFields(input, PLAN_REPORT_FIELDS).length) {
    throw new Error("invalid_plan_report_fields");
  }
  return {
    kind: "plan",
    summary: text(input.summary, PLAN_REPORT_MAX_SUMMARY_CHARS),
    changed_paths: [],
    checks: [],
    findings: [],
    relevant_paths: relativePaths(
      input.relevant_paths, "relevant_paths", PLAN_REPORT_MAX_ITEMS, PLAN_REPORT_MAX_ITEM_CHARS,
    ),
    relevant_symbols: textArray(
      input.relevant_symbols, "relevant_symbols", PLAN_REPORT_MAX_ITEMS, PLAN_REPORT_MAX_ITEM_CHARS,
    ),
    intended_changes: textArray(
      input.intended_changes, "intended_changes", PLAN_REPORT_MAX_ITEMS, PLAN_REPORT_MAX_ITEM_CHARS,
    ),
    required_checks: textArray(
      input.required_checks, "required_checks", PLAN_REPORT_MAX_ITEMS, PLAN_REPORT_MAX_ITEM_CHARS,
    ),
    risks: textArray(
      input.risks, "risks", PLAN_REPORT_MAX_ITEMS, PLAN_REPORT_MAX_ITEM_CHARS,
    ),
    limitations: [],
    open_questions: textArray(
      input.open_questions, "open_questions", PLAN_REPORT_MAX_ITEMS, PLAN_REPORT_MAX_ITEM_CHARS,
    ),
    verdict: null,
  };
}

function expectedReportKind(role, assignmentKind) {
  if (role === "implementer" && assignmentKind === "plan") return "plan";
  return {
    implementer: "implementation",
    reviewer: "review",
    probe: "probe",
    playwright: "playwright",
    django: "django",
  }[role];
}

function normalizeCheck(check) {
  const valid = [
    Boolean(check),
    typeof check === "object",
    ["passed", "failed", "skipped", "unknown"].includes(check?.status),
  ].every(Boolean);
  if (!valid) throw new Error("invalid_check");
  return { name: text(check.name, 500), status: check.status };
}

function normalizeChecks(value) {
  const checks = value ?? [];
  if (![Array.isArray(checks), checks.length <= 50].every(Boolean)) throw new Error("invalid_checks");
  return checks.map(normalizeCheck);
}

function optionalReportText(value) {
  if (value == null) return null;
  return text(value, 500);
}

function validFindingLine(value) {
  if (value === null) return true;
  return [Number.isInteger(value), value > 0].every(Boolean);
}

function normalizeFinding(finding) {
  const value = Object(finding);
  const valid = [
    Boolean(finding),
    typeof finding === "object",
    ["critical", "high", "medium", "low", "info"].includes(value.severity),
  ].every(Boolean);
  if (!valid) throw new Error("invalid_finding");
  const line = value.line ?? null;
  if (!validFindingLine(line)) throw new Error("invalid_finding_line");
  return {
    severity: value.severity,
    path: optionalReportText(value.path),
    line,
    summary: text(value.summary, 500),
    acceptance: optionalReportText(value.acceptance),
  };
}

function normalizeFindings(value) {
  const findings = value ?? [];
  if (![Array.isArray(findings), findings.length <= 50].every(Boolean)) throw new Error("invalid_findings");
  return findings.map(normalizeFinding);
}

function normalizedVerdict(value, role) {
  const verdicts = {
    reviewer: ["approved", "changes_requested"],
    playwright: ["pass", "fail"],
    django: ["advisory_approved", "issues_found"],
  }[role];
  const verdict = value ?? null;
  const valid = verdicts ? verdicts.includes(verdict) : verdict === null;
  if (!valid) throw new Error("invalid_report_verdict");
  return verdict;
}

function normalizedStandardReport(input, role) {
  if (unsupportedReportFields(input, STANDARD_REPORT_FIELDS).length) {
    throw new Error("invalid_report_fields");
  }
  const changedPaths = relativePaths(input.changed_paths, "changed_paths");
  if (role !== "implementer" && changedPaths.length) throw new Error("read_only_role_changed_paths");
  return {
    kind: input.kind,
    summary: text(input.summary, 2000),
    changed_paths: changedPaths,
    checks: normalizeChecks(input.checks),
    findings: normalizeFindings(input.findings),
    risks: textArray(input.risks, "risks"),
    limitations: textArray(input.limitations, "limitations"),
    verdict: normalizedVerdict(input.verdict, role),
  };
}

export function normalizeReport(value, assignmentKind, role) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid_report");
  const input = prepareReportArguments(value);
  const expectedKind = expectedReportKind(role, assignmentKind);
  if (input.kind !== expectedKind) throw new Error("invalid_report_kind");
  return input.kind === "plan"
    ? normalizedPlanReport(input)
    : normalizedStandardReport(input, role);
}

export function assignmentToolNames(normalTools, role, assignmentKind) {
  if (role !== "implementer" || assignmentKind !== "plan") return [...normalTools];
  return normalTools.filter((name) => PLAN_READ_ONLY_TOOLS.has(name));
}

export function reportedAssignmentRemainsActive(active, reported) {
  return active?.id === reported.id;
}

export function acceptedReportResult(report, assignment, role) {
  return {
    content: [{ type: "text", text: `Structured ${report.kind} report accepted for round ${assignment.round}. End this turn; do not wait or poll.` }],
    details: { protocol_version: WORKER_PROTOCOL_VERSION, assignment_id: assignment.id, round: assignment.round, role, report },
    terminate: true,
  };
}

export function progressShouldEmit(
  phase,
  previousPhase,
  now,
  previousAt,
  force = false,
) {
  return force
    || phase !== previousPhase
    || now - previousAt >= PROGRESS_INTERVAL_MS;
}

export function planToolDecision(activeAssignment, role, toolName) {
  const planActive = [
    role === "implementer",
    Object(activeAssignment).kind === "plan",
  ].every(Boolean);
  if (!planActive) return undefined;
  if (PLAN_READ_ONLY_TOOLS.has(toolName)) return undefined;
  return {
    block: true,
    reason: "Inspect/plan assignments are read-only; inspect and submit a plan report without modifying the worktree.",
  };
}
