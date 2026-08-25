#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
MODE=smoke
PROJECT=$ROOT
REGISTRY=
if [[ $# -gt 0 ]]; then
  if [[ "$1" != "--interactive" || ( $# -ne 2 && $# -ne 4 ) ]]; then
    printf 'Usage: %s [--interactive ABSOLUTE_PROJECT [--registry ABSOLUTE_JSON]]\n' "$0" >&2
    exit 2
  fi
  MODE=interactive
  PROJECT=$2
  if [[ "$PROJECT" != /* || ! -d "$PROJECT" ]]; then
    printf '%s\n' 'Interactive project must be an existing absolute directory.' >&2
    exit 2
  fi
  PROJECT=$(cd "$PROJECT" && pwd -P)
  if [[ $# -eq 4 ]]; then
    if [[ "$3" != "--registry" || "$4" != /* || ! -f "$4" ]]; then
      printf '%s\n' 'Interactive registry must be an existing absolute JSON file.' >&2
      exit 2
    fi
    REGISTRY=$4
  fi
fi

TEMP=$(mktemp -d "${TMPDIR:-/tmp}/pi-tmux-unreleased-smoke.XXXXXX")
cleanup() {
  rm -rf "$TEMP"
}
trap cleanup EXIT
chmod 700 "$TEMP"
mkdir -p "$TEMP/npm-home" "$TEMP/npm-cache" "$TEMP/npm-tmp" "$TEMP/install"
: > "$TEMP/user-npmrc"
: > "$TEMP/global-npmrc"
printf '%s\n' '{"name":"unreleased-smoke-host","version":"1.0.0","private":true}' \
  > "$TEMP/install/package.json"

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

node "$ROOT/scripts/verify-package.mjs"
isolated_npm pack "$ROOT" --pack-destination "$TEMP" --ignore-scripts --silent >/dev/null
TARBALL=$(find "$TEMP" -maxdepth 1 -type f -name '*.tgz' -print -quit)
test -n "$TARBALL"
(
  cd "$TEMP/install"
  isolated_npm install --offline --ignore-scripts --no-audit --no-fund \
    "$TARBALL" --silent
)
PACKAGE="$TEMP/install/node_modules/pi-tmux-orchestrator"
test -f "$PACKAGE/extensions/tmux-orchestrator.js"
test -f "$PACKAGE/pi_tmux_orchestrator/custom_roles.py"
"$ROOT/scripts/pi-extension-smoke.sh" "$PACKAGE"

if [[ "$MODE" == "smoke" ]]; then
  printf '%s\n' \
    'Provider-free unreleased checkout smoke passed from an actual isolated tarball install; no publish, real Pi/npm home, auth, prompt, tmux grid, or provider request was used.'
  printf 'Manual isolated TUI discovery (offline/update-disabled with blackhole proxy settings; exit removes its temporary home): %q --interactive %q\n' \
    "$ROOT/scripts/unreleased-extension-smoke.sh" "$ROOT"
  exit 0
fi

if ! command -v pi >/dev/null 2>&1; then
  printf '%s\n' 'pi is required for the isolated interactive discovery mode.' >&2
  exit 1
fi
mkdir -p \
  "$TEMP/pi-home" "$TEMP/home" "$TEMP/xdg-config" "$TEMP/xdg-cache" \
  "$TEMP/xdg-data" "$TEMP/interactive-npm-cache"

isolated_pi() {
  local registry_environment=()
  if [[ -n "$REGISTRY" ]]; then
    registry_environment+=("PI_TMUX_ORCHESTRATOR_ROLE_REGISTRY=$REGISTRY")
  fi
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
    NPM_CONFIG_CACHE="$TEMP/interactive-npm-cache" \
    NPM_CONFIG_OFFLINE=true \
    GIT_TERMINAL_PROMPT=0 \
    HTTP_PROXY=http://127.0.0.1:9 \
    HTTPS_PROXY=http://127.0.0.1:9 \
    ALL_PROXY=http://127.0.0.1:9 \
    NO_PROXY= \
    "${registry_environment[@]}" \
    "$@"
}

(
  cd "$PROJECT"
  isolated_pi pi install "$PACKAGE"
  printf '%s\n' \
    'Launching an isolated Pi TUI from the verified local tarball artifact with offline/update-disabled and blackhole proxy settings. Use /or-help, /or-doctor, or command completion; do not send a model prompt or treat proxy settings as an OS network sandbox. An explicitly supplied --registry is validated by /or-doctor but remains registry-only/not launchable. Exit to delete this temporary home.'
  isolated_pi pi --no-session
)
