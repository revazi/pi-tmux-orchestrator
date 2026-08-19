#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const pythonFiles = [
  "__init__.py",
  "__main__.py",
  "broker.py",
  "broker_client.py",
  "broker_store.py",
  "cli.py",
  "commands.py",
  "configuration.py",
  "constants.py",
  "controller.py",
  "dashboard.py",
  "models.py",
  "output.py",
  "prompts.py",
  "protocol.py",
  "relay.py",
  "rpc.py",
  "rpc_protocol.py",
  "rpc_store.py",
  "rpc_supervisor.py",
  "runtime.py",
  "storage.py",
  "supervisor_api.py",
  "supervisor_commands.py",
  "tmux.py",
].map((name) => `pi_tmux_orchestrator/${name}`);
const expectedFiles = [
  "CHANGELOG.md",
  "LICENSE.md",
  "README.md",
  "SECURITY.md",
  "SKILL.md",
  "VERSION",
  "bin/pi-tmux-agents",
  "extensions/tmux-orchestrator.js",
  "extensions/orchestrator-models.js",
  "extensions/orchestrator-update.js",
  "extensions/orchestrator-parent.js",
  "extensions/orchestrator-worker.js",
  "references/usage.md",
  "references/protocol-v1.md",
  "references/dashboard-design.md",
  ...pythonFiles,
];
const declaredFiles = [
  "CHANGELOG.md",
  "LICENSE.md",
  "README.md",
  "SECURITY.md",
  "SKILL.md",
  "VERSION",
  "extensions/tmux-orchestrator.js",
  "extensions/orchestrator-models.js",
  "extensions/orchestrator-update.js",
  "extensions/orchestrator-parent.js",
  "extensions/orchestrator-worker.js",
  "references/usage.md",
  "references/protocol-v1.md",
  "references/dashboard-design.md",
  "bin/pi-tmux-agents",
  "pi_tmux_orchestrator/*.py",
];
const expectedManifest = {
  name: "pi-tmux-orchestrator",
  version: "0.7.0",
  description: "Pi extension, skill, and dependency-free Python CLI for coordinating coding agents in tmux",
  license: "MIT",
  author: {
    name: "Revaz Zakalashvili",
    email: "revaz.zakalashvili@gmail.com",
    url: "https://github.com/revazi",
  },
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
  bin: { "pi-tmux-agents": "bin/pi-tmux-agents" },
  files: declaredFiles,
  pi: {
    extensions: ["./extensions/tmux-orchestrator.js"],
    skills: ["./SKILL.md"],
  },
};
const expectedLicense = `MIT License

Copyright (c) 2026 Revaz Zakalashvili

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
`;
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

const license = await readFile(resolve(root, "LICENSE.md"), "utf8");
if (license !== expectedLicense) {
  throw new Error("LICENSE.md drifted from the canonical MIT license contract");
}

const readme = await readFile(resolve(root, "README.md"), "utf8");
const requiredBadges = [
  "https://img.shields.io/npm/v/pi-tmux-orchestrator.svg",
  "https://img.shields.io/npm/dm/pi-tmux-orchestrator.svg",
  "https://github.com/revazi/pi-tmux-orchestrator/actions/workflows/ci.yml/badge.svg",
  "https://img.shields.io/badge/license-MIT-blue.svg",
];
for (const badge of requiredBadges) {
  if (!readme.includes(badge)) throw new Error(`README.md omitted required badge: ${badge}`);
}
const extensionSources = await Promise.all([
  "extensions/tmux-orchestrator.js",
  "extensions/orchestrator-models.js",
  "extensions/orchestrator-update.js",
  "extensions/orchestrator-parent.js",
  "extensions/orchestrator-worker.js",
].map((path) => readFile(resolve(root, path), "utf8")));
for (const method of ["setStatus", "setWidget", "setTitle"]) {
  if (extensionSources.some((source) => source.includes(`.${method}(`))) {
    throw new Error(`package extensions must not add persistent Pi UI chrome with ${method}`);
  }
}
const packagedDocs = [
  "README.md",
  "CHANGELOG.md",
  "SECURITY.md",
  "references/usage.md",
  "references/protocol-v1.md",
  "references/dashboard-design.md",
];
const unrelatedRoadmap = /\b(?:Pi Deck|Herdr|future architecture|neutral core|multiplexer-neutral|terminal-neutral|terminal-independent|terminal-client|process-host)\b/i;
for (const path of packagedDocs) {
  const content = await readFile(resolve(root, path), "utf8");
  if (unrelatedRoadmap.test(content)) {
    throw new Error(`${path} contains unrelated product or roadmap language`);
  }
}

const version = (await readFile(resolve(root, "VERSION"), "utf8")).trim();
const python = await readFile(resolve(root, "pi_tmux_orchestrator/constants.py"), "utf8");
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
  `Verified exact publish-ready package ${manifest.version}: ${actualPacked.length} files, MIT/author metadata, explicit public access, zero lifecycle/dependency surface.`,
);
