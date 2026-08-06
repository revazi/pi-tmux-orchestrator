# Usage reference

## Package distribution status

The source and local artifact are prepared as `0.4.0`, with public scoped-package access metadata and `pi-package` discovery. That does not prove the version has been published to npm or listed in a Pi gallery. Once a human verifies publication, use `pi install npm:@revazi/pi-tmux-orchestrator@0.4.0` for a pinned persistent install or `pi -e npm:@revazi/pi-tmux-orchestrator@0.4.0` for one temporary run.

Before publication, `scripts/package-smoke.sh` packs and installs the exact 10-file artifact in disposable locations, validates its MIT/author metadata and empty owned dependency tree, runs isolated Pi RPC `get_commands` against the installed package, and performs an isolated offline npm publication dry-run. Local-path evaluation is also available with `pi --no-session -e /absolute/path/to/pi-tmux-orchestrator`; use a disposable `PI_CODING_AGENT_DIR` when the real Pi home must remain untouched. Persistent inspected-source fallbacks are `pi install /absolute/path/to/pi-tmux-orchestrator` and, only after the tag exists, `pi install git:github.com/revazi/pi-tmux-orchestrator@v0.4.0`.

The extension imports no Pi core package, so the package declares no dependency or peer tree. The owner-authorized license is MIT; see [`LICENSE.md`](../LICENSE.md). The legacy `install.sh` installs only the standalone CLI/root skill, does not install the extension, and does not migrate an existing standalone installation. npm registry, Pi gallery, Git-package update, and rollback acceptance remain unclaimed unless separately exercised.

When available, prefer tool `tmux_orchestrator` (`doctor`, `list`, `status`, `start`, `send`) and commands `/orchestrate`, `/orchestrations`, and `/orchestrator-stop`. Restart remains CLI-only, stop is command-only with UI confirmation, and start requires the interactive TUI.

## JSON boundary

`pi-tmux-agents --json COMMAND ...` emits one schema-v1 JSON object on stdout on success or failure and uses a nonzero exit code for failure:

```json
{"schema_version":"1","command":"list","success":true,"data":{"sessions":[]},"error":null}
```

The envelope always has exactly `schema_version`, `command`, `success`, `data`, and `error`. Errors contain bounded `code` and `message` fields. Arrays are bounded and report truncation. Roles, panes, files, model checks, and paths are structured values. `list` and `status` remain metadata-only: no task, message, prompt, report, provider payload, or specialist body is included. `attach` returns `interactive_only` in JSON mode.

## Commands

### `start`

Creates a detached tmux session with an implementer, reviewer, optional technical probe, optional Playwright tester, optional Django expert, and relay/status monitor.

Required:

- `--task TEXT` or `--task-file PATH`

Common options:

- `--project PATH`: defaults to the current directory
- `--session NAME`: defaults to `pi-<project>-agents`
- `--approve-project`: passes Pi's `--approve` flag to child sessions; use only after project trust is established
- `--with-probe`: adds the optional read-only technical probe pane
- `--probe-task TEXT` or `--probe-task-file PATH`: focused technical probe instructions
- `--with-playwright`: adds an optional read-only Playwright test pane
- `--playwright-task TEXT` or `--playwright-task-file PATH`: focused browser-test instructions
- `--with-django-expert`: adds an optional read-only senior Django review pane
- `--django-task TEXT` or `--django-task-file PATH`: focused Django review and best-practice instructions
- `--attach`: switches/attaches after startup
- `--dry-run`: validates commands, models, project, and configuration without creating files or panes
- `--skip-model-check`: skips catalog availability validation for custom/dynamic model setups

Role model options follow this pattern:

```text
--implementer-provider PROVIDER
--implementer-model MODEL
--implementer-thinking LEVEL
--reviewer-provider PROVIDER
--reviewer-model MODEL
--reviewer-thinking LEVEL
--probe-provider PROVIDER
--probe-model MODEL
--probe-thinking LEVEL
--playwright-provider PROVIDER
--playwright-model MODEL
--playwright-thinking LEVEL
--django-provider PROVIDER
--django-model MODEL
--django-thinking LEVEL
```

Thinking levels: `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`.

### `list`

Lists sessions created by this skill. Unrelated tmux sessions are ignored.

### `status [SESSION]`

Shows role panes, process state, project path, coordination directory, and bounded handoff metadata. Coordination files are displayed by name and byte size only; report content and first-line previews are not printed. If the session is omitted, the command uses the current project's conventional session when unambiguous.

### `attach [SESSION]`

Inside tmux, switches the current client. Outside tmux, attaches normally.

### `send SESSION --role ROLE --message TEXT`

Sends a steering message to an agent. Prefer a mode-`0600` `--message-file` for sensitive or longer instructions. The extension always uses a unique private temporary file and removes it in `finally`; message bodies are never put in child process arguments or returned JSON. The message is submitted immediately; Pi queues it safely if the agent is currently working.

### `restart SESSION --role ROLE ... --yes`

