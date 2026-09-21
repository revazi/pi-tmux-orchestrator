#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONDONTWRITEBYTECODE=1
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/pi-tmux-orchestrator-tests.XXXXXX")
TEST_ROOT=$(cd "$TEST_ROOT" && pwd -P)
export PI_TMUX_ORCHESTRATOR_CONFIG="$TEST_ROOT/missing-model-config.json"
export PI_TMUX_ORCHESTRATOR_BUDGET_CONFIG="$TEST_ROOT/missing-budget-config.json"

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
  "$ROOT/scripts/ensure-tmux.sh" \
  "$ROOT/scripts/package-smoke.sh" \
  "$ROOT/scripts/test-coverage.sh" \
  "$ROOT/scripts/pi-extension-smoke.sh" \
  "$ROOT/scripts/stage-prerelease.sh" \
  "$ROOT/scripts/run-prerelease-isolated.sh"

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
root = Path("$ROOT")
paths = [
    root / "bin" / "pi-tmux-agents",
    *sorted((root / "pi_tmux_orchestrator").glob("*.py")),
    *sorted((root / "scripts").glob("*.py")),
    *sorted((root / "tests").glob("*.py")),
]
for path in paths:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    print(f"OK {path.relative_to(root)}")
PY

printf '%s\n' '==> Unit tests'
python3 -m unittest discover -s "$ROOT/tests" -p 'test_*.py' -v

printf '%s\n' '==> Extension syntax and unit tests'
shopt -s nullglob
for file in "$ROOT/extensions"/*.js "$ROOT/scripts"/*.mjs "$ROOT/tests"/*.mjs "$ROOT/tests/fixtures"/*.mjs; do
  node --check "$file"
done
shopt -u nullglob
node --test "$ROOT/tests/extension.test.mjs"
"$ROOT/scripts/test-coverage.sh" "$TEST_ROOT/coverage/coverage-final.json"
node "$ROOT/scripts/token-efficiency-baseline.mjs" --check
node "$ROOT/scripts/result-volume-baseline.mjs" --check
node "$ROOT/scripts/execution-profile-baseline.mjs" --check
node "$ROOT/scripts/phased-implementation-baseline.mjs" --check
node "$ROOT/scripts/worker-prompt-baseline.mjs" --check
python3 "$ROOT/scripts/specialist-activation-baseline.py" --check
python3 "$ROOT/scripts/workspace-capsule-baseline.py" --check

printf '%s\n' '==> Package verification, npm/Pi local-package install + RPC discovery, and offline publication dry run'
node "$ROOT/scripts/verify-package.mjs"
"$ROOT/scripts/package-smoke.sh"
PRERELEASE_STAGE="$TEST_ROOT/prerelease-stage"
"$ROOT/scripts/stage-prerelease.sh" --output "$PRERELEASE_STAGE" --allow-dirty
NO_PI_BIN="$TEST_ROOT/no-pi-bin"
mkdir -m 700 "$NO_PI_BIN"
ln -s "$(python3 -c 'import sys; print(sys.executable)')" "$NO_PI_BIN/python3"
PATH="$NO_PI_BIN" /bin/bash "$ROOT/scripts/run-prerelease-isolated.sh" \
  --stage "$PRERELEASE_STAGE" --check
python3 - "$PRERELEASE_STAGE/provenance.json" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
if value.get("kind") != "pi-tmux-orchestrator-local-prerelease":
    raise SystemExit("pre-release stage omitted exact provenance")
if value.get("published") is not False:
    raise SystemExit("pre-release stage claimed publication")
package_root = path.parent / value["package_root"]
if not (package_root / "extensions" / "tmux-orchestrator.js").is_file():
    raise SystemExit("pre-release stage omitted the installed extension")
PY
printf '%s\n' '==> Provider-free staged-package actual-Pi fixed-plan custom lifecycle smoke'
python3 "$ROOT/tests/actual_pi_custom_lifecycle.py" \
  "$PRERELEASE_STAGE/package-host/node_modules/pi-tmux-orchestrator"
printf '\nlocal tamper probe\n' >> \
  "$PRERELEASE_STAGE/package-host/node_modules/pi-tmux-orchestrator/README.md"
if "$ROOT/scripts/run-prerelease-isolated.sh" \
  --stage "$PRERELEASE_STAGE" --check >/dev/null 2>&1; then
  printf '%s\n' 'tampered pre-release package unexpectedly passed validation' >&2
  exit 1
fi
rm -rf "$PRERELEASE_STAGE"

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
  --profile economy \
  --implementation-flow phased \
  --skip-model-check \
  --dry-run
if tmux has-session -t =pi-repository-dry-run 2>/dev/null; then
  printf '%s\n' 'Dry run leaked a tmux session.' >&2
  exit 1
fi

printf '%s\n' '==> Model-free tmux functional smoke'
python3 "$ROOT/tests/functional_smoke.py"

printf '%s\n' '==> Model-free custom worker tmux lifecycle smoke'
python3 "$ROOT/tests/custom_worker_tmux_smoke.py"

printf '%s\n' 'All checks passed.'
