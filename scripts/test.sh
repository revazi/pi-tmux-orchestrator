#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONDONTWRITEBYTECODE=1
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/pi-tmux-orchestrator-tests.XXXXXX")
export PI_TMUX_ORCHESTRATOR_CONFIG="$TEST_ROOT/missing-model-config.json"

cleanup() {
  if [[ -n "${TEMP_BIN:-}" ]]; then
    rm -rf "$TEMP_BIN"
  fi
  rm -rf "$TEST_ROOT"
}
trap cleanup EXIT

printf '%s\n' '==> Shell syntax'
bash -n \
  "$ROOT/install.sh" \
  "$ROOT/scripts/test.sh" \
  "$ROOT/scripts/package-smoke.sh" \
  "$ROOT/scripts/pi-extension-smoke.sh"

printf '%s\n' '==> Ruff lint and format'
ruff check \
  "$ROOT/pi_tmux_orchestrator" \
  "$ROOT/tests" \
  "$ROOT/bin/pi-tmux-agents"
ruff format --check \
  "$ROOT/pi_tmux_orchestrator" \
  "$ROOT/tests" \
  "$ROOT/bin/pi-tmux-agents"

printf '%s\n' '==> Python syntax'
python3 - <<PY
import ast
from pathlib import Path
for path in (
    Path("$ROOT/bin/pi-tmux-agents"),
    *sorted(Path("$ROOT/pi_tmux_orchestrator").glob("*.py")),
    Path("$ROOT/tests/support.py"),
    Path("$ROOT/tests/test_orchestrator.py"),
    Path("$ROOT/tests/test_hardening.py"),
    Path("$ROOT/tests/functional_smoke.py"),
    Path("$ROOT/tests/test_json_cli.py"),
    Path("$ROOT/tests/test_supervisor_api.py"),
    Path("$ROOT/tests/test_broker.py"),
    Path("$ROOT/tests/test_rpc_rendering.py"),
):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    print(f"OK {path.relative_to(Path('$ROOT'))}")
PY

printf '%s\n' '==> Unit tests'
python3 -m unittest discover -s "$ROOT/tests" -p 'test_*.py' -v

printf '%s\n' '==> Extension syntax and unit tests'
node --check "$ROOT/extensions/tmux-orchestrator.js"
node --check "$ROOT/extensions/orchestrator-models.js"
node --check "$ROOT/extensions/orchestrator-parent.js"
node --check "$ROOT/extensions/orchestrator-worker.js"
node --test "$ROOT/tests/extension.test.mjs"

printf '%s\n' '==> Package verification, npm/Pi local-package install + RPC discovery, and offline publication dry run'
node "$ROOT/scripts/verify-package.mjs"
"$ROOT/scripts/package-smoke.sh"

printf '%s\n' '==> CLI help and provider-free dry run'
"$ROOT/bin/pi-tmux-agents" --help >/dev/null
TEMP_BIN=$(mktemp -d "${TMPDIR:-/tmp}/pi-tmux-orchestrator-bin.XXXXXX")
printf '#!/usr/bin/env bash\nexit 0\n' > "$TEMP_BIN/pi"
chmod 700 "$TEMP_BIN/pi"
PATH="$TEMP_BIN:$PATH" "$ROOT/bin/pi-tmux-agents" start \
  --project "$ROOT" \
  --task 'Synthetic dry-run only.' \
  --session pi-repository-dry-run \
  --with-probe \
  --with-playwright \
  --with-django-expert \
  --skip-model-check \
  --dry-run
if tmux has-session -t =pi-repository-dry-run 2>/dev/null; then
  printf '%s\n' 'Dry run leaked a tmux session.' >&2
  exit 1
fi

printf '%s\n' '==> Model-free tmux functional smoke'
python3 "$ROOT/tests/functional_smoke.py"

printf '%s\n' 'All checks passed.'