Restarts one role with optional new provider/model/thinking settings. Project files and mailbox state remain; the Pi role conversation is fresh. This is useful after provider denial or a requested model change.

### `stop SESSION --yes`

Kills only the selected orchestration's tmux session. Coordination records remain under `~/.pi/agent/orchestrations/` for audit and diagnosis.

### `doctor`

Checks Pi, Python, tmux, tmux extended-key settings, and default model availability without making provider requests.

## Handoff protocol

The coordination directory is external to the repository and mode `0700`.

1. Implementer writes `handoff-N.md` and `handoff-N.ready`.
2. Relay verifies that both marker and matching handoff report are regular files under the run and that the report is non-empty, then submits a notification to the reviewer and optional Playwright/Django panes.
3. Optional Playwright tester runs the real local test application, writes `playwright-N.md` beginning with `PASS` or `FAIL`, and creates `playwright-N.ready`.
4. Optional Django expert reviews ORM/settings/lifecycle/database/security/testing best practices, writes `django-review-N.md` beginning with `ADVISORY_APPROVED` or `ISSUES_FOUND`, and creates `django-review-N.ready`.
5. Reviewer waits for matching specialist reports, then writes `review-N.md`; its first line is `APPROVED` or `CHANGES_REQUESTED`, followed by `review-N.ready`.
6. Relay accepts only `PASS`/`FAIL`, `ADVISORY_APPROVED`/`ISSUES_FOUND`, and `APPROVED`/`CHANGES_REQUESTED` as the applicable first-line values, then submits specialist and review notices to their intended recipients.
7. Requested changes produce another numbered round, including fresh specialist reviews.
8. Approval causes the implementer to write `implementation-ready.md` and stop before push/merge unless the task and repository workflow explicitly authorize more.
9. Optional technical probe writes `probe.md` and `probe.ready`; relay informs both implementation and review agents.

The relay transports file paths and state transitions, not source documents or provider payloads. A missing, empty, symlinked, non-regular, or invalid-enum report leaves its marker pending. Delivery is recorded per marker and recipient; a successful recipient is not duplicated while another recipient retries, and global completion is recorded only after every enabled intended recipient succeeds. Tmux `send-keys` success is transport-level only and is not a Pi acknowledgement.

## Security boundaries

- Only the implementer receives Pi's normal write tools.
- Reviewer, technical probe, Playwright tester, and Django expert are launched without `edit` or `write`; they retain `bash` for tests, so role prompts also explicitly prohibit tracked modifications.
- The Playwright tester may create browser caches, screenshots, traces, logs, and test databases only under ignored or external temporary paths and must clean up local servers/browser processes.
- The orchestrator never reads or copies Pi authentication files.
- Model validation uses `pi --list-models`, which checks configured availability but sends no model request.
- State root, session, and run paths must resolve within the configured orchestration root and may not themselves be symlink directories. State files used by the orchestrator must be regular non-symlink files.
- Schema-v1 manifests require exact structural, role, pane, trust, canonical path, and containment validation before actions on an existing orchestration.
- Manifest updates use unique mode-`0600` temporary files and atomic replacement. Failed starts kill partial sessions and retain a private `startup-state` diagnosis when safe.
- Avoid task text on the command line when it contains sensitive project information; use `--task-file`.
- Never place credentials, raw career documents, private customer data, prompts, provider responses, or raw provider errors in coordination files.
- `--approve-project` bypasses child-session trust prompts and must be limited to a project already inspected and trusted. The extension additionally requires `ctx.isProjectTrusted()` and explicit per-run UI confirmation; parent trust is never automatically inherited by children.
- Extension start/probe/specialist/message bodies use unique mode-`0600` temporary files, are passed only via `--*-file`, and are removed in `finally`.
- The extension starts no background poller or resource in its factory; status/widget refresh occurs only on command, tool, or session lifecycle points.

## tmux navigation

```text
Ctrl-b q       show pane numbers
Ctrl-b arrows  move between panes
Ctrl-b z       zoom/unzoom current pane
```

Recommended `~/.tmux.conf` for tmux 3.5+:

```tmux
set -g extended-keys on
set -g extended-keys-format csi-u
```

## Troubleshooting

### Provider error in one role

Use `restart` to select another available model without rebuilding the grid:

```bash
pi-tmux-agents restart SESSION --role implementer \
  --provider openai-codex --model gpt-5.6-sol --thinking xhigh --yes
```

### Existing session name

Inspect it with `status`, stop it explicitly, or pass another `--session` name. The tool never replaces an existing tmux session automatically. Existing session/window operations use exact tmux targets, so if the selected orchestration disappears during an operation, a prefix-named session is not substituted.

### Relay pane exited

Project agents continue running, but automatic notifications stop. Restarting relay is intentionally conservative; send messages manually with `send`, or stop and create a new orchestration after preserving work. If a report-ready marker remains pending, verify that its matching report is a regular non-empty file with the required first-line result; transport failures are retried without resending to recipients already recorded as successful.

### Child session asks for project trust

Approve it interactively in each pane, or stop and restart with `--approve-project` after confirming the project is trusted.
