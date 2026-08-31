# Pre-release extension testing

This guide tests the exact local `pi-tmux-orchestrator` checkout before any
version bump, tag, release, or publication. Pi packages execute with full system
access. Review the branch, commit, package allowlist, extension, skill, and Python
CLI before running it.

## Acceptance layers

Keep these results distinct:

1. **Package provenance** proves which Git checkout produced the tarball and
   staged package.
2. **Provider-free discovery** proves Pi loads the staged extension, commands,
   model tool, and skill from that package path.
3. **Local tmux acceptance** exercises broker, panes, routing, restart, and
   retained metadata on the local machine.
4. **Provider-backed acceptance** measures real model behavior and usage. It can
   incur cost and must be explicitly chosen.

Model-free fixtures and serialized-byte/operation counts are proxies. They do
not prove provider cost, cache behavior, reviewer quality, or production-wire
acceptance.

## Prerequisites

- macOS or Linux
- Python 3.11+
- Node.js 22.19+ and npm
- Pi available as `pi`
- tmux 3.2+; 3.5+ recommended
- a reviewed, clean checkout for the default staging command

Run the normal suite first:

```bash
scripts/test.sh
```

## 1. Stage the exact local package

Choose a new persistent directory outside the checkout:

```bash
cd /absolute/path/to/pi-tmux-orchestrator
COMMIT=$(git rev-parse --short=12 HEAD)
STAGE="$HOME/pi-prerelease/pi-tmux-orchestrator-$COMMIT"
mkdir -p "$(dirname "$STAGE")"
scripts/stage-prerelease.sh --output "$STAGE"
```

The command refuses a dirty checkout by default, refuses existing/noncanonical
or in-repository output paths, runs exact package verification, creates the
actual npm tarball with scripts disabled and offline npm configuration, installs
that tarball under the stage, and runs isolated Pi RPC package discovery. It
does not publish or modify the real Pi home, npm home, settings, or auth.

`--allow-dirty` exists only for local script development/smoke. A dirty artifact
is marked `source_state=dirty` and must not be treated as a commit build.

Inspect the bounded provenance:

```bash
python3 -m json.tool "$STAGE/provenance.json"
```

Expected fields include the Git commit/tree, source state, tarball and installed
package-tree SHA-256 values, package name/version, relative tarball/package-root paths, and
`"published": false`. Validate the retained stage again at any time with:

```bash
scripts/run-prerelease-isolated.sh --stage "$STAGE" --check
```

The package keeps the current unreleased package version; the Git commit and
tarball digest identify this pre-release build.

## 2. Provider-free isolated TUI

Use an explicit project you have inspected:

```bash
scripts/run-prerelease-isolated.sh \
  --stage "$STAGE" \
  --project /absolute/path/to/inspected-project
```

The runner validates provenance and the tarball digest, creates disposable
HOME/XDG/npm/Pi directories with no real authentication, disables discovered
extensions/skills, and loads only the staged package with Pi's explicit `-e`
path. It uses offline/update-disabled and blackhole proxy settings as defense in
depth; these are not an OS network sandbox.

In the TUI, do not send a model prompt. Check:

- `/or-dashboard` opens with no running sessions, concise help, and an About
  footer with version, repository, issues, npm, and contribution details, with
  no automatic doctor invocation;
- pressing `d` in the dashboard runs doctor with paths under the disposable environment;
- `/or-models` returns bounded model metadata without credentials;
- command completion contains only `/or-dashboard`, `/or-models`, `/or-start`,
  `/or-send`, and `/or-stop` from the extension;
- the `tmux_orchestrator` model tool and `tmux-agent-orchestrator` skill appear
  as resources from the staged package;
- `/or-start` reaches its bounded preview/confirmation path; cancel before the
  confirmed start because this environment has no provider authentication.

Exit Pi normally. The runner deletes only its disposable environment; the stage
remains for repeat tests.

## 3. Optional provider-backed manual acceptance

This step uses the operator's normal Pi models/authentication and can incur real
provider cost. Perform it only after reviewing the staged provenance and deciding
to spend provider usage.

Resolve the staged package root:

```bash
PACKAGE_ROOT=$(python3 - "$STAGE" <<'PY'
import json
from pathlib import Path
import sys
stage = Path(sys.argv[1])
value = json.loads((stage / "provenance.json").read_text())
print(stage / value["package_root"])
PY
)
```

