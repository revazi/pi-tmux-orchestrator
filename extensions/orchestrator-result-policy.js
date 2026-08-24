import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const KIB = 1024;
const NOTICE_BYTES = 1024;
const NOTICE_LINES = 4;
const DIAGNOSTIC_PATTERN = /(?:^|\b)(?:assertionerror|error|fail(?:ed|ure)?|fatal|not ok|panic)(?:\b|:)/i;
const RESULT_TOOLS = new Set(["read", "grep", "bash"]);

export const RESULT_POLICY = Object.freeze({
  read: Object.freeze({ maxBytes: 16 * KIB, maxLines: 400, direction: "head" }),
  grep: Object.freeze({ maxBytes: 16 * KIB, maxLines: 240, maxMatches: 40, maxContext: 2, direction: "head" }),
  bash: Object.freeze({ maxBytes: 24 * KIB, maxLines: 400, direction: "head_tail" }),
});

function byteLength(value) {
  return Buffer.byteLength(value, "utf8");
}

function splitLines(value) {
  if (!value) return [];
  const lines = value.split("\n");
  if (value.endsWith("\n")) lines.pop();
  return lines;
}

function headWithin(value, maxLines, maxBytes) {
  const output = [];
  let bytes = 0;
  for (const line of splitLines(value).slice(0, maxLines)) {
    const separator = output.length ? 1 : 0;
    const nextBytes = byteLength(line) + separator;
    if (bytes + nextBytes > maxBytes) break;
    output.push(line);
    bytes += nextBytes;
  }
  return output.join("\n");
}

function utf8Tail(value, maxBytes) {
  const source = Buffer.from(value, "utf8");
  if (source.length <= maxBytes) return value;
  let start = source.length - maxBytes;
  while (start < source.length && (source[start] & 0xc0) === 0x80) start += 1;
  return source.subarray(start).toString("utf8");
}

function preserveOversizedTailLine(output, line, maxBytes) {
  if (!output.length) output.unshift(utf8Tail(line, maxBytes));
}

function tailWithin(value, maxLines, maxBytes) {
  const output = [];
  let bytes = 0;
  const candidates = splitLines(value).slice(-maxLines).reverse();
  for (const line of candidates) {
    const nextBytes = byteLength(line) + Math.min(output.length, 1);
    if (bytes + nextBytes > maxBytes) {
      preserveOversizedTailLine(output, line, maxBytes);
      break;
    }
    output.unshift(line);
    bytes += nextBytes;
  }
  return output.join("\n");
}

function diagnosticsWithin(value, maxLines, maxBytes) {
  const matches = splitLines(value).filter((line) => DIAGNOSTIC_PATTERN.test(line));
  return headWithin(matches.join("\n"), maxLines, maxBytes);
}

function headTailWithin(value, maxLines, maxBytes) {
  const headLines = Math.max(1, Math.floor(maxLines * 0.2));
  const diagnosticLines = Math.max(1, Math.floor(maxLines * 0.2));
  const tailLines = Math.max(1, maxLines - headLines - diagnosticLines);
  const headBytes = Math.floor(maxBytes * 0.2);
  const diagnosticBytes = Math.floor(maxBytes * 0.2);
  const tailBytes = maxBytes - headBytes - diagnosticBytes;
  const sections = [
    headWithin(value, headLines, headBytes),
    diagnosticsWithin(value, diagnosticLines, diagnosticBytes),
    tailWithin(value, tailLines, tailBytes),
  ].filter(Boolean);
  return sections.join("\n[... orchestration output omitted ...]\n");
}

function textContent(content) {
  return (Array.isArray(content) ? content : [])
    .filter((item) => item?.type === "text" && typeof item.text === "string")
    .map((item) => item.text)
    .join("\n");
}

function nonTextContent(content) {
  return (Array.isArray(content) ? content : []).filter((item) => item?.type !== "text");
}

function resultStats(value) {
  return { bytes: byteLength(value), lines: splitLines(value).length };
}

function needsTruncation(stats, policy) {
  return stats.bytes > policy.maxBytes || stats.lines > policy.maxLines;
}

function boundedPayload(value, policy) {
  const maxBytes = policy.maxBytes - NOTICE_BYTES;
  const maxLines = policy.maxLines - NOTICE_LINES;
  return policy.direction === "head_tail"
    ? headTailWithin(value, maxLines, maxBytes)
    : headWithin(value, maxLines, maxBytes);
}

