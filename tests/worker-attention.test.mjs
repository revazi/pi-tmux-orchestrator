import assert from "node:assert/strict";
import { EventEmitter, once } from "node:events";
import { mkdtemp, rm } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { attentionParameters, normalizeAttention, redactAttentionMessage, restoreAttentionState, ATTENTION_ENTRY, validReminder } from "../extensions/orchestrator-worker-attention.js";
import { workerFrame } from "../extensions/orchestrator-worker-protocol.js";
import { validateObserverFrame } from "../extensions/orchestrator-parent-protocol.js";
import { parentUpdateContent } from "../extensions/orchestrator-parent-content.js";

const assignmentId = "a".repeat(32);
const metadata = {
  assignment_id: assignmentId, assignment_kind: "implementation", settlement_count: 1,
  report_attempt: "attention", attention_reason: "clarification", reminder_state: "none", activity_phase: "tool",
};

test("attention is strict, Unicode-bounded, redacted before Pi history, and restored body-free", () => {
  assert.equal(attentionParameters.additionalProperties, false);
  for (const reason of attentionParameters.properties.reason.enum) {
    assert.deepEqual(normalizeAttention({ reason, summary: "😀".repeat(500) }), { reason, summary: "😀".repeat(500) });
  }
  for (const input of [null, [], {}, { reason: [] }, { reason: "other" }, { reason: "blocked", extra: "secret" },
    ...[null, 1, "", " ", "x".repeat(501), "\0", "\n", "\x1b", "\x7f", "\x80", "\ud800"].map((question) => ({ reason: "blocked", question }))]) {
    assert.throws(() => normalizeAttention(input), /invalid_worker_attention/);
  }
  const original = { role: "assistant", content: [{ type: "thinking", thinking: "PRIVATE_CANARY" },
    { type: "text", text: "PRIVATE_CANARY" }, { type: "toolCall", id: "call", name: "orchestrator_attention", arguments: { reason: "blocked", question: "PRIVATE_CANARY" } }] };
  let captured;
  const replacement = redactAttentionMessage(original, (_id, value) => { captured = value; });
  assert.equal(captured.question, "PRIVATE_CANARY");
  assert.ok(!JSON.stringify(replacement).includes("PRIVATE_CANARY"));
  const restored = restoreAttentionState([{ type: "custom", customType: ATTENTION_ENTRY, data: { assignment_id: assignmentId, recovery: true, settlement: "b".repeat(32) } },
    { type: "custom", customType: ATTENTION_ENTRY, data: { assignment_id: "f".repeat(32), recovery: false } }], assignmentId);
  assert.deepEqual(restored, { recovery: true, reminderSeen: false, settlement: "b".repeat(32), resumeId: null });
  assert.equal(validReminder({ version: 1, type: "report_reminder", id: assignmentId, assignment_id: assignmentId }, assignmentId), true);
  assert.equal(validReminder({ version: 1, type: "report_reminder", id: assignmentId, assignment_id: assignmentId, text: "private" }, assignmentId), false);
  assert.equal(validReminder({ version: 1, type: "report_reminder", id: assignmentId, assignment_id: assignmentId }, assignmentId, "c".repeat(32)), false);
  assert.deepEqual(restoreAttentionState([{ type: "custom", customType: ATTENTION_ENTRY }], undefined),
    { recovery: false, reminderSeen: false, settlement: undefined, resumeId: null });
  const bounded = restoreAttentionState([{ type: "custom", customType: ATTENTION_ENTRY, data: { assignment_id: assignmentId,
    recovery: "private", reminder_seen: "private", resume_id: "PRIVATE_CANARY", settlement: "PRIVATE_CANARY" } }], assignmentId);
  assert.ok(!JSON.stringify(bounded).includes("PRIVATE_CANARY"));
});

