#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const fixturePath = resolve(root, "tests/fixtures/worker-prompt-baseline.json");
const beforePromptPath = resolve(root, "tests/fixtures/worker-prompt-before.md");
const tools = "read,bash,grep,find,ls,orchestrator_report";

function checked(result, label) {
  if (result.status !== 0) {
    throw new Error(`${label} failed (${result.status}): ${result.stderr || result.stdout}`);
  }
  return result.stdout;
}

function runPi(args, options = {}) {
  return checked(spawnSync("pi", args, { encoding: "utf8", ...options }), "pi");
}

function workerPromptRuntimeAvailable() {
  return spawnSync("pi", ["--version"], { encoding: "utf8" }).status === 0;
}

function generateAfterPrompt() {
  return checked(spawnSync("python3", [
    "-c",
    "from pathlib import Path; from pi_tmux_orchestrator.prompts import role_system_prompt; print(role_system_prompt(Path('<PROJECT>'), 'reviewer'), end='')",
  ], { cwd: root, encoding: "utf8" }), "python3");
}

function normalizePrompt(prompt, temporaryRoot) {
  const variants = [temporaryRoot, `/private${temporaryRoot}`].sort((a, b) => b.length - a.length);
  let normalized = prompt;
  for (const value of variants) normalized = normalized.split(value).join("<TMP>");
  return normalized
    .replace(/Main documentation: .*\/README\.md/g, "Main documentation: <PI>/README.md")
    .replace(/Additional docs: .*\/docs/g, "Additional docs: <PI>/docs")
    .replace(/Examples: .*\/examples/g, "Examples: <PI>/examples");
}

function size(value) {
  return { characters: value.length, utf8_bytes: Buffer.byteLength(value, "utf8") };
}

async function capture(project, agentDir, extension, output, args) {
  runPi([
    "-p", "--no-session", "--no-approve", "--no-extensions", "--extension", extension,
    "--tools", tools, ...args, "/capture-worker-prompt",
  ], {
    cwd: project,
    env: {
      ...process.env,
      PI_CODING_AGENT_DIR: agentDir,
      PI_OFFLINE: "1",
      PI_TELEMETRY: "0",
      WORKER_PROMPT_CAPTURE: output,
    },
  });
  return JSON.parse(await readFile(output, "utf8"));
}

async function prepareFixture(temporaryRoot) {
  const project = join(temporaryRoot, "project");
  const agentDir = join(temporaryRoot, "agent");
  const discoveredDir = join(agentDir, "skills", "discovered");
  const optedDir = join(temporaryRoot, "opted");
  await Promise.all([
    mkdir(project, { recursive: true }),
    mkdir(discoveredDir, { recursive: true }),
    mkdir(optedDir, { recursive: true }),
  ]);
  await writeFile(join(project, "AGENTS.md"), "# Synthetic context\nWORKER_CONTEXT_CANARY\n", "utf8");
  await writeFile(
    join(discoveredDir, "SKILL.md"),
    "---\nname: discovered\ndescription: Must disappear after discovery is disabled.\n---\n",
    "utf8",
  );
  const optedSkill = join(optedDir, "SKILL.md");
  await writeFile(
    optedSkill,
    "---\nname: opted\ndescription: Explicit reviewed worker skill.\n---\n",
    "utf8",
  );
  const extension = join(temporaryRoot, "capture.mjs");
  await writeFile(extension, `
    import { writeFileSync } from "node:fs";
    export default function (pi) {
      pi.registerTool({
        name: "orchestrator_report", label: "Report", description: "Synthetic report",
        parameters: { type: "object", additionalProperties: false, properties: {} },
        async execute() { return { content: [{ type: "text", text: "ok" }] }; },
      });
      pi.registerCommand("capture-worker-prompt", {
        handler: async (_args, ctx) => {
          const options = ctx.getSystemPromptOptions();
          writeFileSync(process.env.WORKER_PROMPT_CAPTURE, JSON.stringify({
            prompt: ctx.getSystemPrompt(),
            options: {
              customPrompt: options.customPrompt ?? null,
              appendSystemPrompt: options.appendSystemPrompt ?? null,
              selectedTools: options.selectedTools ?? [],
              contextFiles: (options.contextFiles ?? []).map((item) => ({ path: item.path, content: item.content })),
              skills: (options.skills ?? []).map((item) => ({ name: item.name, path: item.filePath })),
            },
          }));
        },
      });
    }
  `, "utf8");
  return { project, agentDir, optedSkill, extension };
}

function ensure(condition, message) {
  if (!condition) throw new Error(message);
}

function validateBefore(before) {
  ensure(before.options.customPrompt === null, "before fixture unexpectedly used a custom prompt");
  ensure(
    before.options.appendSystemPrompt?.includes("Role: `reviewer`"),
    "before fixture did not append role guidance",
  );
  ensure(
    before.options.skills.some((skill) => skill.name === "discovered"),
    "before fixture did not discover the synthetic global skill",
  );
}

