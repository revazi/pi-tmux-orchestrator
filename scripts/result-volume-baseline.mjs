#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  applyToolInputPolicy,
  applyToolResultPolicy,
} from "../extensions/orchestrator-result-policy.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const baselinePath = resolve(root, "tests/fixtures/result-volume-baseline.json");

function toolResult(toolName, text) {
  return { role: "toolResult", toolName, content: [{ type: "text", text }] };
}

function assistant(toolName, input) {
  return {
    role: "assistant",
    content: [{ type: "toolCall", id: `next-${toolName}`, name: toolName, arguments: input }],
  };
}

function serializedBytes(messages) {
  return Buffer.byteLength(JSON.stringify(messages), "utf8");
}

function sourceLines(prefix, count, width) {
  return Array.from(
    { length: count }, (_, index) => `${prefix} ${index + 1} λ😀 ${"x".repeat(width)}`,
  ).join("\n");
}

function scenario(toolName) {
  const definitions = {
    read: {
      input: { path: "synthetic-large.txt", limit: 5_000 },
      beforeSource: sourceLines("read", 500, 80),
      afterSource: sourceLines("read", 400, 80),
      nextInput: { path: "synthetic-large.txt", offset: 401, limit: 100 },
      nextResult: sourceLines("page", 100, 40),
    },
    grep: {
      input: { pattern: "broad", path: "src", limit: 5_000, context: 50 },
      beforeSource: sourceLines("src/file.py:10:", 450, 70),
      afterSource: sourceLines("src/file.py:10:", 200, 70),
      nextInput: { pattern: "specific", path: "src", limit: 20, context: 1 },
      nextResult: sourceLines("src/file.py:20:", 20, 40),
    },
    bash: {
      input: { command: "synthetic test" },
      beforeSource: sourceLines("progress", 500, 80).replace(
        "progress 251", "FAILED safety-critical synthetic assertion 251",
      ),
    },
  };
  return definitions[toolName];
}

async function measureScenario(toolName) {
  const definition = scenario(toolName);
  const afterSource = definition.afterSource || definition.beforeSource;
  const event = {
    toolName,
    input: { ...definition.input },
    content: [{ type: "text", text: afterSource }],
    details: toolName === "bash" ? { fullOutputPath: "/tmp/synthetic-full-output" } : undefined,
  };
  const inputPolicy = applyToolInputPolicy(event);
  const limited = await applyToolResultPolicy(event, inputPolicy);
  const beforeCalls = [[toolResult(toolName, definition.beforeSource)]];
  const afterCalls = [[toolResult(toolName, limited.content[0].text)]];
  if (definition.nextInput) {
    afterCalls.push([
      toolResult(toolName, limited.content[0].text),
      assistant(toolName, definition.nextInput),
      toolResult(toolName, definition.nextResult),
    ]);
  }
  const beforeBytes = beforeCalls.map(serializedBytes);
  const afterBytes = afterCalls.map(serializedBytes);
  const beforeTotal = beforeBytes.reduce((total, value) => total + value, 0);
  const afterTotal = afterBytes.reduce((total, value) => total + value, 0);
  return {
    before: { provider_calls: beforeCalls.length, by_call_utf8_bytes: beforeBytes, total_utf8_bytes: beforeTotal },
    after: { provider_calls: afterCalls.length, by_call_utf8_bytes: afterBytes, total_utf8_bytes: afterTotal },
    additional_provider_calls: afterCalls.length - beforeCalls.length,
    context_reduction_percent: Number(((1 - (afterTotal / beforeTotal)) * 100).toFixed(1)),
    result: limited.observation,
  };
}

function aggregateScenarios(scenarios) {
  const values = Object.values(scenarios);
  const beforeBytes = values.reduce((total, item) => total + item.before.total_utf8_bytes, 0);
  const afterBytes = values.reduce((total, item) => total + item.after.total_utf8_bytes, 0);
  return {
    before_provider_calls: values.reduce((total, item) => total + item.before.provider_calls, 0),
    after_provider_calls: values.reduce((total, item) => total + item.after.provider_calls, 0),
    additional_provider_calls: values.reduce((total, item) => total + item.additional_provider_calls, 0),
    before_utf8_bytes: beforeBytes,
    after_utf8_bytes: afterBytes,
    context_reduction_percent: Number(((1 - (afterBytes / beforeBytes)) * 100).toFixed(1)),
  };
}

export async function buildResultVolumeBaseline() {
  const entries = await Promise.all(
    ["read", "grep", "bash"].map(async (tool) => [tool, await measureScenario(tool)]),
  );
  const scenarios = Object.fromEntries(entries);
  return {
    schema_version: 1,
    metric_scope: "model-free-serialized-provider-context-proxy",
    caveat: "Serialized UTF-8 bytes and synthetic additional calls are deterministic proxies, not provider tokens, billing, quality, or production-wire acceptance.",
    scenarios,
    aggregate: aggregateScenarios(scenarios),
  };
}

function encodedBaseline(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

async function executeMode(mode, baseline) {
  if (mode === "--write") {
    await writeFile(baselinePath, encodedBaseline(baseline), "utf8");
    console.log(`Wrote ${baselinePath}`);
    return;
  }
  if (mode === "--check") {
    const expected = JSON.parse(await readFile(baselinePath, "utf8"));
    assert.deepEqual(
      baseline,
      expected,
      "Result-volume baseline drifted; inspect the change and run --write only when intentional.",
    );
    console.log(`Verified result-volume baseline: ${Object.keys(baseline.scenarios).length} scenarios.`);
    return;
  }
  if (mode === "print") {
    process.stdout.write(encodedBaseline(baseline));
    return;
  }
  throw new Error(`Unknown mode: ${mode}`);
}

async function main() {
  await executeMode(process.argv[2] || "print", await buildResultVolumeBaseline());
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
