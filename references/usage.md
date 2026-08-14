# Usage reference

## Commands

### `start`

Creates a detached tmux grid with an implementer, reviewer, broker/status
monitor, and optional probe, Playwright, and Django roles.

Required:

- `--task TEXT` or `--task-file PATH`

Common options:

- `--project PATH`
- `--session NAME`
- `--approve-project`: separately confirmed Pi trust bypass for inspected projects
- `--with-probe` and optional `--probe-task[-file]`
- `--with-playwright` and optional `--playwright-task[-file]`
- `--with-django-expert` and optional `--django-task[-file]`
- `--rpc-workers`: headless RPC event panes instead of interactive TUI panes
- `--attach`
- `--dry-run`
- `--skip-model-check`

Model arguments use `--ROLE-provider`, `--ROLE-model`, and `--ROLE-thinking`.
Thinking levels are `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, and
`max`.

All newly started runs use broker protocol v1. `--rpc-workers` does not select a
different coordination protocol.

### `list`

Lists live tmux sessions marked as Pi Tmux Orchestrator grids.

### `status [SESSION]`

Shows bounded pane metadata, broker workflow state, role lifecycle, actual
provider token totals when available, and context pressure. It never prints
workflow payload bodies.

### `attach [SESSION]`

Switches a tmux client when already inside tmux or attaches from outside.

### `send SESSION --role ROLE --message[-file] ...`

Sends one operator message through the authenticated broker bridge. `steer` and
`follow-up` delivery are supported. A successful response acknowledges
acceptance; completion is observed through lifecycle/events.

Use `--command-id` with a 32-character lowercase hexadecimal ID for retry-safe
deduplication. Conflicting reuse is rejected. An interrupted unprovable delivery
is `uncertain` and requires explicit retry.

### `abort SESSION --role ROLE`

Requests broker-bridge abort for either TUI or RPC presentation. Abort acceptance
does not prove the provider operation reached a terminal state.

### `restart SESSION --role ROLE ... --yes`

Restarts one role and starts a fresh Pi conversation while preserving the
worktree and metadata. Pending unprovable work remains `uncertain`; it is not
blindly replayed.

### `stop SESSION --yes`

Kills only the selected tmux grid. Pi sessions and metadata-only broker state
remain under `~/.pi/agent/orchestrations/`.

### `doctor`

Checks Pi, Python, tmux, tmux extended-key settings, and default model
availability without a provider request.

### `supervisor ...`

Supervisor API v2 reads retained state without tmux runtime observation:

```bash
pi-tmux-agents --json supervisor capabilities
pi-tmux-agents --json supervisor sessions
pi-tmux-agents --json supervisor runs SESSION
pi-tmux-agents --json supervisor snapshot SESSION --run RUN_ID
pi-tmux-agents --json supervisor events SESSION --run RUN_ID \
  --cursor implementer=0 --cursor reviewer=0 --limit 50
```

Host liveness is `not_observed`; retained PIDs do not imply a running process.

## Event-driven workflow

1. Every worker bridge connects to the owner-only run socket.
2. The broker adds task/role baseline context to each Pi session without waking idle roles.
3. It triggers only the implementer and optional initial probe.
4. The implementer submits a bounded `implementation` report with `orchestrator_report`.
5. Enabled specialists inspect the shared worktree and submit typed evidence.
6. The broker supplies all evidence to the reviewer and wakes it once.
7. `changes_requested` supplies one bounded review to the implementer and starts the next round.
8. `approved` marks the run ready without waking the implementer for an acknowledgement turn.

The terminating report tool avoids an extra post-report provider turn. Idle
agents end their turn and never sleep or poll. Timeouts detect failure; they do
not schedule workflow transitions.

Report fields, limits, ACLs, acknowledgements, deduplication, retry, crash
semantics, and token accounting are specified in
[protocol-v1.md](protocol-v1.md).

## Durable state

Files remain for:

- mode-`0700` run/session directories;
- mode-`0600` manifests and authentication tokens;
- Pi's own JSONL sessions;
- one mode-`0600` metadata-only SQLite database;
- a transient startup payload deleted by the broker immediately after reading.

New workers never create or poll Markdown reports, readiness markers, mailbox
payload files, or relay-seen files. The database excludes task, assignment,
report, prompt, message, provider, diff, and log bodies.

Retained manifests from `0.4.x` remain compatible with legacy readers and
controls. There is no option to start that protocol in `0.5.0`.

## Token accounting and budgets

The worker bridge reports Pi/provider values for:

- input and output tokens;
- cache-read and cache-write tokens;
- reasoning tokens when exposed;
- total cost;
- current context tokens/window/percentage.

Unavailable values remain unavailable; no provider token estimate is invented.
Status and Supervisor API expose per-role and total usage. Soft role/run budgets
warn before subsequent work. A budget cannot stop an already-started provider
response at an exact token.

## Security boundaries

- Only the implementer receives normal write tools.
- Read-only roles retain `bash` and are governed by explicit role instructions;
  they are not OS-sandboxed.
- Run directory `0700`; socket/database/token files `0600`.
- Independent role tokens and control token; broker enforces role/report ACLs.
- Same-user peer credential validation where supported.
- No Pi/provider credential reading or copying.
- No TCP listener, cloud service, external message queue, or package dependency.
- Project trust remains explicit and mandatory.
- Status, journals, registries, and Supervisor API never include workflow payloads.
- No exactly-once claim: crash ambiguity is `uncertain`.

## tmux navigation

```text
Ctrl-b q       show pane numbers
Ctrl-b arrows  move between panes
Ctrl-b z       zoom/unzoom current pane
```

Recommended for tmux 3.5+:

```tmux
set -g extended-keys on
set -g extended-keys-format csi-u
```

## Troubleshooting

### Existing session name

Use `status`, explicitly stop it, or select another `--session`. The tool never
replaces an existing tmux session.

### Worker is `disconnected`

Inspect its pane. The bridge reconnects with bounded exponential backoff without
using model turns. A transition in an unprovable window remains `uncertain`.

### Worker is `waiting`

Pi settled while an assignment remained open, usually because it did not call
`orchestrator_report`. Send one focused reminder or restart the role; the broker
does not run an unlimited reminder loop.

### Broker pane exited

Workflow delivery stops. Do not start a legacy relay. Preserve the worktree and
restart or stop/recreate the brokered run after inspecting retained state.

### Project trust prompt

Approve each interactive child only after inspection, use saved/global trust
for RPC presentation, or restart with a separately confirmed `--approve-project`.
