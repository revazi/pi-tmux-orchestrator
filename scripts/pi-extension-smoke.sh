#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if ! command -v pi >/dev/null 2>&1; then
  printf '%s\n' 'SKIP disposable Pi RPC discovery smoke (pi not available).'
  exit 0
fi
TEMP=$(mktemp -d "${TMPDIR:-/tmp}/pi-tmux-extension-smoke.XXXXXX")
cleanup() {
  rm -rf "$TEMP"
}
trap cleanup EXIT
chmod 700 "$TEMP"

python3 - "$ROOT" "$TEMP/pi-home" <<'PY'
import json
import os
import subprocess
import sys

root, pi_home = sys.argv[1:]
environment = os.environ.copy()
environment.update(
    {
        "PI_CODING_AGENT_DIR": pi_home,
        "PI_SKIP_VERSION_CHECK": "1",
        "PI_TELEMETRY": "0",
    }
)
process = subprocess.Popen(
    ["pi", "--mode", "rpc", "--no-session", "--extension", root],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
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
expected = {
    "orchestrate": "extension",
    "orchestrations": "extension",
    "orchestrator-stop": "extension",
    "skill:tmux-agent-orchestrator": "skill",
}
actual = {command.get("name"): command.get("source") for command in commands}
if len(commands) != len(expected) or actual != expected:
    raise SystemExit("Pi RPC package command discovery did not match the exact expected surface")

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

printf '%s\n' 'Disposable Pi RPC discovery smoke passed (exact commands/skill; no prompt or provider request).'