test("live parent frames reject malformed/private metadata and parent update is prose-free", () => {
  const state = { version: 1, type: "assignment_state", session: "s", role: "implementer", state: "waiting", assignment: metadata };
  assert.equal(validateObserverFrame(state, "s", "b".repeat(32)), state);
  const attention = { version: 1, type: "attention", session: "s", role: "implementer", assignment_id: assignmentId, attention: { reason: "blocked", question: "PRIVATE_CANARY" } };
  assert.equal(validateObserverFrame(attention, "s", "b".repeat(32)), attention);
  for (const assignment of [{ ...metadata, question: "private" }, { ...metadata, settlement_count: 3 }, { ...metadata, report_attempt: "invalid" }, { ...metadata, attention_reason: [] }, { ...metadata, activity_phase: "raw" }]) {
    assert.throws(() => validateObserverFrame({ ...state, assignment }, "s", "b".repeat(32)));
  }
  const update = parentUpdateContent("s", "needs_attention", 1, [], [{ role: "implementer", state: "waiting", assignment: metadata }]);
  assert.match(update.content, /settlements=1 phase=tool attempt=attention reason=clarification/);
  assert.match(update.content, /not task completion/);
  assert.ok(!update.content.includes("PRIVATE_CANARY"));
});

let harnessSequence = 0;
async function workerHarness(t, mode) {
  const directory = await mkdtemp(join(tmpdir(), "worker-attention-"));
  const socketPath = join(directory, "broker.sock");
  const incoming = new EventEmitter();
  const frames = [];
  let peer;
  let allowance = 1;
  const server = net.createServer((connection) => {
    peer = connection;
    let buffer = Buffer.alloc(0);
    connection.on("data", (chunk) => {
      buffer = Buffer.concat([buffer, chunk]);
      while (buffer.length >= 4 && buffer.length >= buffer.readUInt32BE(0) + 4) {
        const size = buffer.readUInt32BE(0);
        const frame = JSON.parse(buffer.subarray(4, size + 4).toString());
        buffer = buffer.subarray(size + 4);
        frames.push(frame);
        const success = frame.type !== "recovery_turn" || allowance-- > 0;
        connection.write(workerFrame({ version: 1, type: "response", id: frame.id, success, status: success ? "recorded" : "conflict" }));
        incoming.emit(frame.type, frame);
        if (frame.type === "ack") incoming.emit(`delivery:${frame.delivery_id}`, frame);
      }
    });
  });
  server.listen(socketPath);
  await once(server, "listening");
  const environment = { PI_TMUX_ORCHESTRATOR_ROLE: "implementer", PI_TMUX_ORCHESTRATOR_TOKEN: "b".repeat(32), PI_TMUX_ORCHESTRATOR_SOCKET: socketPath, PI_TMUX_ORCHESTRATOR_GENERATION: "1" };
  const old = Object.fromEntries(Object.keys(environment).map((key) => [key, process.env[key]]));
  Object.assign(process.env, environment);
  const { default: worker } = await import(`../extensions/orchestrator-worker.js?attention-${mode}-${++harnessSequence}`);
  for (const [key, value] of Object.entries(old)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; }
  const hooks = new Map();
  const tools = new Map();
  const entries = [];
  const messages = [];
  const aborts = [];
  const pi = { on: (name, hook) => hooks.set(name, hook), registerTool: (tool) => tools.set(tool.name, tool),
    getActiveTools: () => ["read", "write", "orchestrator_report", "orchestrator_attention"], setActiveTools: () => {},
    appendEntry: (customType, data) => entries.push({ type: "custom", customType, data }), sendMessage: (value) => messages.push(value) };
  const ctx = { mode, sessionManager: { getEntries: () => entries, getBranch: () => [] }, getContextUsage: () => undefined, isIdle: () => true, abort: () => aborts.push(true) };
  worker(pi);
  t.after(async () => { hooks.get("session_shutdown")(); peer?.destroy(); await new Promise((resolve) => server.close(resolve)); await rm(directory, { recursive: true, force: true }); });
  const hello = once(incoming, "hello", { signal: AbortSignal.timeout(3000) });
  hooks.get("session_start")({}, ctx);
  await hello;
  const ack = once(incoming, "ack", { signal: AbortSignal.timeout(3000) });
  peer.write(workerFrame({ version: 1, type: "assignment", id: "c".repeat(32), assignment_id: assignmentId, kind: "implementation", round: 1, content: "synthetic assignment", trigger: true }));
  await ack;
  return { hooks, tools, entries, messages, frames, incoming, ctx, aborts, send: (value) => peer.write(workerFrame(value)),
    async reconnect() {
      const connected = once(incoming, "hello", { signal: AbortSignal.timeout(3000) });
      peer.destroy();
      await connected;
    },
    async restart() {
      hooks.get("session_shutdown")();
      worker(pi); // same retained Pi entries, fresh extension closure
      const connected = once(incoming, "hello", { signal: AbortSignal.timeout(3000) });
      hooks.get("session_start")({}, ctx);
      await connected;
    },
  };
}