function validateAfter(after, generated) {
  ensure(after.options.customPrompt === generated, "after fixture did not use the lean custom worker prompt");
  ensure(after.options.appendSystemPrompt === null, "after fixture unexpectedly appended role guidance");
  ensure(after.options.skills.length === 1, "after fixture loaded multiple skills");
  ensure(after.options.skills[0].name === "opted", "after fixture omitted the explicit skill");
  ensure(
    after.options.contextFiles.some((item) => item.content.includes("WORKER_CONTEXT_CANARY")),
    "governing context-file discovery was disabled",
  );
  ensure(!after.options.selectedTools.includes("edit"), "read-only prompt fixture gained edit");
  ensure(!after.options.selectedTools.includes("write"), "read-only prompt fixture gained write");
  ensure(
    after.options.selectedTools.includes("orchestrator_report"),
    "worker report tool is absent from the lean prompt fixture",
  );
  ensure(after.prompt.startsWith(generated), "Pi did not preserve the lean prompt prefix");
  ensure(after.prompt.includes("WORKER_CONTEXT_CANARY"), "Pi omitted governing context");
}

function baselineData(piVersion, beforePrompt, afterPrompt) {
  const beforeSize = size(beforePrompt);
  const afterSize = size(afterPrompt);
  return {
    schema_version: 1,
    metric_scope: "model-free-built-worker-system-prompt",
    caveat: "Normalized serialized characters and UTF-8 bytes are deterministic prompt-size proxies, not provider tokens, billing, cache efficiency, or production-wire acceptance.",
    pi_version: piVersion,
    role: "reviewer",
    before: { ...beforeSize, skill_discovery: true, loaded_skills: ["discovered"] },
    after: { ...afterSize, skill_discovery: false, loaded_skills: ["opted"] },
    reduction: {
      characters: beforeSize.characters - afterSize.characters,
      utf8_bytes: beforeSize.utf8_bytes - afterSize.utf8_bytes,
      character_percent: Number((((beforeSize.characters - afterSize.characters) / beforeSize.characters) * 100).toFixed(1)),
    },
  };
}

async function buildWorkerPromptBaseline() {
  const temporaryRoot = await mkdtemp(join(tmpdir(), "pi-worker-prompt-"));
  try {
    const fixture = await prepareFixture(temporaryRoot);
    const before = await capture(
      fixture.project, fixture.agentDir, fixture.extension, join(temporaryRoot, "before.json"),
      ["--append-system-prompt", beforePromptPath],
    );
    const generated = generateAfterPrompt();
    const afterPromptPath = join(temporaryRoot, "after.md");
    await writeFile(afterPromptPath, generated, "utf8");
    const after = await capture(
      fixture.project, fixture.agentDir, fixture.extension, join(temporaryRoot, "after.json"),
      ["--no-skills", "--skill", fixture.optedSkill, "--system-prompt", afterPromptPath],
    );
    validateBefore(before);
    validateAfter(after, generated);
    return baselineData(
      runPi(["--version"]).trim(),
      normalizePrompt(before.prompt, temporaryRoot),
      normalizePrompt(after.prompt, temporaryRoot),
    );
  } finally {
    await rm(temporaryRoot, { recursive: true, force: true });
  }
}

export async function buildWorkerPromptBaselineIfAvailable() {
  if (!workerPromptRuntimeAvailable()) return null;
  return buildWorkerPromptBaseline();
}

function canonical(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

async function writeBaseline(baseline) {
  await writeFile(fixturePath, canonical(baseline), "utf8");
  console.log(`Wrote ${fixturePath}`);
}

async function checkBaseline(baseline) {
  const expected = JSON.parse(await readFile(fixturePath, "utf8"));
  if (canonical(expected) !== canonical(baseline)) {
    throw new Error("Worker prompt baseline drifted; inspect and use --write only for intentional prompt/resource changes.");
  }
  console.log(`Verified worker prompt baseline: ${baseline.before.characters} -> ${baseline.after.characters} normalized characters.`);
}

function modeHandler(mode) {
  const handlers = { "--write": writeBaseline, "--check": checkBaseline, print: async (value) => process.stdout.write(canonical(value)) };
  const handler = handlers[mode];
  if (!handler) throw new Error(`Unknown mode: ${mode}`);
  return handler;
}

function unavailableBaseline(mode) {
  if (mode === "--write") throw new Error("Cannot write the worker prompt baseline without Pi");
  console.log("SKIP actual-Pi worker prompt baseline check (pi not available).");
}

async function main() {
  const mode = process.argv[2] || "print";
  const handler = modeHandler(mode);
  const baseline = await buildWorkerPromptBaselineIfAvailable();
  if (baseline === null) return unavailableBaseline(mode);
  await handler(baseline);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
