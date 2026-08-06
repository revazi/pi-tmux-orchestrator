#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONDONTWRITEBYTECODE=1

cleanup() {
  if [[ -n "${TEMP_BIN:-}" ]]; then
    rm -rf "$TEMP_BIN"
  fi
}
trap cleanup EXIT

printf '%s\n' '==> Shell syntax'
bash -n "$ROOT/install.sh" "$ROOT/scripts/test.sh"

printf '%s\n' '==> Python syntax'
python3 - <<PY
import ast
from pathlib import Path
for path in (
    Path("$ROOT/scripts/pi-tmux-agents.py"),
    Path("$ROOT/tests/test_orchestrator.py"),
    Path("$ROOT/tests/functional_smoke.py"),
):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    print(f"OK {path.relative_to(Path('$ROOT'))}")
PY

printf '%s\n' '==> Unit tests'
python3 -m unittest discover -s "$ROOT/tests" -p 'test_*.py' -v

printf '%s\n' '==> CLI help and provider-free dry run'
"$ROOT/scripts/pi-tmux-agents.py" --help >/dev/null
TEMP_BIN=$(mktemp -d "${TMPDIR:-/tmp}/pi-tmux-orchestrator-bin.XXXXXX")
printf '#!/usr/bin/env bash\nexit 0\n' > "$TEMP_BIN/pi"
chmod 700 "$TEMP_BIN/pi"
PATH="$TEMP_BIN:$PATH" "$ROOT/scripts/pi-tmux-agents.py" start \
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