for (const mode of ["tui", "rpc"]) {
  test(`${mode} worker attention and one-turn recovery share actual framed transport and bounded journals`, async (t) => {
    const h = await workerHarness(t, mode);
    h.hooks.get("agent_start")({}, h.ctx);
    const settled = once(h.incoming, "settlement", { signal: AbortSignal.timeout(3000) });
    h.hooks.get("agent_settled")({}, h.ctx);
    await settled;
    h.hooks.get("agent_settled")({}, h.ctx); // duplicate must not create another settlement
    const reminder = { version: 1, type: "report_reminder", id: assignmentId, assignment_id: assignmentId };
    h.send(reminder);
    // A subsequent ack acts as a transport barrier, without polling.
    const ack = once(h.incoming, "ack", { signal: AbortSignal.timeout(3000) });
    h.send({ version: 1, type: "context", id: "d".repeat(32), kind: "baseline", round: 1, content: "synthetic baseline", trigger: false });
    await ack;
    const system = h.hooks.get("before_agent_start")({ systemPrompt: "base" });
    assert.match(system.systemPrompt, /one recovery provider turn/);
    h.hooks.get("agent_start")({}, h.ctx);
    await h.hooks.get("before_provider_request")({}, h.ctx);
    await assert.rejects(h.hooks.get("before_provider_request")({}, h.ctx), /exhausted/);
    assert.equal(h.aborts.length, 1);
    const second = once(h.incoming, "settlement", { signal: AbortSignal.timeout(3000) });
    h.hooks.get("agent_settled")({}, h.ctx);
    await second;
    const guidanceBarrier = once(h.incoming, `delivery:${"3".repeat(32)}`, { signal: AbortSignal.timeout(3000) });
    h.send({ version: 1, type: "context", id: "1".repeat(32), kind: "operator_message", assignment_id: assignmentId, round: 1, content: "explicit guidance", trigger: true });
    h.send(reminder); // late duplicate after guidance must not mint another reminder
    h.send({ version: 1, type: "context", id: "2".repeat(32), kind: "operator_message", assignment_id: "f".repeat(32), round: 1, content: "STALE_GUIDANCE_CANARY", trigger: true });
    h.send({ version: 1, type: "context", id: "3".repeat(32), kind: "baseline", round: 1, content: "barrier", trigger: false });
    await guidanceBarrier;
    assert.equal(h.messages.filter((item) => item.details?.kind === "report_reminder").length, 1);
    assert.ok(!JSON.stringify(h.messages).includes("STALE_GUIDANCE_CANARY"));
    const replacement = h.hooks.get("message_end")({ message: { role: "assistant", content: [{ type: "text", text: "PRIVATE_CANARY" }, { type: "toolCall", id: "attention-call", name: "orchestrator_attention", arguments: { reason: "clarification", question: "PRIVATE_CANARY" } }] } }, h.ctx);
    const result = await h.tools.get("orchestrator_attention").execute("attention-call", replacement.message.content[0].arguments);
    assert.equal(result.terminate, true);
    assert.equal(h.hooks.get("tool_call")({ toolName: "read" }, h.ctx).terminate, true);
    assert.ok(!JSON.stringify([replacement, result, h.entries, h.messages]).includes("PRIVATE_CANARY"));
    assert.equal(h.frames.filter((frame) => frame.type === "settlement").length, 2);
    assert.equal(h.frames.find((frame) => frame.type === "attention").attention.question, "PRIVATE_CANARY");
    const schemaResult = await h.hooks.get("tool_result")({ toolName: "orchestrator_report", toolCallId: "invalid", isError: true, content: [{ type: "text", text: "PRIVATE_VALIDATION_CANARY" }] });
    assert.ok(!JSON.stringify(schemaResult).includes("PRIVATE_VALIDATION_CANARY"));
    const report = await h.tools.get("orchestrator_report").execute("report-call", { kind: "implementation", summary: "done" }, undefined, undefined, h.ctx);
    assert.equal(report.terminate, true);
    await assert.rejects(h.tools.get("orchestrator_report").execute("report-call", { kind: "implementation", summary: "done" }, undefined, undefined, h.ctx), /no_active/);
  });
}

