#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' 'Usage: scripts/run-prerelease-isolated.sh --stage ABSOLUTE_STAGE (--project ABSOLUTE_PROJECT | --check)' >&2
  exit 2
}

STAGE=
PROJECT=
CHECK=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stage)
      [[ $# -ge 2 && -z "$STAGE" ]] || usage
      STAGE=$2
      shift 2
      ;;
    --project)
      [[ $# -ge 2 && -z "$PROJECT" && "$CHECK" == false ]] || usage
      PROJECT=$2
      shift 2
      ;;
    --check)
      [[ -z "$PROJECT" && "$CHECK" == false ]] || usage
      CHECK=true
      shift
      ;;
    *) usage ;;
  esac
done
[[ -n "$STAGE" && "$STAGE" == /* ]] || usage
if [[ "$CHECK" == true ]]; then
  [[ -z "$PROJECT" ]] || usage
else
  [[ -n "$PROJECT" && "$PROJECT" == /* && -d "$PROJECT" ]] || usage
fi
[[ -d "$STAGE" && -f "$STAGE/provenance.json" ]] || usage
CANONICAL_STAGE=$(cd "$STAGE" && pwd -P)
CANONICAL_PROJECT=$PROJECT
if [[ "$CHECK" != true ]]; then
  CANONICAL_PROJECT=$(cd "$PROJECT" && pwd -P)
fi
if [[ "$CANONICAL_STAGE" != "$STAGE" || ( "$CHECK" != true && "$CANONICAL_PROJECT" != "$PROJECT" ) ]]; then
  printf '%s\n' 'Stage and project paths must be canonical and contain no symlink components.' >&2
  exit 2
fi
if ! command -v pi >/dev/null 2>&1; then
  printf '%s\n' 'pi is required to run the staged pre-release extension.' >&2
  exit 1
fi

VALIDATED=$(python3 - "$STAGE" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

stage = Path(sys.argv[1])
provenance_path = stage / "provenance.json"
value = json.loads(provenance_path.read_text(encoding="utf-8"))
expected = {
    "schema_version",
    "kind",
    "package_name",
    "package_version",
    "git_commit",
    "git_tree",
    "source_state",
    "git_status_sha256",
    "tarball",
    "tarball_sha256",
    "package_root",
    "package_tree_sha256",
    "published",
}
if set(value) != expected or value["schema_version"] != 1:
    raise SystemExit("pre-release provenance shape is invalid")
if value["kind"] != "pi-tmux-orchestrator-local-prerelease":
    raise SystemExit("pre-release provenance kind is invalid")
if value["package_name"] != "pi-tmux-orchestrator" or value["published"] is not False:
    raise SystemExit("pre-release provenance package identity is invalid")
if value["source_state"] not in {"clean", "dirty"}:
    raise SystemExit("pre-release provenance source state is invalid")
for field in (
    "git_commit",
    "git_tree",
    "git_status_sha256",
    "tarball_sha256",
    "package_tree_sha256",
):
    text = value[field]
    valid_lengths = {64} if field.endswith("sha256") else {40, 64}
    if not isinstance(text, str) or len(text) not in valid_lengths or any(c not in "0123456789abcdef" for c in text):
        raise SystemExit(f"pre-release provenance {field} is invalid")

def within(relative: object) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise SystemExit("pre-release provenance path is invalid")
    requested = stage / relative
    candidate = requested.resolve(strict=True)
    if candidate != requested:
        raise SystemExit("pre-release provenance path contains a symlink component")
    try:
        candidate.relative_to(stage)
    except ValueError:
        raise SystemExit("pre-release provenance path escapes the stage")
    return candidate

tarball = within(value["tarball"])
package_root = within(value["package_root"])
if hashlib.sha256(tarball.read_bytes()).hexdigest() != value["tarball_sha256"]:
    raise SystemExit("pre-release tarball digest changed")
package_tree = hashlib.sha256()
for path in sorted(package_root.rglob("*")):
    if path.is_symlink():
        raise SystemExit("staged package tree contains a symlink")
    if path.is_dir():
        continue
    if not path.is_file():
        raise SystemExit("staged package tree contains a non-regular entry")
    relative = path.relative_to(package_root).as_posix().encode("utf-8")
    content = path.read_bytes()
    package_tree.update(len(relative).to_bytes(4, "big"))
    package_tree.update(relative)
    package_tree.update((path.stat().st_mode & 0o777).to_bytes(2, "big"))
    package_tree.update(len(content).to_bytes(8, "big"))
    package_tree.update(content)
if package_tree.hexdigest() != value["package_tree_sha256"]:
    raise SystemExit("staged package tree digest changed")
manifest = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
if manifest.get("name") != value["package_name"] or manifest.get("version") != value["package_version"]:
    raise SystemExit("staged package manifest does not match provenance")
print(f"{value['package_root']}\t{value['git_commit']}")
PY
)
IFS=$'\t' read -r PACKAGE_REL COMMIT <<< "$VALIDATED"
PACKAGE_ROOT="$STAGE/$PACKAGE_REL"
if [[ "$CHECK" == true ]]; then
  printf 'Validated staged pre-release commit %s at %s\n' "$COMMIT" "$PACKAGE_ROOT"
  exit 0
fi

TEMP=$(mktemp -d "${TMPDIR:-/tmp}/pi-tmux-prerelease-tui.XXXXXX")
cleanup() {
  rm -rf "$TEMP"
}
trap cleanup EXIT
chmod 700 "$TEMP"
mkdir -p \
  "$TEMP/pi-home" "$TEMP/home" "$TEMP/xdg-config" "$TEMP/xdg-cache" \
  "$TEMP/xdg-data" "$TEMP/npm-cache" "$TEMP/npm-tmp"
: > "$TEMP/user-npmrc"
: > "$TEMP/global-npmrc"

printf 'Launching staged commit %s from %s\n' "$COMMIT" "$PACKAGE_ROOT"
printf '%s\n' \
  'This Pi home is disposable and has no real authentication. Use /or-about, /or-help, /or-doctor, /or-models, command completion, and preview/cancel /or-start. Do not send a model prompt. Offline/update-disabled and blackhole proxy settings are not an OS network sandbox.'
(
  cd "$PROJECT"
  env -i \
    PATH="$PATH" \
    HOME="$TEMP/home" \
    PI_CODING_AGENT_DIR="$TEMP/pi-home" \
    PI_SKIP_VERSION_CHECK=1 \
    PI_TELEMETRY=0 \
    PI_TMUX_ORCHESTRATOR_DISABLE_UPDATE_NOTICE=1 \
    XDG_CONFIG_HOME="$TEMP/xdg-config" \
    XDG_CACHE_HOME="$TEMP/xdg-cache" \
    XDG_DATA_HOME="$TEMP/xdg-data" \
    NPM_CONFIG_USERCONFIG="$TEMP/user-npmrc" \
    NPM_CONFIG_GLOBALCONFIG="$TEMP/global-npmrc" \
    NPM_CONFIG_CACHE="$TEMP/npm-cache" \
    NPM_CONFIG_OFFLINE=true \
    TMPDIR="$TEMP/npm-tmp" \
    GIT_TERMINAL_PROMPT=0 \
    HTTP_PROXY=http://127.0.0.1:9 \
    HTTPS_PROXY=http://127.0.0.1:9 \
    ALL_PROXY=http://127.0.0.1:9 \
    NO_PROXY= \
    pi --no-extensions --no-skills -e "$PACKAGE_ROOT" --no-session
)
