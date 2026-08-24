# Usage reference

## Commands

Every canonical `/orchestrator-*` Pi command also has an `/or-*` short alias,
for example `/or-attach` and `/or-status`. Aliases share the canonical handler,
selector, confirmation, and safety behavior.

A regular interactive Pi session checks the public npm package metadata once at
startup and shows a non-blocking warning only when a newer release is available.
Use `/or-about` for installed/latest versions and update links, update with
`pi update npm:pi-tmux-orchestrator`, or set
`PI_TMUX_ORCHESTRATOR_DISABLE_UPDATE_NOTICE=1` to disable the startup check.
Worker and controller sessions never perform this check.

### Model policy

The strict user-global file `~/.pi/agent/tmux-orchestrator.json` supports:

```json
{
  "version": 1,
  "defaults": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-5",
    "thinking": "high"
  },
  "roles": {
    "reviewer": {
      "provider": "google",
      "model": "gemini-3.1-pro-preview",
      "thinking": "medium"
    }
  }
}
```

The file is relative to `PI_CODING_AGENT_DIR`; an absolute
`PI_TMUX_ORCHESTRATOR_CONFIG` overrides its location. Only version, defaults,
roles, provider, model, and thinking fields are accepted. It must never contain
credentials. Explicit `--ROLE-*` or model-tool `modelOverrides` values win over
role configuration, global defaults, and packaged fallbacks, in that order.

The model tool's `models` action and `/or-models [query]` expose a maximum of 100
available model metadata rows from the current Pi registry, respecting scoped
models and never exposing authentication. Natural-language requests can use
`useParentModel` for the current Pi provider/model/thinking or exact `all` and
per-role `modelOverrides` after resolving ambiguous IDs with `models`.

### `start`

Creates a detached tmux grid with an implementer, reviewer, broker/status
monitor, and optional probe, Playwright, and Django roles. The monitor is an
in-place, event-driven dashboard with full, compact, and narrow layouts; it does
not tail worker output or poll broker state.

Required:

- `--task TEXT` or `--task-file PATH`

Common options:

- `--project PATH`
- `--session NAME`
- `--context-capsule TEXT` or `--context-capsule-file PATH`: optional bounded parent recap
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

The model tool accepts a structured `contextCapsule` with optional current
state, settled decisions, constraints, acceptance criteria, relevant paths,
known evidence, open questions, and out-of-scope arrays. The invoking parent
synthesizes it from context already present in that Pi turn; no summarizer agent
or additional model request is used. The rendered capsule is at most 12 KiB.
Do not copy a transcript, prompts, provider bodies, credentials, logs, or diffs.
CLI callers may provide equivalent reviewed Markdown with the capsule options.
The start confirmation shows only presence and character count.

All newly started runs use broker protocol v1. `--rpc-workers` does not select a
different coordination protocol. RPC panes are a plain headless automation
view; interactive TUI panes are the default and retain Pi's native highlighting,
tool rendering, and input editor for direct steering.

### `list`

Lists live tmux sessions marked as Pi Tmux Orchestrator grids. In the Pi TUI,
omitting `[SESSION]` from `status`, `watch`, `attach`, `send`, or `stop` opens a
selector populated from this metadata-only list. Each option shows the exact
session and project; invalid orchestration metadata is excluded.

### `status [SESSION]`

Shows bounded pane metadata, broker workflow state, role lifecycle, actual
provider token totals when available, and context pressure. The live broker
pane presents the same metadata hierarchy with exact configured
provider/model/thinking values, assignment/generation where available,
soft-budget warnings, a bounded metadata-event rail, and attach/status/stop
help. Healthy/success is green, active is cyan, attention/budget is yellow,
error/uncertainty is red, and secondary metadata is dim. `NO_COLOR`,
`TERM=dumb`, and non-TTY output remain plain. Neither view prints workflow
payload bodies. See [broker dashboard design](dashboard-design.md) for the
operator hierarchy, wireframe, responsive contract, and omissions.

### `attach [SESSION]`