for (const mode of ["tui", "rpc"]) {
  for (const cause of ["attention", "used_reminder"]) {
    test(`${mode} explicit resume after ${cause} survives duplicate assignment, reconnect and worker handover`, async (t) => {
      const h = await workerHarness(t, mode);
      h.hooks.get("agent_start")({}, h.ctx);
      if (cause === "attention") {
        const replacement = h.hooks.get("message_end")({ message: { role: "assistant", content: [
          { type: "toolCall", id: "call", name: "orchestrator_attention", arguments: { reason: "blocked", summary: "PRIVATE_CANARY" } },
        ] } }, h.ctx);
        await h.tools.get("orchestrator_attention").execute("call", replacement.message.content[0].arguments);
      } else {
        const settled = once(h.incoming, "settlement", { signal: AbortSignal.timeout(3000) });
        h.hooks.get("agent_settled")({}, h.ctx);
        await settled;
        const barrier = once(h.incoming, "ack", { signal: AbortSignal.timeout(3000) });
        h.send({ version: 1, type: "report_reminder", id: assignmentId, assignment_id: assignmentId });
        h.send({ version: 1, type: "context", id: "d".repeat(32), kind: "baseline", round: 1, content: "barrier", trigger: false });
        await barrier;
        h.hooks.get("agent_start")({}, h.ctx);
        await h.hooks.get("before_provider_request")({}, h.ctx);
      }
      const resumeId = "7".repeat(32);
      const guidance = once(h.incoming, `delivery:${resumeId}`, { signal: AbortSignal.timeout(3000) });
      h.send({ version: 1, type: "context", id: resumeId, kind: "operator_message", assignment_id: assignmentId,
        resume_id: resumeId, round: 1, content: "synthetic guidance", trigger: true });
      await guidance;
      const settlements = h.frames.filter((frame) => frame.type === "settlement").length;
      h.hooks.get("agent_settled")({}, h.ctx); // late settlement of the pre-guidance run
      for (const recovery of ["duplicate", "reconnect", "restart"]) {
        if (recovery !== "duplicate") await h[recovery]();
        const deliveryId = recovery === "restart" ? "e".repeat(32) : "c".repeat(32);
        const ack = once(h.incoming, `delivery:${deliveryId}`, { signal: AbortSignal.timeout(3000) });
        h.send({ version: 1, type: "report_reminder", id: assignmentId, assignment_id: assignmentId }); // stale epoch
        h.send({ version: 1, type: "assignment", id: deliveryId, assignment_id: assignmentId, kind: "implementation",
          round: 1, content: "synthetic assignment", trigger: true, recovery_waiting: false, resume_id: resumeId });
        await ack;
        await h.hooks.get("before_provider_request")({}, h.ctx);
        assert.equal(h.aborts.length, 0);
        assert.equal(h.frames.filter((frame) => frame.type === "recovery_turn").length, cause === "used_reminder" ? 1 : 0);
      }
      assert.equal(h.frames.filter((frame) => frame.type === "settlement").length, settlements);
      assert.equal(restoreAttentionState(h.entries, assignmentId).recovery, false);
      assert.equal(restoreAttentionState(h.entries, assignmentId).resumeId, resumeId);
      assert.ok(!JSON.stringify(h.entries).includes("PRIVATE_CANARY"));
      h.hooks.get("agent_start")({}, h.ctx);
      const settled = once(h.incoming, "settlement", { signal: AbortSignal.timeout(3000) });
      h.hooks.get("agent_settled")({}, h.ctx);
      assert.equal((await settled)[0].resume_id, resumeId);
      await h.tools.get("orchestrator_report").execute("report", { kind: "implementation", summary: "done" }, undefined, undefined, h.ctx);
    });
  }
}

