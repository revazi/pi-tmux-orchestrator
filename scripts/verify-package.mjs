#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const expectedFiles = [
  "CHANGELOG.md",
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
  version: "0.4.0",
  description: "Pi extension, skill, and dependency-free Python CLI for coordinating coding agents in tmux",
  license: "UNLICENSED",
  keywords: [
    "pi-package",
    "pi",
    "tmux",
    "coding-agent",
    "multi-agent",
    "orchestration",
    "developer-tools",
  ],
  homepage: "https://github.com/revazi/pi-tmux-orchestrator#readme",
  repository: {
    type: "git",
    url: "git+https://github.com/revazi/pi-tmux-orchestrator.git",
  },
  bugs: { url: "https://github.com/revazi/pi-tmux-orchestrator/issues" },
  publishConfig: { access: "public" },
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
  "private",
  "scripts",
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
    throw new Error(`${label} drifted from the publish-ready package contract`);
  }
}

const manifest = JSON.parse(await readFile(resolve(root, "package.json"), "utf8"));
for (const field of forbiddenControlFields) {
  if (Object.hasOwn(manifest, field)) {
    throw new Error(`publish-ready package must not declare ${field}`);
  }
}
assertExact("package.json", manifest, expectedManifest);

const version = (await readFile(resolve(root, "VERSION"), "utf8")).trim();
const python = await readFile(resolve(root, "scripts/pi-tmux-agents.py"), "utf8");
const pythonVersion = python.match(/^VERSION = "([^"]+)"$/m)?.[1];
if (version !== expectedManifest.version || pythonVersion !== expectedManifest.version) {
  throw new Error("VERSION and Python CLI must match the exact package version");
}

const npmTemp = await mkdtemp(join(tmpdir(), "pi-tmux-verify-package-"));
const npmHome = resolve(npmTemp, "home");
const npmCache = resolve(npmTemp, "cache");
const userConfig = resolve(npmTemp, "user-npmrc");
const globalConfig = resolve(npmTemp, "global-npmrc");
await mkdir(npmHome);
await writeFile(userConfig, "", "utf8");
await writeFile(globalConfig, "", "utf8");
let packed;
try {
  packed = spawnSync("npm", ["pack", "--dry-run", "--json", "--ignore-scripts", "--offline"], {
    cwd: root,
    encoding: "utf8",
    env: {
      PATH: process.env.PATH || "/usr/bin:/bin",
      HOME: npmHome,
      TMPDIR: npmTemp,
      LANG: process.env.LANG || "C",
      npm_config_userconfig: userConfig,
      npm_config_globalconfig: globalConfig,
      npm_config_cache: npmCache,
      npm_config_update_notifier: "false",
    },
  });
} finally {
  await rm(npmTemp, { recursive: true, force: true });
}
if (packed.status !== 0) throw new Error("isolated offline npm pack dry-run failed");

const report = JSON.parse(packed.stdout);
if (!Array.isArray(report) || report.length !== 1 || !Array.isArray(report[0].files)) {
  throw new Error("unexpected npm pack JSON report");
}
if (report[0].name !== manifest.name || report[0].version !== manifest.version) {
  throw new Error("npm pack report name/version drifted from package.json");
}
if (!Array.isArray(report[0].bundled) || report[0].bundled.length !== 0) {
  throw new Error("package must not bundle dependencies");
}
const actualPacked = report[0].files.map((item) => item.path).sort();
const expectedPacked = [...expectedFiles, "package.json"].sort();
assertExact("packed file allowlist", actualPacked, expectedPacked);

const cliEntry = report[0].files.find((item) => item.path === manifest.bin["pi-tmux-agents"]);
if (!cliEntry || (cliEntry.mode & 0o111) === 0) {
  throw new Error("packed CLI bin must remain executable");
}

const forbiddenPath = /(^|\/)(?:tests?|\.github|orchestrations?|prompts?|sessions?|auth|credentials?|secrets?|node_modules|__pycache__|\.ruff_cache)(?:\/|$)|(^|\/)\.env(?:\.|$)|\.pyc$|\.tgz$|(^|\/)\.git(?:\/|$)/i;
for (const path of actualPacked) {
  if (forbiddenPath.test(path)) throw new Error(`forbidden package path: ${path}`);
}
console.log(
  `Verified exact publish-ready package ${manifest.version}: ${actualPacked.length} files, explicit public access, zero lifecycle/dependency surface.`,
);
