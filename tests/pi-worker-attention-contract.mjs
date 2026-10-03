// Actual installed Pi hook + persistence methods with synthetic messages only.
// No AgentSession/ModelRegistry/AuthStorage construction, provider call, or auth reads.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { existsSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

let piRoot = process.argv[2] ?? dirname(realpathSync(execFileSync("which", ["pi"], { encoding: "utf8" }).trim()));
while (!existsSync(join(piRoot, "dist/core/extensions/runner.js"))) {
  if (process.argv[2] || piRoot === dirname(piRoot)) throw new Error("installed_pi_sdk_unavailable");
  piRoot = dirname(piRoot);
}
const { ExtensionRunner } = await import(pathToFileURL(join(piRoot, "dist/core/extensions/runner.js")));
const { AgentSession } = await import(pathToFileURL(join(piRoot, "dist/core/agent-session.js")));
const { SessionManager } = await import(pathToFileURL(join(piRoot, "dist/core/session-manager.js")));
const directory = await mkdtemp(join(tmpdir(), "pi-attention-contract-"));
const environment = { PI_TMUX_ORCHESTRATOR_ROLE: "implementer", PI_TMUX_ORCHESTRATOR_TOKEN: "a".repeat(32),
  PI_TMUX_ORCHESTRATOR_SOCKET: join(directory, "unused.sock"), PI_TMUX_ORCHESTRATOR_GENERATION: "1" };
const old = Object.fromEntries(Object.keys(environment).map((key) => [key, process.env[key]]));
Object.assign(process.env, environment);
try {
  const { default: worker } = await import("../extensions/orchestrator-worker.js");
  for (const mode of ["tui", "rpc"]) {
    const session = SessionManager.create(directory, join(directory, mode));
    const handlers = new Map();
    worker({ on: (name, hook) => handlers.set(name, [hook]), registerTool() {} });
    const runner = new ExtensionRunner([{ path: "synthetic-worker", handlers }], {}, directory, session, {});
    runner.mode = mode === "tui" ? "interactive" : "rpc";
    const errors = [];
    runner.onError((error) => errors.push(error));
    const agentSession = Object.create(AgentSession.prototype);
    agentSession._extensionRunner = runner;
    for (const argumentsValue of [{ reason: "blocked", summary: "PRIVATE_SUMMARY_CANARY", question: "PRIVATE_QUESTION_CANARY" },
      { reason: "invalid", raw: "PRIVATE_INVALID_ARGS_CANARY" }]) {
      const message = { role: "assistant", timestamp: 0, stopReason: "toolUse", content: [
        { type: "thinking", thinking: "PRIVATE_THINKING_CANARY", thinkingSignature: "PRIVATE_SIGNATURE_CANARY" },
        { type: "text", text: "PRIVATE_TEXT_CANARY" },
        { type: "toolCall", id: "call", name: "orchestrator_attention", arguments: argumentsValue },
      ] };
      const original = message;
      // This is Pi's real replacement path, including in-place agent-state mutation.
      await agentSession._emitExtensionEvent({ type: "message_end", message });
      assert.equal(message, original);
      assert.ok(!JSON.stringify(message).includes("PRIVATE_"));
      assert.deepEqual(message.content[0].arguments, { reason: argumentsValue.reason === "blocked" ? "blocked" : "report_failure" });
      // AgentSession appends this same message immediately after hook dispatch.
      session.appendMessage(message);
    }
    assert.equal(errors.length, 0);
    assert.ok(!JSON.stringify(session.getEntries()).includes("PRIVATE_"));
    assert.ok(!(await readFile(session.getSessionFile(), "utf8")).includes("PRIVATE_"));
  }
  console.log("Actual Pi TUI/RPC message_end replacement and disk-persistence contract passed (synthetic, provider-free).");
} finally {
  for (const [key, value] of Object.entries(old)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; }
  await rm(directory, { recursive: true, force: true });
}
