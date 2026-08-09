# Pi Tmux Orchestrator

A reusable [Pi](https://github.com/badlogic/pi-mono) extension, skill, and dependency-free Python CLI with a persistent, project-neutral Pi controller for coordinating coding agents in monitorable tmux grids. The source package is prepared as version `0.4.0`; that publish-ready state is not evidence that an npm release or Pi gallery entry exists.

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
- One persistent, project-neutral controller Pi session with stable identity and private storage
- Optional headless Pi RPC workers with private mailbox transport, idempotency keys, durable worker registries, lifecycle journals, follow-up delivery, and abort
- A versioned, tmux-independent supervisor read API for retained sessions/runs, snapshots, per-role event cursors, and exact command status

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

## Implementation architecture

`bin/pi-tmux-agents` is only an executable launcher. The authoritative standard-library implementation lives in `pi_tmux_orchestrator/`, separated into CLI/commands, controller, tmux, storage/manifest validation, prompts, relay, RPC protocol/store/supervisor modules, the versioned `supervisor_api` service, and separate supervisor CLI handlers. The JSON CLI remains the stable control-plane boundary used by the thin Pi extension and future Pi Deck clients. Tmux currently hosts panes and attachment; the supervisor read API consumes only private durable state and never queries tmux for runtime truth.

## Requirements

- Pi coding agent available as `pi` (package extension tested with Pi 0.80.10 and designed for newer compatible APIs)
- Python 3.11+
- Node 22.19+ for package verification/evaluation
- Ruff 0.11.11 for repository development checks (not a runtime/package dependency)
- tmux 3.2+; tmux 3.5+ is recommended
- A project already inspected and trusted before using `--approve-project`

Recommended `~/.tmux.conf` for tmux 3.5+:

```tmux
set -g extended-keys on
set -g extended-keys-format csi-u
```

## Package installation and evaluation

Version `0.4.0` is a real Pi package in this repository. After inspecting the source, install a local checkout in place or install the public Git package:

```bash
pi install /absolute/path/to/pi-tmux-orchestrator
pi install git:github.com/revazi/pi-tmux-orchestrator
```

For a temporary run that does not add the package to settings, use:

```bash
pi -e /absolute/path/to/pi-tmux-orchestrator
pi -e git:github.com/revazi/pi-tmux-orchestrator
```

The unversioned Git form uses the public repository's current default-branch source at installation time; use an explicit tag or commit when one is available and reproducibility is required. Pi packages execute with the current user's full permissions, so inspect the source before installation.

The scoped npm manifest is publish-ready, but npm availability is still unverified. Only after an operator confirms that exact registry version exists should these commands be described as usable:

```bash
pi install npm:@revazi/pi-tmux-orchestrator@0.4.0
pi -e npm:@revazi/pi-tmux-orchestrator@0.4.0
```

Run the package acceptance in disposable homes before publication:

```bash
scripts/package-smoke.sh
```

The smoke builds the exact 29-file modular tarball, installs it with npm scripts disabled and an empty dependency tree, then uses isolated `pi install <local-package-root>` and launches Pi RPC without `--extension`. It requires package-provenance discovery of exactly nine extension commands plus the root skill, performs an offline `npm publish --dry-run` against a loopback registry, sends no prompt/provider request, and does not use configured Pi/npm homes or credentials.

The package imports no Pi core module, so it declares no dependencies or peers and owns no runtime tree. It is distributed under the MIT License in [LICENSE.md](LICENSE.md).

The legacy `install.sh` remains a standalone CLI/root-skill fallback and does not install the extension. Existing standalone installations are not migrated automatically. Evaluate the installer only with a disposable destination:

```bash
TEMP_PI_HOME=$(mktemp -d)
PI_AGENT_HOME="$TEMP_PI_HOME" ./install.sh
PATH="$TEMP_PI_HOME/bin:$PATH" pi-tmux-agents --version
rm -rf "$TEMP_PI_HOME"
```

No npm-registry, Pi-gallery, Git-package update, or rollback acceptance is claimed by the local smokes.

## Persistent controller session

Start one detached controller Pi session independently of any target repository:

```bash
pi-tmux-agents controller start
pi-tmux-agents controller status
pi-tmux-agents controller attach
```

The controller has the stable Pi session ID `pi-tmux-orchestrator-controller-v1`, resumes the same Pi conversation after a confirmed stop/start, and runs in a neutral workspace under `~/.pi/agent/orchestrator-controller/`. Its state, workspace, prompt, and dedicated Pi session directory are private and external to target repositories. Set `PI_TMUX_CONTROLLER_HOME` to choose another controller root.

The controller explicitly loads this package's extension and skill when they are colocated with the CLI, ignores project context files in its neutral workspace, omits `edit` and `write`, and requires an explicit target project for controller-mode starts. Existing orchestration grids remain independent and can outlive the controller. Tmux is currently the controller's attachable terminal host, not its persistent Pi identity or source of orchestration state.

Stop only after explicit confirmation; the Pi conversation and controller state are retained:

```bash
pi-tmux-agents controller stop --confirm
```

The command refuses duplicate starts and never replaces an unrelated tmux session using the reserved `pi-orchestrator-controller` name. Controller mode also blocks Pi `/new`, `/resume`, `/fork`, and `/clone` transitions so the TUI cannot silently leave its fixed controller identity. Controller attach remains interactive-only and cannot be used through JSON mode.

## Start from Pi

The installed extension exposes exactly these slash commands:

| Command | Behavior |
| --- | --- |
| `/orchestrator-help` | Show a bounded command overview without a subprocess. |
| `/orchestrator-doctor` | Run the authoritative JSON CLI prerequisite checks without a provider request. |
| `/orchestrator-start [task]` | Collect a task when omitted, select optional roles and TUI/RPC worker transport, enforce parent/child trust boundaries, preview, and confirm before start. |
| `/orchestrator-list` | List running orchestrations and refresh the bounded metadata-only widget. |
| `/orchestrator-status [session]` | Show metadata-only status for an exact session, or use safe unambiguous current-project resolution when omitted. |
| `/orchestrator-send [session]` | Collect an exact session when omitted, select one of five roles, edit a non-empty private message, and send it through a unique mode-`0600` file. |
| `/orchestrator-stop [session]` | Collect an exact session when omitted and require explicit confirmation before delegated `--yes`. |
| `/orchestrate` | Backward-compatible alias for `/orchestrator-start`. |
| `/orchestrations` | Backward-compatible alias for `/orchestrator-list`. |

The `tmux_orchestrator` model tool remains available for bounded `doctor`, `list`, `status`, `start`, and `send` actions; it intentionally excludes stop and restart. Start and slash-command send/stop require the interactive TUI. Parent trust is checked before an optional child `--approve`, a separate confirmation is required for every run, and parent trust is never treated as inherited by children. Message text never enters subprocess argv, status, details, notifications, or widgets.

Attach, the supervisor API, RPC events/abort, and restart remain terminal/CLI-only. Attach takes over the terminal; abort requires RPC workers; restart requires explicit confirmation and provider/model/thinking configuration. The extension uses Pi's existing dialogs; Pi Deck will consume the supervisor API in a separate phase.

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

Use acknowledged headless RPC workers instead of interactive child Pi TUIs when desired:

```bash
pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md \
  --rpc-workers \
  --approve-project \
  --attach
```

Without `--approve-project`, non-interactive RPC workers apply an existing saved Pi trust decision or global `defaultProjectTrust`. The default `ask` and `never` policies load context instructions but ignore project-local executable resources without prompting; `always` trusts them. Interactive TUI workers remain the default. RPC panes are read-only event streams; steer them with `send`, queue later work with `--delivery follow-up`, and interrupt active work with `abort` rather than typing into the pane. Each RPC role has a stable worker ID, a generation counter, a bounded command registry, and a private metadata-only lifecycle journal.

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

The dedicated controller can manage grids through the `/orchestrator-*` commands, or use the same authoritative CLI directly:

```bash
pi-tmux-agents list
pi-tmux-agents status SESSION
pi-tmux-agents --json supervisor capabilities
pi-tmux-agents --json supervisor sessions
pi-tmux-agents --json supervisor snapshot SESSION --run RUN_ID
pi-tmux-agents --json supervisor events SESSION --run RUN_ID \
  --cursor implementer=0 --cursor reviewer=0 --limit 50
pi-tmux-agents --json supervisor command SESSION --run RUN_ID \
  --role implementer --command-id 0123456789abcdef0123456789abcdef
pi-tmux-agents attach SESSION
pi-tmux-agents send SESSION --run RUN_ID --role implementer \
  --message "Prioritize the failing regression."
pi-tmux-agents send SESSION --role reviewer --delivery follow-up \
  --command-id 0123456789abcdef0123456789abcdef \
  --message "Review this after the current run settles."
pi-tmux-agents abort SESSION --role implementer
pi-tmux-agents events SESSION --role reviewer --after 0 --limit 50
pi-tmux-agents restart SESSION --role implementer \
  --provider openai-codex --model gpt-5.6-sol --thinking xhigh --yes
pi-tmux-agents stop SESSION --yes
```

A restart preserves project files and coordination state but starts a fresh Pi conversation for that role. Follow-up delivery and abort require an RPC-worker grid. `--command-id` accepts an optional 32-character lowercase hexadecimal idempotency key; a retry with the same role, command type, and delivery is acknowledged from the durable registry without forwarding the payload again. Reusing an ID with conflicting metadata is rejected; for matching metadata, the first accepted payload wins and later payload text is intentionally neither compared nor hashed. RPC send/abort success still means Pi accepted the command, while lifecycle events distinguish received, accepted, started, completed, failed, aborted, rejected, and crash-uncertain states.

The supervisor API version is independent of the outer JSON envelope version. `supervisor capabilities` explicitly describes acceptance-versus-completion, crash-`uncertain`, and non-exactly-once semantics. `supervisor sessions` discovers bounded retained state, `runs` selects exact history, `snapshot` exposes durable worker/runtime metadata, `events` returns independent per-role pages using repeatable `--cursor ROLE=SEQUENCE`, and `command` polls one idempotency key without returning its payload. These reads do not inspect tmux and remain available after the host session exits. They intentionally report the tmux host runtime as `not_observed`; retained process metadata is not a liveness claim. `send` and `abort` accept `--run RUN_ID` so an API client can target an exact RPC run without resolving a tmux session.

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
- The controller uses a reserved exact tmux session, a stable Pi session ID, a neutral no-context workspace, and strict private state markers; duplicate or unmarked name collisions are refused.
- State root, session, and run directories must be canonical non-symlink directories; state files must be regular non-symlink files.
- Schema-v1 and transport-aware schema-v2 manifests are strictly validated before acting on an existing orchestration's panes or processes.
- Ready markers remain pending until their report is valid and transport succeeds for every enabled recipient; successful recipients are not notified again during another recipient's retry.
- TUI-worker `tmux send-keys` success is transport-level only. RPC-worker mailbox delivery waits for Pi's correlated command response and reports an acknowledgement, but that acknowledgement proves acceptance/queueing rather than task completion.
- RPC mailbox payloads use unique mode-`0600` files under the private coordination directory and are deleted after forwarding, rejection, or client timeout; cleanup is not a secure-erasure claim.
- RPC registries and rotating event journals contain only bounded process/session/command metadata, never task or message bodies. A crash after forwarding but before Pi's response is recorded as `uncertain` and is never blindly redelivered under the same command ID.
- Supervisor API scans, pages, cursors, issue lists, and command lookups are bounded. Its host status is explicitly `not_observed`, so stale retained PIDs or runtime records are never presented as proof that tmux or a worker is alive.
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

Failures return nonzero with `data: null` (or bounded diagnostic data for checks) and `error: {"code":"...","message":"..."}`. `doctor`, `controller`, `supervisor`, `list`, `status`, `events`, `start`, `send`, `abort`, `restart`, and `stop` return structured metadata. Grid attach and `controller attach` fail with `interactive_only`. Task, prompt, report, provider, specialist, and message bodies are never returned; list/status expose bounded names, paths, role/pane records, and file sizes only.

## Author and license

Created and maintained by [Revaz Zakalashvili](https://github.com/revazi) (`@revazi`). Public contact: [revaz.zakalashvili@gmail.com](mailto:revaz.zakalashvili@gmail.com).

Licensed under the [MIT License](LICENSE.md). Copyright (c) 2026 Revaz Zakalashvili.

## Development

Run all local checks with the pinned CI Ruff version available:

```bash
python -m pip install ruff==0.11.11
scripts/test.sh
```

The suite includes 79+ standard-library Python tests, Node extension tests for the exact nine-command surface plus controller/RPC/trust/private-message boundaries, deterministic manifest/pack verification, npm installation of the actual tarball, isolated Pi local-package installation plus package-provenance RPC discovery, an offline npm publication dry-run, and a model-free controller plus default-TUI and acknowledged five-worker RPC/tmux smoke. It sends no real prompt or provider request and isolates Pi/npm configuration from real authentication files.

## Project status

Current source package: `0.4.0`, technically publish-ready and MIT-licensed. The authoritative process/data plane lives in the modular `pi_tmux_orchestrator/` Python package; `bin/pi-tmux-agents` is only a launcher and the extension invokes its JSON mode with argument arrays. Worker transport is selectable: interactive Pi TUIs remain the default, while opt-in RPC supervisors provide correlated control, durable metadata-only registries, lifecycle events, crash-uncertain recovery, and idempotent retries without adding dependencies. Supervisor API v1 now provides the bounded tmux-independent client boundary; the next major phase is the richer shared Pi Deck TUI as its client, followed by extracting worker hosting from the tmux adapter.

A version in source, a tarball, and successful dry-runs do not prove npm-registry or Pi-gallery publication. This repository does not distribute credentials, model access, or provider configuration.