Switches the existing tmux client when already inside tmux or attaches from
outside. JSON mode is allowed only for the in-tmux `switch-client` path used by
the Pi extension; it never attempts to replace or suspend the invoking Pi
process. Prefix then `L` is the detach/return operation: it switches that exact
client back to the invoking Pi while leaving the worker grid running. Reattach
with the same `attach` command.

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

Respawns one role's worker process through a broker-authoritative generation
handover and reopens its exact Pi session ID, preserving the conversation and
complete JSONL history. The live broker replays the bounded baseline and
materializes the latest coalesced run-state capsule, including evidence deferred
while the role was active, before recovering an accepted active assignment. A
local respawn failure, replacement disconnect, broker interruption, or
unprovable assignment remains `uncertain`; it is not blindly replayed.

### `stop SESSION --yes`

Kills only the selected tmux grid. Pi sessions and metadata-only broker state
remain under `~/.pi/agent/orchestrations/`.

### `doctor`

Checks Pi, Python, tmux, tmux extended-key settings, the model configuration
path, and effective configured model availability without a provider request.

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

## Parent Pi supervision

When the package extension starts a run, the invoking Pi itself is the parent
supervisor and opens an authenticated, read-only broker observer. Starting a
normal run creates one detached worker grid; it does not start a second parent
Pi, parent window, or persistent controller. Use `/orchestrator-watch SESSION`
or the model tool's `watch` action to subscribe the invoking Pi to a compatible
existing run without changing the terminal.

Use `/orchestrator-attach SESSION` or the model tool's `attach` action to ensure
that subscription and switch the invoking Pi's existing tmux client into the
live worker grid. Select panes with normal tmux keys and type directly into a
native Pi worker's input editor to steer it. Prefix then `L` detaches from the
grid by returning that exact client to the same invoking Pi; the workers and
observer continue running, so attach/detach can be repeated. Pi exposes no
supported terminal-suspension API for an outside-tmux parent, so seamless
in-place attach deliberately requires the invoking Pi to already run inside
tmux.

Tmux remains the live worker view. The observer does not mirror raw worker logs
into the parent. It shows bounded non-triggering lifecycle and report-received
progress, sends bounded structured role reports when the workflow becomes
`ready`, and sends attention/uncertainty updates when parent intervention is
required. Terminal updates trigger the parent Pi to assess results and decide
follow-up. Metadata-only `status` summaries include the workflow and role states
so completion is legible even before an observer is attached.

Observer report bodies are bounded, held only in broker memory while live, and
stored only in the relevant Pi sessions. They are excluded from SQLite, status,
journals, registries, and Supervisor API output. Terminal-started detached runs remain supported without a parent observer.
Parent observation requires a broker process from `0.6.0` or later; retained
runs hosted by an older broker remain status-readable but cannot be retrofitted
with live report observation.

## Event-driven workflow

1. Every worker bridge connects to the owner-only run socket.
2. The broker adds task/role baseline plus the optional parent context capsule to each Pi session without waking idle roles.
3. It triggers only the implementer and optional initial probe.
4. The implementer submits a bounded `implementation` report with `orchestrator_report`.
5. Enabled specialists inspect the shared worktree and submit typed evidence.
6. Accepted evidence updates one latest-per-role run-state capsule bounded to 16 KiB of UTF-8; recipients no longer accumulate one context body for every historical report. Updates targeting an active role are coalesced until its next assignment.
7. The broker supplies the latest run state to the reviewer and wakes it once.
8. `changes_requested` supplies the coalesced latest run state and starts the next implementation round.
9. Acceptance of each distinct new assignment emits one metadata-only `context_boundary` event. Pi still invokes context projection for every provider request, but only that boundary changes the pruning policy; every assistant/tool turn in the current assignment remains visible across requests.
10. A confirmed restart replays the live in-memory baseline and latest coalesced run state, including a pending replacement deferred during the active assignment, before recovery without creating a second boundary for that assignment.
11. `approved` marks the run ready without waking the implementer for an acknowledgement turn.
12. An attached parent observer returns the latest structured reports to the parent Pi.