test("late report-schema result cannot classify a newer assignment as rejected", async (t) => {
  const h = await workerHarness(t, "tui");
  h.hooks.get("message_end")({ message: { role: "assistant", content: [
    { type: "toolCall", id: "old-report", name: "orchestrator_report", arguments: { kind: "implementation", summary: "done" } },
  ] } }, h.ctx);
  await h.tools.get("orchestrator_report").execute("old-report", { kind: "implementation", summary: "done" }, undefined, undefined, h.ctx);
  const nextAssignment = "f".repeat(32);
  const ack = once(h.incoming, "ack", { signal: AbortSignal.timeout(3000) });
  h.send({ version: 1, type: "assignment", id: "e".repeat(32), assignment_id: nextAssignment, kind: "implementation", round: 2,
    content: "synthetic next assignment", trigger: true });
  await ack;
  const error = { toolName: "orchestrator_report", toolCallId: "old-report", isError: true,
    content: [{ type: "text", text: "PRIVATE_VALIDATION_CANARY" }] };
  const result = await h.hooks.get("tool_result")(error);
  h.hooks.get("message_end")({ message: { role: "assistant", content: [
    { type: "toolCall", id: "current-report", name: "orchestrator_report", arguments: { raw: "PRIVATE_ARGS_CANARY" } },
  ] } }, h.ctx);
  const rejected = once(h.incoming, "rejected_report", { signal: AbortSignal.timeout(3000) });
  await h.hooks.get("tool_result")({ ...error, toolCallId: "current-report" });
  assert.equal((await rejected)[0].assignment_id, nextAssignment);
  assert.equal(h.frames.filter((frame) => frame.type === "rejected_report").length, 1);
  assert.ok(!JSON.stringify([result, h.frames, h.entries]).includes("PRIVATE_"));
});

test("accepted completion replay clears only its matching assignment without synthesizing a report", async (t) => {
  const h = await workerHarness(t, "tui");
  const stale = { version: 1, type: "assignment_closed", assignment_id: "f".repeat(32), report_id: "e".repeat(32) };
  h.send(stale);
  h.send({ ...stale, assignment_id: assignmentId, raw: "PRIVATE_CANARY" });
  const firstAck = once(h.incoming, "ack", { signal: AbortSignal.timeout(3000) });
  h.send({ version: 1, type: "context", id: "f".repeat(32), kind: "baseline", round: 1, content: "barrier", trigger: false });
  await firstAck;
  assert.equal(h.entries.some((entry) => entry.data.kind === "report"), false);
  h.send({ version: 1, type: "assignment_closed", assignment_id: assignmentId, report_id: "e".repeat(32) });
  const ack = once(h.incoming, "ack", { signal: AbortSignal.timeout(3000) });
  h.send({ version: 1, type: "context", id: "e".repeat(32), kind: "baseline", round: 1, content: "barrier", trigger: false });
  await ack;
  await assert.rejects(h.tools.get("orchestrator_report").execute("call", { kind: "implementation", summary: "done" }), /no_active/);
  assert.equal(h.frames.some((frame) => frame.type === "report"), false);
  assert.equal(h.entries.filter((entry) => entry.data.kind === "report").length, 1);
  assert.ok(!JSON.stringify(h.entries).includes("PRIVATE_CANARY"));
});