function requestedPositiveNumber(value) {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : undefined;
}

function cappedValue(requested, defaultValue, maximum) {
  const accepted = requestedPositiveNumber(requested);
  return accepted === undefined ? defaultValue : Math.min(accepted, maximum);
}

function valueOrNull(value) {
  return value === undefined ? null : value;
}

function readInputPolicy(event) {
  const requestedLimit = requestedPositiveNumber(event.input.limit);
  const effectiveLimit = cappedValue(requestedLimit, RESULT_POLICY.read.maxLines, RESULT_POLICY.read.maxLines);
  event.input.limit = effectiveLimit;
  return {
    input_capped: requestedLimit !== effectiveLimit,
    requested_limit: valueOrNull(requestedLimit),
    effective_limit: effectiveLimit,
  };
}

function grepInputWasCapped(requestedLimit, effectiveLimit, requestedContext, effectiveContext) {
  return [
    requestedLimit !== effectiveLimit,
    requestedContext !== effectiveContext,
  ].some(Boolean);
}

function grepInputPolicy(event) {
  const requestedLimit = requestedPositiveNumber(event.input.limit);
  const requestedContext = requestedPositiveNumber(event.input.context) || 0;
  const effectiveLimit = cappedValue(requestedLimit, RESULT_POLICY.grep.maxMatches, RESULT_POLICY.grep.maxMatches);
  const effectiveContext = Math.min(requestedContext, RESULT_POLICY.grep.maxContext);
  event.input.limit = effectiveLimit;
  event.input.context = effectiveContext;
  return {
    input_capped: grepInputWasCapped(
      requestedLimit, effectiveLimit, requestedContext, effectiveContext,
    ),
    requested_limit: valueOrNull(requestedLimit),
    effective_limit: effectiveLimit,
    requested_context: requestedContext,
    effective_context: effectiveContext,
  };
}

const INPUT_POLICIES = { read: readInputPolicy, grep: grepInputPolicy };

export function applyToolInputPolicy(event) {
  const apply = INPUT_POLICIES[event.toolName];
  return apply ? apply(event) : undefined;
}

function existingBashOutputPath(event) {
  const value = Object(event.details).fullOutputPath;
  if (typeof value !== "string") return undefined;
  return value || undefined;
}

async function bashOutputPath(event, source) {
  const existing = existingBashOutputPath(event);
  if (existing) return existing;
  try {
    const directory = await mkdtemp(join(tmpdir(), "pi-tmux-bash-"));
    const path = join(directory, "output.txt");
    await writeFile(path, source, { encoding: "utf8", flag: "wx", mode: 0o600 });
    return path;
  } catch {
    return undefined;
  }
}

function bashContinuation(fullOutputPath) {
  if (!fullOutputPath) return "Full output path unavailable; rerun a narrower command.";
  if (byteLength(fullOutputPath) > 500) return "Full output path is too long to include; rerun a narrower command.";
  return `Full output: ${fullOutputPath}. Inspect only a targeted slice.`;
}

function continuationNotice(event, sourceStats, outputStats, fullOutputPath) {
  if (event.toolName === "read") {
    const nextOffset = Math.max(1, requestedPositiveNumber(event.input.offset) ?? 1) + outputStats.lines;
    return `[Orchestration result truncated: showing ${outputStats.lines} of ${sourceStats.lines} lines (${outputStats.bytes} of ${sourceStats.bytes} UTF-8 bytes). Request a targeted next page with read offset=${nextOffset} and limit<=${RESULT_POLICY.read.maxLines}.]`;
  }
  if (event.toolName === "grep") {
    return `[Orchestration result truncated: showing ${outputStats.lines} of ${sourceStats.lines} lines (${outputStats.bytes} of ${sourceStats.bytes} UTF-8 bytes). Refine grep pattern/path/glob; matches are capped at ${RESULT_POLICY.grep.maxMatches} with context<=${RESULT_POLICY.grep.maxContext}. Use read for exact lines.]`;
  }
  return `[Orchestration result truncated: showing bounded beginning, failure diagnostics, and ending (${outputStats.bytes} of ${sourceStats.bytes} UTF-8 bytes). ${bashContinuation(fullOutputPath)}]`;
}