The terminating report tool avoids an extra post-report provider turn. Idle
workers end their turn and never sleep or poll. A watching parent also ends its
turn and relies on broker events instead of sleeping or repeatedly polling
status/tmux. Non-terminal progress does not start a turn when the parent is
idle; if the parent is already active, progress is steered in before its next
model step. Timeouts detect failure; they do not schedule workflow transitions.

The deterministic two-round context regression measures serialized
provider-visible message characters before and after a new assignment boundary.
Its current fixture drops from 99,170 to 8,678 characters (91.2%); CI requires
at least a 50% reduction. A separate regression proves multiple assistant/tool
turns accumulate throughout one assignment and remain until the next boundary.
These are stable context-size proxies, not provider-specific token savings or
production-wire acceptance. Cumulative usage and current context occupancy
remain separate Pi/provider metadata.

Report fields, limits, ACLs, acknowledgements, deduplication, retry, crash
semantics, and token accounting are specified in
[protocol-v1.md](protocol-v1.md).

## Durable state

Files remain for:

- mode-`0700` run/session directories;
- mode-`0600` manifests and authentication tokens;
- Pi's complete JSONL sessions;
- one mode-`0600` metadata-only SQLite database;
- a transient startup payload deleted by the broker immediately after reading.

The live broker's rendered role baselines, bounded accepted report evidence, and
run-state capsules needed for confirmed handover replay are ephemeral. Its
copies are lost on broker exit and never enter SQLite, status, dashboards,
registries, journals, or the Supervisor API; an interrupted handover fails
closed as `uncertain`.

New workers never create or poll Markdown reports, readiness markers, mailbox
payload files, or relay-seen files. The database excludes task, assignment,
report, prompt, message, provider, diff, and log bodies.

Retained manifests from `0.4.x` remain compatible with legacy readers and
controls. There is no option to start that protocol in current releases.

## Token accounting and budgets

The worker bridge reports Pi/provider values for:

- input and output tokens;
- cache-read and cache-write tokens;
- reasoning tokens when exposed;
- total cost;
- current context tokens/window/percentage.

Unavailable values remain unavailable; no provider token estimate is invented.
Status and Supervisor API expose cumulative per-role/total usage separately
from each role's current context occupancy. Soft role/run budgets warn before
subsequent work. A budget cannot stop an already-started provider response at an
exact token.

The categories have different meanings and costs:

- input is provider-reported non-cached or otherwise provider-classified input;
- cache read/write records provider-reported cache activity and must not be
  presented as uncached input;
- output is generated assistant output;
- reasoning is separate only when the provider exposes it;
- context occupancy is a current-window measurement, not cumulative usage;
- cost is authoritative only when the provider reports it.

For development comparison, `operational_tokens` means input + output + cache
read + cache write. It is deliberately labeled as an operational aggregate, not
a billing unit or estimate of provider charges.

The source repository includes a checked-in model-free baseline:

```bash
node scripts/token-efficiency-baseline.mjs --check
```

It measures serialized provider-visible characters/UTF-8 bytes by synthetic
provider call and tool-result characters by tool across simple, medium, and
multi-round fixtures. These values are reproducible proxies only. The retained
usage analyzer reads public broker metadata without reading Pi histories or any
workflow/provider body:

```bash
python -m pi_tmux_orchestrator.token_efficiency \
  --state-root ~/.pi/agent/orchestrations --max-runs 100
```

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
`orchestrator_report`. The broker marks the workflow `needs_attention` and an
attached parent Pi receives an event-driven update. Send one focused reminder
or restart the role; the broker does not run an unlimited reminder loop.

### Broker pane exited

Workflow delivery stops. Do not start a legacy relay. Preserve the worktree and
restart or stop/recreate the brokered run after inspecting retained state.

### Project trust prompt

Approve each interactive child only after inspection, use saved/global trust
for RPC presentation, or restart with a separately confirmed `--approve-project`.
