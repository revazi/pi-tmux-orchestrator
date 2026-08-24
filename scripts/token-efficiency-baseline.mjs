#!/usr/bin/env node
import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { testHooks as workerHooks } from "../extensions/orchestrator-worker.js";
import {
  measureFixture,
  tokenEfficiencyFixtures,
} from "../tests/fixtures/token-efficiency-fixtures.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const baselinePath = resolve(root, "tests/fixtures/token-efficiency-baseline.json");

export function buildTokenEfficiencyBaseline() {
  return {
    schema_version: 1,
    metric_scope: "model-free-provider-context-proxy",
    caveat: "Serialized characters and UTF-8 bytes are deterministic proxies, not provider tokens, billing, or production-wire acceptance.",
    fixtures: Object.fromEntries(
      Object.entries(tokenEfficiencyFixtures).map(([name, fixture]) => [
        name,
        measureFixture(fixture(), workerHooks.filterWorkerContext),
      ]),
    ),
  };
}

function canonical(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

async function writeBaseline(baseline) {
  await writeFile(baselinePath, canonical(baseline), "utf8");
  console.log(`Wrote ${baselinePath}`);
}

async function checkBaseline(baseline) {
  const expected = JSON.parse(await readFile(baselinePath, "utf8"));
  if (canonical(expected) !== canonical(baseline)) {
    throw new Error("Token-efficiency baseline drifted; inspect the change and run --write only when intentional.");
  }
  console.log(`Verified token-efficiency baseline: ${Object.keys(baseline.fixtures).length} fixtures.`);
}

function printBaseline(baseline) {
  process.stdout.write(canonical(baseline));
}

async function main() {
  const mode = process.argv[2] || "print";
  const handlers = { "--write": writeBaseline, "--check": checkBaseline, print: printBaseline };
  const handler = handlers[mode];
  if (!handler) throw new Error(`Unknown mode: ${mode}`);
  await handler(buildTokenEfficiencyBaseline());
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