function inputObservation(inputPolicy) {
  const input = inputPolicy || {};
  return {
    input_capped: Boolean(input.input_capped),
    requested_limit: valueOrNull(input.requested_limit),
    effective_limit: valueOrNull(input.effective_limit),
    requested_context: valueOrNull(input.requested_context),
    effective_context: valueOrNull(input.effective_context),
  };
}

function resultObservation(event, policy, sourceStats, emittedStats, truncated, inputPolicy) {
  return {
    schema_version: 1,
    event: "result",
    tool: event.toolName,
    truncated,
    direction: policy.direction,
    source_bytes: sourceStats.bytes,
    source_lines: sourceStats.lines,
    emitted_bytes: emittedStats.bytes,
    emitted_lines: emittedStats.lines,
    ...inputObservation(inputPolicy),
  };
}

function unchangedResult(event, policy, sourceStats, inputPolicy) {
  return {
    observation: resultObservation(
      event, policy, sourceStats, sourceStats, false, inputPolicy,
    ),
    pending: undefined,
  };
}

function truncatedBy(sourceStats, policy) {
  return sourceStats.bytes > policy.maxBytes ? "bytes" : "lines";
}

function policyTruncation(payload, payloadStats, sourceStats, policy) {
  return {
    content: payload,
    truncated: true,
    truncatedBy: truncatedBy(sourceStats, policy),
    totalLines: sourceStats.lines,
    totalBytes: sourceStats.bytes,
    outputLines: payloadStats.lines,
    outputBytes: payloadStats.bytes,
    lastLinePartial: false,
    firstLineExceedsLimit: false,
    maxLines: policy.maxLines,
    maxBytes: policy.maxBytes,
  };
}

function resultDetails(event, payload, payloadStats, sourceStats, policy, fullOutputPath) {
  const details = {
    ...Object(event.details),
    truncation: policyTruncation(payload, payloadStats, sourceStats, policy),
  };
  if (event.toolName === "bash") details.fullOutputPath = fullOutputPath;
  return details;
}

async function truncatedResult(event, policy, source, sourceStats, inputPolicy) {
  const payload = boundedPayload(source, policy);
  const payloadStats = resultStats(payload);
  const fullOutputPath = event.toolName === "bash"
    ? await bashOutputPath(event, source)
    : undefined;
  const notice = continuationNotice(event, sourceStats, payloadStats, fullOutputPath);
  const text = `${payload}\n\n${notice}`;
  return {
    content: [{ type: "text", text }, ...nonTextContent(event.content)],
    details: resultDetails(
      event, payload, payloadStats, sourceStats, policy, fullOutputPath,
    ),
    observation: resultObservation(
      event, policy, sourceStats, resultStats(text), true, inputPolicy,
    ),
    pending: { tool: event.toolName, input: { ...event.input } },
  };
}

export async function applyToolResultPolicy(event, inputPolicy) {
  const policy = RESULT_POLICY[event.toolName];
  if (!policy) return undefined;
  const source = textContent(event.content);
  const sourceStats = resultStats(source);
  if (!needsTruncation(sourceStats, policy)) {
    return unchangedResult(event, policy, sourceStats, inputPolicy);
  }
  return truncatedResult(event, policy, source, sourceStats, inputPolicy);
}

function sameReadTarget(previous, current) {
  return previous.tool === "read" && current.toolName === "read"
    && previous.input.path === current.input.path;
}

function readPagination(previous, current) {
  if (!sameReadTarget(previous, current)) return false;
  const before = requestedPositiveNumber(previous.input.offset) ?? 1;
  const after = requestedPositiveNumber(current.input.offset) ?? 1;
  return after > before;
}

function refinedGrep(previous, current) {
  if (previous.tool !== "grep" || current.toolName !== "grep") return false;
  return ["pattern", "path", "glob"].some(
    (key) => previous.input[key] !== current.input[key],
  );
}

export function immediateFollowupObservation(previous, current) {
  if (!previous) return undefined;
  return {
    schema_version: 1,
    event: "immediate_followup",
    previous_tool: previous.tool,
    next_tool: RESULT_TOOLS.has(current.toolName) ? current.toolName : "other",
    same_read_target: sameReadTarget(previous, current),
    read_pagination: readPagination(previous, current),
    refined_grep: refinedGrep(previous, current),
  };
}
