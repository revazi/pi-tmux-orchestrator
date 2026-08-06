# Pi Tmux Orchestrator

A reusable [Pi](https://github.com/badlogic/pi-mono) skill and dependency-free Python CLI for coordinating coding agents in monitorable tmux grids, with an unreleased private `0.4.0-dev.0` Pi package/extension candidate.

It turns the recurring “implementer + reviewer + optional specialist” setup into one command, with durable handoffs and explicit safety boundaries.

## What it provides

- One primary implementer with normal Pi coding tools
- One independent reviewer with read/verification tools
- An optional workflow-read-only integration, contract, security, or runtime probe
- An optional workflow-read-only Playwright tester for real local browser behavior
- An optional workflow-read-only senior Django expert for framework-specific review
- A live relay/status pane that validates and routes report-ready transitions
- Numbered implementation, specialist, and review rounds
- Configurable provider, model, and thinking level per role
- Role messaging and model restart commands
- External coordination state that does not pollute project repositories
- Model-free dry runs and functional smoke tests
- An opt-in versioned JSON CLI boundary and thin Pi extension that delegates to it

## Default grid

```text
┌──────────────────────────────┬──────────────────────────────┐
│ Implementer                  │ Reviewer                     │
│ openai-codex/gpt-5.6-sol     │ openai-codex/gpt-5.4         │
│ xhigh                        │ high                         │
├──────────────────────────────┼──────────────────────────────┤
│ Optional probe               │ Optional Playwright tester   │
│ openai-codex/gpt-5.4-mini    │ openai-codex/gpt-5.4         │
│ high                         │ high                         │
├──────────────────────────────┼──────────────────────────────┤
│ Optional Django expert       │ Relay + status               │
│ openai-codex/gpt-5.4         │ handoffs and health          │
│ high                         │                              │
└──────────────────────────────┴──────────────────────────────┘
```

The default grid has implementer, reviewer, and monitor panes. Optional roles add panes, and tmux tiles the resulting grid.

## Requirements

- Pi coding agent available as `pi` (extension candidate tested with Pi 0.80.10 and designed for newer compatible APIs)
- Python 3.11+
- Node 22.19+ for candidate package verification/evaluation
- tmux 3.2+; tmux 3.5+ is recommended
- A project already inspected and trusted before using `--approve-project`

Recommended `~/.tmux.conf` for tmux 3.5+:

```tmux
set -g extended-keys on
set -g extended-keys-format csi-u
```

## Private candidate and disposable evaluation

`0.4.0-dev.0` is an unreleased, private, non-publishable candidate (`private: true`, `UNLICENSED`). Do **not** install this candidate into the real Pi home or publish it. The extension imports no Pi core package, so the manifest declares no dependencies or peers and owns no runtime tree.

Evaluate source-package discovery through isolated Pi RPC:

```bash
scripts/pi-extension-smoke.sh
```

The smoke uses a disposable `PI_CODING_AGENT_DIR`, sends only RPC `get_commands`, tolerates the extension's status/widget lifecycle events, and asserts exactly the three extension commands plus `skill:tmux-agent-orchestrator`. It sends no prompt and makes no provider request. For a disposable standalone-skill smoke, redirect the legacy installer too:

```bash
TEMP_PI_HOME=$(mktemp -d)
PI_AGENT_HOME="$TEMP_PI_HOME" ./install.sh
PATH="$TEMP_PI_HOME/bin:$PATH" pi-tmux-agents --version
rm -rf "$TEMP_PI_HOME"
```

The legacy `install.sh` remains the standalone CLI/skill fallback and does not install the extension. Existing `0.3.0` installations are not migrated automatically. No Git-package update or rollback acceptance is claimed; neither flow is exercised here.

## Start from Pi

When loaded, the extension provides:

- tool `tmux_orchestrator`: `doctor`, `list`, `status`, `start`, and `send`
- `/orchestrate`: confirmed interactive start
- `/orchestrations`: metadata-only list/widget refresh
- `/orchestrator-stop`: stop with explicit UI confirmation

The tool intentionally excludes restart and stop. Start is rejected outside the interactive TUI because confirmation is required. Parent trust is checked before an optional child `--approve`; a separate confirmation is required for every run, and parent trust is never treated as inherited by children.

Without the extension, use the skill/CLI fallback in a new Pi session:

```text
/skill:tmux-agent-orchestrator Start an implementer and reviewer for the current task and attach.
```

Ask for a probe when useful:

```text
/skill:tmux-agent-orchestrator Start an implementer, reviewer, and independent API contract probe for this task.
```

Ask for Playwright or Django review only when the task benefits from those specialists:

```text
/skill:tmux-agent-orchestrator Add a Playwright tester and Django expert to the implementer/reviewer workflow for this task.
```

The skill writes the agreed task to files and invokes the CLI rather than rebuilding prompts and tmux panes manually.

## Start from the terminal

Create a task file:

```bash
cat > /tmp/pi-agent-task.md <<'TASK'
Implement the requested change, add focused tests, run project verification, and stop after independent review approval.
TASK
```

Start the default two-agent workflow:

```bash
pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md \
  --approve-project \
  --attach
```

Add a focused probe:

```bash
cat > /tmp/pi-agent-probe.md <<'PROBE'
Independently inspect the provider request and response contract using synthetic data only. Report concrete gaps and limitations.
PROBE

pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md \
  --with-probe \
  --probe-task-file /tmp/pi-agent-probe.md \
  --approve-project \
  --attach
```

Add browser and Django specialists with focused task files:

```bash
pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md \
  --with-playwright \
  --playwright-task-file /tmp/pi-agent-playwright.md \
  --with-django-expert \
  --django-task-file /tmp/pi-agent-django.md \
  --approve-project \
  --attach
```

Each role supports matching `--ROLE-provider`, `--ROLE-model`, and `--ROLE-thinking` overrides. Playwright and Django are also valid roles for `send` and `restart`.

## Manage grids

```bash
pi-tmux-agents list
pi-tmux-agents status SESSION
pi-tmux-agents attach SESSION
pi-tmux-agents send SESSION --role implementer --message "Prioritize the failing regression."
pi-tmux-agents restart SESSION --role implementer \
  --provider openai-codex --model gpt-5.6-sol --thinking xhigh --yes
pi-tmux-agents stop SESSION --yes
```

A restart preserves project files and coordination state but starts a fresh Pi conversation for that role.

## Handoff flow

1. The implementer writes `handoff-N.md` and `handoff-N.ready`.
2. After validating the marker and a regular non-empty handoff report, the relay notifies the reviewer and enabled Playwright/Django specialists.
3. Playwright writes `playwright-N.md` (`PASS` or `FAIL`) and `playwright-N.ready`.
4. Django writes `django-review-N.md` (`ADVISORY_APPROVED` or `ISSUES_FOUND`) and `django-review-N.ready`.
5. The reviewer waits for enabled specialist reports, then writes `review-N.md` (`APPROVED` or `CHANGES_REQUESTED`) and `review-N.ready`.
6. The relay routes specialist reports to implementer/reviewer and the review to the implementer only after validating each required first-line result.
7. Requested changes start another numbered round with fresh specialist reports.
8. Approval produces `implementation-ready.md` and stops before push or merge unless explicitly authorized.
9. An optional probe writes `probe.md` and `probe.ready`; the relay informs implementer and reviewer.

Coordination records live under:

```text
~/.pi/agent/orchestrations/<session>/<run>/
```

They are created with private permissions and remain outside the target repository. Status and monitor views show report file names and byte sizes, never report previews.

## Safety model

- Only the implementer receives Pi's normal write tools.
- Reviewer, probe, Playwright tester, and Django expert are launched without `edit` and `write`.
- Workflow-read-only roles retain `bash` for verification and are not OS-sandboxed, so prompts also prohibit tracked modifications.
- Playwright artifacts and test data are restricted to ignored or external temporary paths, with bounded process cleanup.
- Child sessions read the target project's governing instructions before acting.
- `--approve-project` is explicit because it bypasses child trust prompts.
- Existing tmux sessions are never replaced; every operation on an existing session/window uses an exact target, so a vanished target cannot fall through to a prefix collision during attach, status, stop, or cleanup.
- State root, session, and run directories must be canonical non-symlink directories; state files must be regular non-symlink files.
- Schema-v1 manifests are strictly validated before acting on an existing orchestration's panes or processes.
- Ready markers remain pending until their report is valid and transport succeeds for every enabled recipient; successful recipients are not notified again during another recipient's retry.
- Tmux `send-keys` success is transport-level only. It does not prove that Pi processed or acknowledged a notice.
- Failed starts retain a private `startup-state` diagnosis and kill any partial tmux session.
- The orchestrator never reads or copies Pi authentication files.
- `pi --list-models` validates availability without making a model request.
- Role prompts are attached by file rather than exposed as command-line payloads.
- Handoffs must not contain credentials, private documents, prompts, provider payloads, endpoints, or raw errors.

See [SECURITY.md](SECURITY.md) and [references/usage.md](references/usage.md) for details.

## JSON CLI boundary

Put `--json` before or after the command to emit exactly one JSON object on stdout. The v1 envelope is:

```json
{"schema_version":"1","command":"status","success":true,"data":{},"error":null}
```

Failures return nonzero with `data: null` (or bounded diagnostic data for checks) and `error: {"code":"...","message":"..."}`. `doctor`, `list`, `status`, `start`, `send`, `restart`, and `stop` return structured metadata. `attach` fails with `interactive_only`. Task, prompt, report, provider, specialist, and message bodies are never returned; list/status expose bounded names, paths, role/pane records, and file sizes only.

## Development

Run all local checks:

```bash
scripts/test.sh
```

The suite includes 39+ standard-library Python tests, Node built-in extension tests, deterministic `npm pack --dry-run --json` verification, a disposable tarball install, an isolated Pi RPC command/skill discovery smoke, and the existing six-pane tmux smoke. It sends no prompt or provider request and does not inspect real authentication files.

## Project status

Current candidate: `0.4.0-dev.0`, private and unreleased. The Python/tmux CLI remains the authoritative process/data plane; the extension only invokes its JSON mode with argument arrays. Child Pi TUIs remain separate processes, with no SDK/RPC child bridge.

This repository is not published to npm, PyPI, or a Pi gallery and does not distribute credentials, model access, or provider configuration.