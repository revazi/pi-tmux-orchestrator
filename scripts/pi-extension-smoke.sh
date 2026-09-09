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
    "or-models": ("extension", "extensions/tmux-orchestrator.js"),
    "or-dashboard": ("extension", "extensions/tmux-orchestrator.js"),
    "or-start": ("extension", "extensions/tmux-orchestrator.js"),
    "or-send": ("extension", "extensions/tmux-orchestrator.js"),
    "or-stop": ("extension", "extensions/tmux-orchestrator.js"),
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
# Exercise the installed shared worker bridge in actual Pi without a prompt or
# provider request. The extra tool models an extension with write capability.
observation = Path(workspace) / "custom-tools.json"
verifier = Path(workspace) / "custom-verifier.mjs"
verifier.write_text(
    'import { writeFileSync } from "node:fs";\n'
    'export default function(pi) {\n'
    '  pi.registerTool({name:"fixture_mutator",label:"Forbidden fixture",description:"Never execute",parameters:{type:"object",properties:{}},async execute(){throw new Error("must not execute");}});\n'
    '  pi.on("session_start", () => writeFileSync(process.env.SMOKE_TOOLS_FILE, JSON.stringify(pi.getActiveTools())));\n'
    '}\n',
    encoding="utf-8",
)
custom_environment = {
    **environment,
    "PI_TMUX_ORCHESTRATOR_ROLE": "custom-smoke",
    "PI_TMUX_ORCHESTRATOR_SPECIALIST_CONTRACT": "probe",
    "PI_TMUX_ORCHESTRATOR_TOKEN": "a" * 32,
    "PI_TMUX_ORCHESTRATOR_SOCKET": str(Path(workspace) / "absent-broker.sock"),
    "PI_TMUX_ORCHESTRATOR_GENERATION": "1",
    "PI_TMUX_ORCHESTRATOR_GUARDRAILS": json.dumps({"enforcement": "warn-only", "warning": {}, "hard": {}}),
    "SMOKE_TOOLS_FILE": str(observation),
}
def custom_probe(argv):
    observation.unlink(missing_ok=True)
    custom = subprocess.run(
        argv, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=workspace, env=custom_environment, timeout=30, check=False,
    )
    if custom.returncode or custom.stderr or not observation.is_file():
        raise SystemExit("Actual-Pi custom worker contract startup failed")
    if set(json.loads(observation.read_text())) != {"read", "grep", "find", "ls", "orchestrator_report"}:
        raise SystemExit("Actual-Pi custom worker exposed tools outside the fixed read-only set")
    records = [json.loads(line) for line in custom.stdout.split(b"\n") if line.strip()]
    responses = [record for record in records if record.get("id") == request_id and record.get("success") is True]
    if len(responses) != 1:
        raise SystemExit("Actual-Pi custom worker RPC discovery failed")
    return responses[0]

custom_probe(
    ["pi", "--mode", "rpc", "--no-session", "--no-extensions", "--no-skills",
     "--no-context-files", "--extension", str(root_path / "extensions/orchestrator-worker.js"),
     "--extension", str(verifier), "--tools", "read,bash,edit,write,grep,find,ls,orchestrator_report,fixture_mutator"]
)

# Use the installed Python resource-preparation path, not hand-authored resource
# flags. No broker is running: this checks bootstrap, not workflow acceptance.
import hashlib
sys.path.insert(0, str(root_path))
from pi_tmux_orchestrator.custom_role_resources import select_custom_roles
from pi_tmux_orchestrator.worker_resources import prepare_worker_resources

source = Path(home).resolve() / "custom-source"
source.mkdir(mode=0o700)
coord = Path(home).resolve() / "custom-coord"
coord.mkdir(mode=0o700)

def resource(name, content):
    path = source / name
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return {"path": str(path), "sha256": hashlib.sha256(content.encode()).hexdigest()}

definition = {
    "id": "custom-smoke", "contract": "probe",
    "prompt": resource("prompt.md", "Inspect read-only; no writer or reviewer authority.\n"),
    "skills": [resource("skill.md", "---\nname: custom-smoke-skill\ndescription: Synthetic read-only fixture.\nallowed-tools: write edit bash\n---\nOnly inspect; no executable attachments.\n")],
}
registry = source / "roles.json"
registry.write_text(json.dumps({"version": 1, "roles": [definition]}))
registry.chmod(0o600)
selection = select_custom_roles(Path(workspace).resolve(), ["custom-smoke"], str(registry))
manifest = {
    "version": 6, "project": str(Path(workspace).resolve()),
    "custom_role_registry": selection["registry_path"],
    "roles": {"custom-smoke": {"tools": "read,grep,find,ls", "custom_role": selection["roles"]["custom-smoke"]}},
}
resource_args = []
contract = prepare_worker_resources(resource_args, coord, manifest, "custom-smoke", root_path / "extensions/orchestrator-worker.js")
custom_environment["PI_TMUX_ORCHESTRATOR_SPECIALIST_CONTRACT"] = contract
# Mutate the external files after preparation. Pi must still load the reviewed
# snapshot skill, rather than reopening the mutable external resource.
Path(definition["prompt"]["path"]).write_text("UNREVIEWED")
Path(definition["skills"][0]["path"]).write_text("UNREVIEWED")
marker = Path(workspace) / "unexpected-extension-execution"
custom_environment["SMOKE_UNEXPECTED_EXTENSION_FILE"] = str(marker)
for directory in (Path(pi_home) / "extensions", Path(workspace) / ".pi" / "extensions"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "unsafe.mjs").write_text('import {writeFileSync} from "node:fs"; export default function(){writeFileSync(process.env.SMOKE_UNEXPECTED_EXTENSION_FILE,"unexpected");}\n')
response = custom_probe(
    ["pi", "--mode", "rpc", "--no-session", "--approve", "--no-context-files",
     "--tools", "read,grep,find,ls,orchestrator_report", *resource_args, "--extension", str(verifier)]
)
if marker.exists():
    raise SystemExit("Custom worker loaded an unapproved discovered extension")
if "skill:custom-smoke-skill" not in {item["name"] for item in response.get("data", {}).get("commands", [])}:
    raise SystemExit("Custom worker did not load the reviewed snapshot skill")
PY

printf '%s\n' 'Isolated Pi local-package install + RPC discovery, custom-worker read-only tool scope, and verified-resource bootstrap smoke passed (no prompt, provider request, or real home/auth access; not full custom-role orchestration acceptance).'
