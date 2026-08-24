#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEMP=$(mktemp -d "${TMPDIR:-/tmp}/pi-tmux-package-smoke.XXXXXX")
cleanup() {
  rm -rf "$TEMP"
}
trap cleanup EXIT
chmod 700 "$TEMP"

EXPECTED_FILES=(
  CHANGELOG.md
  LICENSE.md
  README.md
  SECURITY.md
  SKILL.md
  VERSION
  bin/pi-tmux-agents
  extensions/tmux-orchestrator.js
  extensions/orchestrator-context.js
  extensions/orchestrator-models.js
  extensions/orchestrator-update.js
  extensions/orchestrator-parent.js
  extensions/orchestrator-worker.js
  package.json
  pi_tmux_orchestrator/__init__.py
  pi_tmux_orchestrator/__main__.py
  pi_tmux_orchestrator/broker.py
  pi_tmux_orchestrator/broker_client.py
  pi_tmux_orchestrator/broker_store.py
  pi_tmux_orchestrator/cli.py
  pi_tmux_orchestrator/commands.py
  pi_tmux_orchestrator/configuration.py
  pi_tmux_orchestrator/constants.py
  pi_tmux_orchestrator/context_capsules.py
  pi_tmux_orchestrator/controller.py
  pi_tmux_orchestrator/dashboard.py
  pi_tmux_orchestrator/models.py
  pi_tmux_orchestrator/output.py
  pi_tmux_orchestrator/prompts.py
  pi_tmux_orchestrator/protocol.py
  pi_tmux_orchestrator/relay.py
  pi_tmux_orchestrator/rpc.py
  pi_tmux_orchestrator/rpc_protocol.py
  pi_tmux_orchestrator/rpc_store.py
  pi_tmux_orchestrator/rpc_supervisor.py
  pi_tmux_orchestrator/runtime.py
  pi_tmux_orchestrator/storage.py
  pi_tmux_orchestrator/supervisor_api.py
  pi_tmux_orchestrator/supervisor_commands.py
  pi_tmux_orchestrator/tmux.py
  pi_tmux_orchestrator/token_efficiency.py
  references/usage.md
  references/protocol-v1.md
  references/dashboard-design.md
)

mkdir -p "$TEMP/npm-home" "$TEMP/npm-cache" "$TEMP/npm-tmp"
: > "$TEMP/user-npmrc"
: > "$TEMP/global-npmrc"

isolated_npm() {
  env -i \
    PATH="$PATH" \
    HOME="$TEMP/npm-home" \
    TMPDIR="$TEMP/npm-tmp" \
    LANG="${LANG:-C}" \
    npm_config_userconfig="$TEMP/user-npmrc" \
    npm_config_globalconfig="$TEMP/global-npmrc" \
    npm_config_cache="$TEMP/npm-cache" \
    npm_config_update_notifier=false \
    npm_config_audit=false \
    npm_config_fund=false \
    npm_config_offline=true \
    npm "$@"
}

isolated_npm pack "$ROOT" --pack-destination "$TEMP" --ignore-scripts --silent >/dev/null
TARBALL=$(find "$TEMP" -maxdepth 1 -type f -name '*.tgz' -print -quit)
test -n "$TARBALL"

python3 - "$TARBALL" "${EXPECTED_FILES[@]}" <<'PY'
import json
import sys
import tarfile

tarball, *expected = sys.argv[1:]
expected_names = sorted(f"package/{path}" for path in expected)
with tarfile.open(tarball, "r:gz") as archive:
    members = archive.getmembers()
    manifest = json.load(archive.extractfile("package/package.json"))
    license_text = archive.extractfile("package/LICENSE.md").read().decode("utf-8")
actual_names = sorted(member.name for member in members)
if actual_names != expected_names:
    raise SystemExit(f"actual tarball allowlist mismatch: {actual_names!r}")
if any(not member.isfile() for member in members):
    raise SystemExit("actual tarball contains a non-regular entry")
