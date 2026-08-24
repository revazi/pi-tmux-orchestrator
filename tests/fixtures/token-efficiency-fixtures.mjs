const MESSAGE_TYPE = "pi-tmux-orchestrator-message-v1";

function textContentCharacters(content) {
  const blocks = Array.isArray(content) ? content : [];
  return sum(blocks.map((item) => item.text.length));
}

function serializedContextSize(messages) {
  const serialized = JSON.stringify(messages);
  return {
    characters: serialized.length,
    utf8Bytes: Buffer.byteLength(serialized, "utf8"),
  };
}

function providerCallMetrics(messages, filterWorkerContext) {
  const calls = [];
  for (const [index, item] of messages.entries()) {
    if (item?.role !== "assistant") continue;
    const visible = filterWorkerContext(messages.slice(0, index));
    calls.push({
      call: calls.length + 1,
      messages: visible.length,
      ...serializedContextSize(visible),
    });
  }
  return calls;
}

function resultCounter(results, tool) {
  if (!results.has(tool)) results.set(tool, { count: 0, characters: 0 });
  return results.get(tool);
}

function toolResultMetrics(messages) {
  const results = new Map();
  for (const item of messages.filter((message) => message?.role === "toolResult")) {
    const current = resultCounter(results, item.toolName);
    current.count += 1;
    current.characters += textContentCharacters(item.content);
  }
  return results;
}

function sum(values) {
  return values.reduce((total, value) => total + value, 0);
}

export function measureFixture(messages, filterWorkerContext) {
  const providerCalls = providerCallMetrics(messages, filterWorkerContext);
  const toolResults = toolResultMetrics(messages);
  const characters = providerCalls.map((item) => item.characters);
  const utf8Bytes = providerCalls.map((item) => item.utf8Bytes);
  const resultValues = [...toolResults.values()];
  return {
    provider_calls: providerCalls.length,
    provider_context: {
      by_call: providerCalls,
      total_characters: sum(characters),
      peak_characters: Math.max(0, ...characters),
      total_utf8_bytes: sum(utf8Bytes),
      peak_utf8_bytes: Math.max(0, ...utf8Bytes),
    },
    tool_results: {
      count: sum(resultValues.map((value) => value.count)),
      characters: sum(resultValues.map((value) => value.characters)),
      by_tool: Object.fromEntries(
        [...toolResults.entries()].sort(([left], [right]) => left.localeCompare(right)),
      ),
    },
  };
}

function orchestration(details, content) {
  return { role: "custom", customType: MESSAGE_TYPE, content, details };
}

function assistant(toolCalls, text = "") {
  const content = [];
  if (text) content.push({ type: "text", text });
  for (const [id, name, args] of toolCalls) {
    content.push({ type: "toolCall", id, name, arguments: args });
  }
  return { role: "assistant", content };
}

function toolResult(id, name, text) {
  return {
    role: "toolResult",
    toolCallId: id,
    toolName: name,
    content: [{ type: "text", text }],
    isError: false,
  };
}

function baseline() {
  return orchestration(
    { kind: "context", delivery_kind: "baseline", round: 1 },
    "Bounded task baseline with governing project constraints.",
  );
}

function assignment(id, round, kind = "implementation") {
  return orchestration(
    { kind: "assignment", assignment_id: id, assignment_kind: kind, round },
    `Active ${kind} assignment for round ${round}.`,
  );
}

function reportCall(id, kind = "implementation") {
  return assistant([[id, "orchestrator_report", { kind, summary: "Bounded synthetic result." }]]);
}

const FIRST_ASSIGNMENT = "a".repeat(32);
const SECOND_ASSIGNMENT = "b".repeat(32);

function simpleFixture() {
  return [
    baseline(),
    assignment(FIRST_ASSIGNMENT, 1),
    assistant([["simple-read", "read", { path: "src/core.py", limit: 200 }]]),
    toolResult("simple-read", "read", `λ😀${"r".repeat(1_997)}`),
    reportCall("simple-report"),
    toolResult("simple-report", "orchestrator_report", "accepted"),
  ];
}

function mediumFixture() {
  return [
    baseline(),
    assignment(FIRST_ASSIGNMENT, 1),
    assistant([
      ["medium-find", "find", { pattern: "*.py", path: "src" }],
      ["medium-grep", "grep", { pattern: "Coordinator", path: "src", limit: 40 }],
    ]),
    toolResult("medium-find", "find", "f".repeat(600)),
    toolResult("medium-grep", "grep", "g".repeat(600)),
    assistant([
      ["medium-read-1", "read", { path: "src/a.py", limit: 300 }],
      ["medium-read-2", "read", { path: "src/b.py", limit: 300 }],
      ["medium-read-3", "read", { path: "tests/test_core.py", limit: 300 }],
    ]),
    toolResult("medium-read-1", "read", "a".repeat(4_000)),
    toolResult("medium-read-2", "read", "b".repeat(4_000)),
    toolResult("medium-read-3", "read", "c".repeat(4_000)),
    assistant([["medium-edit", "edit", { path: "src/a.py", edits: [{ oldText: "old", newText: "new" }] }]]),
    toolResult("medium-edit", "edit", "updated"),
    assistant([["medium-test", "bash", { command: "python -m unittest tests.test_core" }]]),
    toolResult("medium-test", "bash", "t".repeat(2_000)),
    reportCall("medium-report"),
    toolResult("medium-report", "orchestrator_report", "accepted"),
  ];
}

function multiRoundFixture() {
  return [
    baseline(),
    assignment(FIRST_ASSIGNMENT, 1),
    assistant([["round-1-read", "read", { path: "src/large.py", limit: 1_000 }]]),
    toolResult("round-1-read", "read", "r".repeat(30_000)),
    assistant([["round-1-test", "bash", { command: "python -m unittest" }]]),
    toolResult("round-1-test", "bash", "t".repeat(15_000)),
    reportCall("round-1-report"),
    toolResult("round-1-report", "orchestrator_report", "accepted"),
    { role: "user", content: [{ type: "text", text: "Preserve this direct operator decision." }] },
    orchestration(
      { kind: "context", delivery_kind: "run_state", round: 2 },
      "Latest bounded reviewer evidence requests one focused repair.",
    ),
    assignment(SECOND_ASSIGNMENT, 2),
    assistant([["round-2-read", "read", { path: "src/large.py", offset: 400, limit: 120 }]]),
    toolResult("round-2-read", "read", "n".repeat(3_000)),
    reportCall("round-2-report"),
    toolResult("round-2-report", "orchestrator_report", "accepted"),
  ];
}

export const tokenEfficiencyFixtures = {
  simple: simpleFixture,
  medium: mediumFixture,
  multi_round: multiRoundFixture,
};
