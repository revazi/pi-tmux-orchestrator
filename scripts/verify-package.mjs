#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const expectedFiles = [
  "CHANGELOG.md",
  "CONTRIBUTING.md",
  "README.md",
  "SECURITY.md",
  "SKILL.md",
  "VERSION",
  "extensions/tmux-orchestrator.js",
  "references/usage.md",
  "scripts/pi-tmux-agents.py",
];
const expectedManifest = {
  name: "@revazi/pi-tmux-orchestrator",
  version: "0.4.0-dev.0",
  description: "Unreleased private Pi package candidate for the Python/tmux orchestrator",
  private: true,
  license: "UNLICENSED",
  type: "module",
  engines: { node: ">=22.19" },
  os: ["darwin", "linux"],
  bin: { "pi-tmux-agents": "scripts/pi-tmux-agents.py" },
  files: expectedFiles,
  pi: {
    extensions: ["./extensions/tmux-orchestrator.js"],
    skills: ["./SKILL.md"],
  },
};
const forbiddenControlFields = [
  "scripts",
  "publishConfig",
  "dependencies",
  "devDependencies",
  "optionalDependencies",
  "peerDependencies",
  "bundledDependencies",
  "bundleDependencies",
];

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, canonical(value[key])]),
    );
  }
  return value;
}

function assertExact(label, actual, expected) {
  if (JSON.stringify(canonical(actual)) !== JSON.stringify(canonical(expected))) {
    throw new Error(`${label} drifted from the private candidate contract`);
  }
}

const manifest = JSON.parse(await readFile(resolve(root, "package.json"), "utf8"));
for (const field of forbiddenControlFields) {
  if (Object.hasOwn(manifest, field)) {
    throw new Error(`candidate must not declare ${field}`);
  }
}
assertExact("package.json", manifest, expectedManifest);

const version = (await readFile(resolve(root, "VERSION"), "utf8")).trim();
const python = await readFile(resolve(root, "scripts/pi-tmux-agents.py"), "utf8");
const pythonVersion = python.match(/^VERSION = "([^"]+)"$/m)?.[1];
if (version !== expectedManifest.version || pythonVersion !== expectedManifest.version) {
  throw new Error("VERSION and Python CLI must match the exact package candidate version");
}

const packed = spawnSync("npm", ["pack", "--dry-run", "--json", "--ignore-scripts"], {
  cwd: root,
  encoding: "utf8",
  env: { ...process.env, npm_config_update_notifier: "false" },
});
if (packed.status !== 0) throw new Error("npm pack dry-run failed");

const report = JSON.parse(packed.stdout);
if (!Array.isArray(report) || report.length !== 1 || !Array.isArray(report[0].files)) {
  throw new Error("unexpected npm pack JSON report");
}
if (!Array.isArray(report[0].bundled) || report[0].bundled.length !== 0) {
  throw new Error("candidate must not bundle dependencies");
}
const actualPacked = report[0].files.map((item) => item.path).sort();
const expectedPacked = [...expectedFiles, "package.json"].sort();
assertExact("packed file allowlist", actualPacked, expectedPacked);

const forbiddenPath = /(node_modules|(^|\/)tests?\/|orchestrations?|prompts?|sessions?|auth|credentials?|secrets?|\.env|__pycache__|\.pyc$|\.ruff_cache|\.git)/i;
for (const path of actualPacked) {
  if (forbiddenPath.test(path)) throw new Error(`forbidden package path: ${path}`);
}
console.log(
  `Verified exact private package ${manifest.version}: ${actualPacked.length} files, zero lifecycle/publication/dependency surface.`,
);