expected_author = {
    "name": "Revaz Zakalashvili",
    "email": "revaz.zakalashvili@gmail.com",
    "url": "https://github.com/revazi",
}
if manifest.get("license") != "MIT" or manifest.get("author") != expected_author:
    raise SystemExit("actual tarball omitted exact MIT/author metadata")
if not license_text.startswith("MIT License\n\nCopyright (c) 2026 Revaz Zakalashvili\n"):
    raise SystemExit("actual tarball omitted the canonical MIT license")
PY

mkdir "$TEMP/install"
printf '%s\n' '{"name":"package-smoke-host","version":"1.0.0","private":true}' > "$TEMP/install/package.json"
(
  cd "$TEMP/install"
  isolated_npm install --offline --ignore-scripts --no-audit --no-fund "$TARBALL" --silent
  isolated_npm ls --all --json > "$TEMP/dependency-tree.json"
)

PACKAGE="$TEMP/install/node_modules/pi-tmux-orchestrator"
VERSION=$($TEMP/install/node_modules/.bin/pi-tmux-agents --version)
test "$VERSION" = "pi-tmux-agents 0.8.1"
test ! -e "$TEMP/install/node_modules/@earendil-works"
test -f "$PACKAGE/LICENSE.md"
test -z "$(find "$PACKAGE" -type d -name node_modules -print -quit)"
test -z "$(find "$PACKAGE" -type f \( -name '*.tgz' -o -name 'package-lock.json' \) -print -quit)"

python3 - "$TEMP/dependency-tree.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    tree = json.load(handle)
if tree.get("problems"):
    raise SystemExit("npm dependency tree reported problems")
dependencies = tree.get("dependencies", {})
if set(dependencies) != {"pi-tmux-orchestrator"}:
    raise SystemExit("installed root dependency surface was not exact")
package = dependencies["pi-tmux-orchestrator"]
if package.get("version") != "0.8.1" or package.get("dependencies"):
    raise SystemExit("installed package owns a dependency tree")
PY

"$ROOT/scripts/pi-extension-smoke.sh" "$PACKAGE"

mkdir -p "$TEMP/publish-home" "$TEMP/publish-cache" "$TEMP/publish-tmp"
env -i \
  PATH="$PATH" \
  HOME="$TEMP/publish-home" \
  TMPDIR="$TEMP/publish-tmp" \
  LANG="${LANG:-C}" \
  npm_config_userconfig="$TEMP/user-npmrc" \
  npm_config_globalconfig="$TEMP/global-npmrc" \
  npm_config_cache="$TEMP/publish-cache" \
  npm_config_registry="http://127.0.0.1:9" \
  npm_config_update_notifier=false \
  npm publish "$TARBALL" \
    --dry-run --offline --ignore-scripts --access public --json \
    > "$TEMP/publish-dry-run.json"

python3 - "$TEMP/publish-dry-run.json" "${EXPECTED_FILES[@]}" <<'PY'
import json
import sys

report_path, *expected = sys.argv[1:]
with open(report_path, encoding="utf-8") as handle:
    raw_report = json.load(handle)
package_name = "pi-tmux-orchestrator"
# npm 11.16 wraps the single-package report under its package name.
if isinstance(raw_report, dict) and set(raw_report) == {package_name}:
    report = raw_report[package_name]
else:
    report = raw_report
if not isinstance(report, dict):
    raise SystemExit("publication dry-run report shape mismatch")
if report.get("name") != package_name or report.get("version") != "0.8.1":
    raise SystemExit("publication dry-run name/version mismatch")
if report.get("bundled") != []:
    raise SystemExit("publication dry-run reported bundled dependencies")
actual = sorted(item.get("path") for item in report.get("files", []))
if actual != sorted(expected):
    raise SystemExit("publication dry-run file allowlist mismatch")
PY

printf '%s\n' 'Actual deterministic modular tarball with broker/bridge, MIT/author metadata, npm install, isolated Pi local-package install/RPC discovery, and offline npm publication dry-run passed (no owned dependency tree, real Pi/npm home, auth, or provider request).'
