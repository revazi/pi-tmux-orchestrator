#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  printf 'Usage: %s INSTALLED_PACKAGE_ROOT\n' "$0" >&2
  exit 2
fi
PACKAGE_ROOT=$(cd "$1" && pwd)
test -f "$PACKAGE_ROOT/package.json"
test -f "$PACKAGE_ROOT/extensions/tmux-orchestrator.js"
test -f "$PACKAGE_ROOT/SKILL.md"

if ! command -v pi >/dev/null 2>&1; then
  printf '%s\n' 'SKIP isolated Pi local-package install/RPC discovery smoke (pi not available).'
  exit 0
fi
TEMP=$(mktemp -d "${TMPDIR:-/tmp}/pi-tmux-extension-smoke.XXXXXX")
cleanup() {
  rm -rf "$TEMP"
}
trap cleanup EXIT
chmod 700 "$TEMP"
mkdir -p \
  "$TEMP/pi-home" "$TEMP/home" "$TEMP/workspace" \
  "$TEMP/xdg-config" "$TEMP/xdg-cache" "$TEMP/xdg-data" "$TEMP/npm-cache"
: > "$TEMP/user-npmrc"
: > "$TEMP/global-npmrc"

python3 - \
  "$PACKAGE_ROOT" "$TEMP/pi-home" "$TEMP/home" "$TEMP/workspace" \
  "$TEMP/user-npmrc" "$TEMP/global-npmrc" "$TEMP/npm-cache" \
  "$TEMP/xdg-config" "$TEMP/xdg-cache" "$TEMP/xdg-data" <<'PY'
import json
import os
from pathlib import Path
import subprocess
import sys

(
    root,
    pi_home,
    home,
    workspace,
    user_npmrc,
    global_npmrc,
    npm_cache,
    xdg_config,
    xdg_cache,
    xdg_data,
) = sys.argv[1:]
environment = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": home,
    "PI_CODING_AGENT_DIR": pi_home,
    "PI_SKIP_VERSION_CHECK": "1",
    "PI_TELEMETRY": "0",
    "XDG_CONFIG_HOME": xdg_config,
    "XDG_CACHE_HOME": xdg_cache,
    "XDG_DATA_HOME": xdg_data,
    "NPM_CONFIG_USERCONFIG": user_npmrc,
    "NPM_CONFIG_GLOBALCONFIG": global_npmrc,
    "NPM_CONFIG_CACHE": npm_cache,
    "NPM_CONFIG_OFFLINE": "true",
    "GIT_TERMINAL_PROMPT": "0",
    "HTTP_PROXY": "http://127.0.0.1:9",
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "ALL_PROXY": "http://127.0.0.1:9",
    "NO_PROXY": "",
}
for key in ("TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE"):
    if key in os.environ:
        environment[key] = os.environ[key]

try:
    installed = subprocess.run(
        ["pi", "install", root],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=workspace,
        env=environment,
        timeout=30,
        check=False,
    )
except subprocess.TimeoutExpired:
    raise SystemExit("Pi local-package install timed out")
if installed.returncode != 0:
    raise SystemExit("Pi local-package install failed")
if installed.stderr:
    raise SystemExit("Pi local-package install emitted unexpected stderr")