Record installed settings before the test:

```bash
pi list > /tmp/pi-packages-before.txt
```

Launch Pi with normal authentication but without discovered extension/skill
packages. The explicit staged package remains temporary for this process and is
not added to settings:

```bash
cd /absolute/path/to/inspected-test-project
pi --no-extensions --no-skills -e "$PACKAGE_ROOT" --no-session
```

Run the smallest useful matrix rather than every expensive combination:

### A. Command and parent supervision

- Run `/or-models` and open `/or-dashboard`; confirm help is concise, the About
  footer contains version/project/package/contribution details, and doctor
  appears only after `d`.
- Start one small `single` workflow with only implementer and mandatory reviewer.
- Reopen `/or-dashboard`, confirm the run and usage metadata appear, use Enter
  to attach when the parent Pi is inside tmux, and verify `x` requires explicit
  confirmation before stopping the selected run.
- Confirm the invoking Pi remains the parent and receives lifecycle/final reports.
- Verify idle workers do not poll or issue provider turns.

### B. Phased implementation and review

- Start one bounded complex task with `phased` flow.
- Confirm inspect/plan is read-only, terminates at the plan report, and the next
  implementation assignment receives the bounded plan without inspection turns.
- Confirm only the implementer writes and the built-in reviewer must approve.

### C. Specialist activation

- Enable only specialists relevant to the test.
- Use a documentation-only change to verify deterministic skips are visible to
  the reviewer without waking irrelevant specialists.
- Use an ambiguous/high-risk change or explicit force selection to verify the
  configured specialist runs and review waits for its real report.
- Do not treat synthetic probe/browser evidence as production acceptance.

### D. Workspace capsule experiment

- Use a clean canonical Git root and one or two reviewed relevant paths.
- Enable the workspace capsule and inspect confirmation counts/digest metadata.
- Confirm workers still read governing `AGENTS.md`/`CLAUDE.md` content.
- Exercise one normal source edit and, if needed, a confirmed worker restart;
  normal clean/dirty changes must not stale replay.
- Separately verify changed HEAD/instruction/marker/path trust identity fails
  closed. Keep this experiment opt-in; do not infer savings from local bytes.

### E. TUI/RPC and operator controls

- Run at least one native TUI workflow. Use RPC workers only through an explicit
  request.
- Exercise `status`, `watch`, `attach` (prefix then `L` returns), bounded `send`,
  and one confirmed restart.
- Stop each completed test session explicitly; retained metadata should remain
  readable through Supervisor API v2 without tmux.

### F. Usage and quality evidence

For each provider-backed run, record only bounded non-sensitive facts:

- provider calls and token categories by assignment;
- provider-reported cost when available;
- context occupancy/pressure;
- required checks and their outcomes;
- reviewer findings and revision rounds;
- whether the final result was accepted.

Never post task, prompt, report, provider, diff, log, credential, or private
source bodies in public results.

## 4. Failure capture

If a test fails, capture:

```bash
"$PACKAGE_ROOT/bin/pi-tmux-agents" status SESSION
"$PACKAGE_ROOT/bin/pi-tmux-agents" --json supervisor snapshot SESSION --run RUN_ID
```

Also record the staged commit and tarball digest from `provenance.json`, the test
case, transport, flow/profile, enabled/forced roles, and the exact failed check.
Redact all workflow and provider bodies.

An interrupted assignment or restart is `uncertain`; do not blindly replay it.
Do not replace an existing tmux session.

## 5. Rollback and cleanup

The recommended `-e` procedure does not modify Pi package settings. After all
sessions are explicitly stopped and Pi exits:

```bash
pi list > /tmp/pi-packages-after.txt
diff -u /tmp/pi-packages-before.txt /tmp/pi-packages-after.txt
rm -rf "$STAGE"
rm -f /tmp/pi-packages-before.txt /tmp/pi-packages-after.txt
```

If you deliberately used `pi install "$PACKAGE_ROOT"` instead, remove that exact
local source and verify settings before deleting the stage:

```bash
pi remove "$PACKAGE_ROOT"
pi list
rm -rf "$STAGE"
```

Never remove or overwrite the released/global package merely to test a staged
build. Explicit `--no-extensions --no-skills -e "$PACKAGE_ROOT"` isolates the
pre-release resource selection for one Pi process.
