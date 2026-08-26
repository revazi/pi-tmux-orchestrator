#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
OUTPUT=
ALLOW_DIRTY=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)
      if [[ $# -lt 2 || -n "$OUTPUT" ]]; then
        printf '%s\n' 'Usage: scripts/stage-prerelease.sh --output ABSOLUTE_NEW_DIRECTORY [--allow-dirty]' >&2
        exit 2
      fi
      OUTPUT=$2
      shift 2
      ;;
    --allow-dirty)
      ALLOW_DIRTY=true
      shift
      ;;
    *)
      printf '%s\n' 'Usage: scripts/stage-prerelease.sh --output ABSOLUTE_NEW_DIRECTORY [--allow-dirty]' >&2
      exit 2
      ;;
  esac
done

if [[ -z "$OUTPUT" || "$OUTPUT" != /* || -e "$OUTPUT" ]]; then
  printf '%s\n' 'Pre-release output must be an absolute path that does not exist.' >&2
  exit 2
fi
OUTPUT_PARENT=$(dirname "$OUTPUT")
OUTPUT_NAME=$(basename "$OUTPUT")
if [[ ! -d "$OUTPUT_PARENT" || "$OUTPUT_NAME" == "." || "$OUTPUT_NAME" == ".." ]]; then
  printf '%s\n' 'Pre-release output parent must be an existing directory.' >&2
  exit 2
fi
CANONICAL_PARENT=$(cd "$OUTPUT_PARENT" && pwd -P)
if [[ "$CANONICAL_PARENT/$OUTPUT_NAME" != "$OUTPUT" ]]; then
  printf '%s\n' 'Pre-release output must be canonical and contain no symlink components.' >&2
  exit 2
fi
case "$OUTPUT/" in
  "$ROOT/"*)
    printf '%s\n' 'Pre-release output must remain outside the source checkout.' >&2
    exit 2
    ;;
esac

GIT_STATUS=$(git -C "$ROOT" status --porcelain=v1 --untracked-files=all)
SOURCE_STATE=clean
if [[ -n "$GIT_STATUS" ]]; then
  SOURCE_STATE=dirty
  if [[ "$ALLOW_DIRTY" != true ]]; then
    printf '%s\n' 'Source checkout is dirty; commit/review it first or use --allow-dirty only for local smoke.' >&2
    exit 1
  fi
fi
COMMIT=$(git -C "$ROOT" rev-parse --verify HEAD)
TREE=$(git -C "$ROOT" rev-parse --verify 'HEAD^{tree}')
STATUS_SHA=$(printf '%s' "$GIT_STATUS" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')

mkdir -m 700 "$OUTPUT"
SUCCESS=false
cleanup() {
  rm -rf "$OUTPUT/.build"
  if [[ "$SUCCESS" != true ]]; then
    rm -rf "$OUTPUT"
  fi
}
trap cleanup EXIT
mkdir -p \
  "$OUTPUT/.build/npm-home" "$OUTPUT/.build/npm-cache" \
  "$OUTPUT/.build/npm-tmp" "$OUTPUT/artifact" "$OUTPUT/package-host"
chmod 700 "$OUTPUT/.build" "$OUTPUT/artifact" "$OUTPUT/package-host"
: > "$OUTPUT/.build/user-npmrc"
: > "$OUTPUT/.build/global-npmrc"
printf '%s\n' '{"name":"pi-tmux-prerelease-host","version":"1.0.0","private":true}' \
  > "$OUTPUT/package-host/package.json"

isolated_npm() {
  env -i \
    PATH="$PATH" \
    HOME="$OUTPUT/.build/npm-home" \
    TMPDIR="$OUTPUT/.build/npm-tmp" \
    LANG="${LANG:-C}" \
    npm_config_userconfig="$OUTPUT/.build/user-npmrc" \
    npm_config_globalconfig="$OUTPUT/.build/global-npmrc" \
    npm_config_cache="$OUTPUT/.build/npm-cache" \
    npm_config_update_notifier=false \
    npm_config_audit=false \
    npm_config_fund=false \
    npm_config_offline=true \
    npm "$@"
}

node "$ROOT/scripts/verify-package.mjs"
isolated_npm pack "$ROOT" --pack-destination "$OUTPUT/artifact" \
  --ignore-scripts --silent >/dev/null
TARBALL=$(find "$OUTPUT/artifact" -maxdepth 1 -type f -name '*.tgz' -print -quit)
if [[ -z "$TARBALL" ]]; then
  printf '%s\n' 'Pre-release tarball was not created.' >&2
  exit 1
fi
(
  cd "$OUTPUT/package-host"
  isolated_npm install --offline --ignore-scripts --no-audit --no-fund \
    "$TARBALL" --silent
)
PACKAGE_ROOT="$OUTPUT/package-host/node_modules/pi-tmux-orchestrator"
if [[ ! -d "$PACKAGE_ROOT" ]]; then
  printf '%s\n' 'Pre-release package root was not installed.' >&2
  exit 1
fi
"$ROOT/scripts/pi-extension-smoke.sh" "$PACKAGE_ROOT"
FINAL_COMMIT=$(git -C "$ROOT" rev-parse --verify HEAD)
FINAL_STATUS=$(git -C "$ROOT" status --porcelain=v1 --untracked-files=all)
if [[ "$FINAL_COMMIT" != "$COMMIT" || "$FINAL_STATUS" != "$GIT_STATUS" ]]; then
  printf '%s\n' 'Source checkout changed while staging; discard and retry.' >&2
  exit 1
fi

python3 - \
  "$ROOT/package.json" "$OUTPUT/provenance.json" "$TARBALL" "$PACKAGE_ROOT" \
  "$COMMIT" "$TREE" "$SOURCE_STATE" "$STATUS_SHA" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

(
    manifest_path,
    output_path,
    tarball_path,
    package_root_path,
    commit,
    tree,
    source_state,
    status_sha,
) = sys.argv[1:]
manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
tarball = Path(tarball_path)
package_root = Path(package_root_path)
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
provenance = {
    "schema_version": 1,
    "kind": "pi-tmux-orchestrator-local-prerelease",
    "package_name": manifest["name"],
    "package_version": manifest["version"],
    "git_commit": commit,
    "git_tree": tree,
    "source_state": source_state,
    "git_status_sha256": status_sha,
    "tarball": f"artifact/{tarball.name}",
    "tarball_sha256": hashlib.sha256(tarball.read_bytes()).hexdigest(),
    "package_root": "package-host/node_modules/pi-tmux-orchestrator",
    "package_tree_sha256": package_tree.hexdigest(),
    "published": False,
}
Path(output_path).write_text(
    json.dumps(provenance, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
chmod 600 "$OUTPUT/provenance.json"
SUCCESS=true
cleanup
trap - EXIT

printf 'Staged pre-release package: %s\n' "$PACKAGE_ROOT"
printf 'Provenance: %s\n' "$OUTPUT/provenance.json"
printf 'Git commit: %s (source_state=%s)\n' "$COMMIT" "$SOURCE_STATE"
printf 'Provider-free isolated TUI: %q --stage %q --project %q\n' \
  "$ROOT/scripts/run-prerelease-isolated.sh" "$OUTPUT" "$ROOT"