process = subprocess.Popen(
    ["pi", "--mode", "rpc", "--no-session"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    cwd=workspace,
    env=environment,
)
request_id = "rpc-smoke-get-commands"
payload = json.dumps({"id": request_id, "type": "get_commands"}).encode() + b"\n"
try:
    stdout, stderr = process.communicate(payload, timeout=30)
except subprocess.TimeoutExpired:
    process.kill()
    process.communicate()
    raise SystemExit("Pi RPC discovery smoke timed out")
if process.returncode != 0:
    raise SystemExit("Pi RPC discovery smoke failed")
if stderr:
    raise SystemExit("Pi RPC discovery smoke emitted unexpected stderr")

records = []
for raw_line in stdout.split(b"\n"):
    if raw_line.endswith(b"\r"):
        raw_line = raw_line[:-1]
    if not raw_line:
        continue
    try:
        records.append(json.loads(raw_line.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SystemExit("Pi RPC emitted invalid strict JSONL")

responses = [record for record in records if record.get("id") == request_id]
if len(responses) != 1:
    raise SystemExit("Pi RPC get_commands response count was not exactly one")
response = responses[0]
if not (
    response.get("type") == "response"
    and response.get("command") == "get_commands"
    and response.get("success") is True
):
    raise SystemExit("Pi RPC get_commands response was unsuccessful")

commands = response.get("data", {}).get("commands")
if not isinstance(commands, list):
    raise SystemExit("Pi RPC get_commands response omitted commands")
root_path = Path(root).resolve()
package_commands = [
    command
    for command in commands
    if command.get("sourceInfo", {}).get("origin") == "package"
    and isinstance(command.get("sourceInfo", {}).get("baseDir"), str)
    and Path(command["sourceInfo"]["baseDir"]).resolve() == root_path
]
expected = {
    "orchestrator-help": ("extension", "extensions/tmux-orchestrator.js"),
    "orchestrator-about": ("extension", "extensions/tmux-orchestrator.js"),
    "orchestrator-doctor": ("extension", "extensions/tmux-orchestrator.js"),
    "orchestrator-models": ("extension", "extensions/tmux-orchestrator.js"),
    "orchestrator-dashboard": ("extension", "extensions/tmux-orchestrator.js"),
    "orchestrator-start": ("extension", "extensions/tmux-orchestrator.js"),
    "orchestrator-list": ("extension", "extensions/tmux-orchestrator.js"),
    "orchestrator-status": ("extension", "extensions/tmux-orchestrator.js"),
    "orchestrator-watch": ("extension", "extensions/tmux-orchestrator.js"),
    "orchestrator-attach": ("extension", "extensions/tmux-orchestrator.js"),
    "orchestrator-send": ("extension", "extensions/tmux-orchestrator.js"),
    "orchestrator-stop": ("extension", "extensions/tmux-orchestrator.js"),
    "or-help": ("extension", "extensions/tmux-orchestrator.js"),
    "or-about": ("extension", "extensions/tmux-orchestrator.js"),
    "or-doctor": ("extension", "extensions/tmux-orchestrator.js"),
    "or-models": ("extension", "extensions/tmux-orchestrator.js"),
    "or-dashboard": ("extension", "extensions/tmux-orchestrator.js"),
    "or-start": ("extension", "extensions/tmux-orchestrator.js"),
    "or-list": ("extension", "extensions/tmux-orchestrator.js"),
    "or-status": ("extension", "extensions/tmux-orchestrator.js"),
    "or-watch": ("extension", "extensions/tmux-orchestrator.js"),
    "or-attach": ("extension", "extensions/tmux-orchestrator.js"),
    "or-send": ("extension", "extensions/tmux-orchestrator.js"),
    "or-stop": ("extension", "extensions/tmux-orchestrator.js"),
    "orchestrate": ("extension", "extensions/tmux-orchestrator.js"),
    "orchestrations": ("extension", "extensions/tmux-orchestrator.js"),
    "skill:tmux-agent-orchestrator": ("skill", "SKILL.md"),
}
actual = {command.get("name"): command.get("source") for command in package_commands}
expected_sources = {name: source for name, (source, _path) in expected.items()}
if len(package_commands) != len(expected) or actual != expected_sources:
    raise SystemExit("Pi RPC package command discovery did not match the exact expected surface")

for command in package_commands:
    name = command["name"]
    source_path = command.get("sourceInfo", {}).get("path")
    if not isinstance(source_path, str):
        raise SystemExit(f"Pi RPC command omitted package source path: {name}")
    actual_path = Path(source_path).resolve()
    expected_path = (root_path / expected[name][1]).resolve()
    source_info = command.get("sourceInfo", {})
    base_dir = source_info.get("baseDir")
    if (
        actual_path != expected_path
        or not actual_path.is_relative_to(root_path)
        or source_info.get("origin") != "package"
        or source_info.get("scope") != "user"
        or not isinstance(base_dir, str)
        or Path(base_dir).resolve() != root_path
    ):
        raise SystemExit(f"Pi RPC command was not discovered through the installed package: {name}")

for record in records:
    if record is response:
        continue
    if not (
        record.get("type") == "extension_ui_request"
        and record.get("method") in {"setStatus", "setWidget"}
        and (record.get("statusKey") == "tmux-orchestrator" or record.get("widgetKey") == "tmux-orchestrator")
    ):
        raise SystemExit("Pi RPC emitted an unexpected non-response record")
PY

printf '%s\n' 'Isolated Pi local-package install + RPC discovery passed (exact twenty-six commands/root skill from the npm-installed tarball path; no prompt, provider request, or real home/auth access).'
