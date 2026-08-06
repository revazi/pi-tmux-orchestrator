#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEMP=$(mktemp -d "${TMPDIR:-/tmp}/pi-tmux-package-smoke.XXXXXX")
cleanup() {
  rm -rf "$TEMP"
}
trap cleanup EXIT

npm pack "$ROOT" --pack-destination "$TEMP" --ignore-scripts --silent >/dev/null
TARBALL=$(find "$TEMP" -maxdepth 1 -type f -name '*.tgz' -print -quit)
mkdir "$TEMP/install"
cd "$TEMP/install"
npm init --yes --silent >/dev/null
npm install --ignore-scripts --no-audit --no-fund "$TARBALL" --silent
PACKAGE="$TEMP/install/node_modules/@revazi/pi-tmux-orchestrator"
VERSION=$($TEMP/install/node_modules/.bin/pi-tmux-agents --version)
test "$VERSION" = "pi-tmux-agents 0.4.0-dev.0"
test ! -e "$TEMP/install/node_modules/@earendil-works"
test -z "$(find "$PACKAGE" -type d -name node_modules -print -quit)"
test -z "$(find "$PACKAGE" -type f \( -name '*.tgz' -o -name 'package-lock.json' \) -print -quit)"
printf '%s\n' 'Disposable package install smoke passed (no owned Pi/node_modules tree; no auth access).'
