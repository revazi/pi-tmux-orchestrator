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

### Budget policy

The strict version-1 user-global file
`~/.pi/agent/tmux-orchestrator-budgets.json` has this shape:

```json
{
  "version": 1,
  "enforcement": "warn-only",
  "warning": {
    "run": { "operational_tokens": 600000 },
    "role": { "operational_tokens": 200000 },
    "assignment": {}
  },
  "hard": {
    "run": {},
    "role": {},
    "assignment": {}
  }
}
```

`PI_TMUX_ORCHESTRATOR_BUDGET_CONFIG` may select another absolute path outside
the target project. The scopes are run, role, and assignment. Allowed metrics
are `provider_calls`, `input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_write_tokens`, `reasoning_tokens`, `operational_tokens`, `cost_total`,
`context_tokens`, and `context_percent`. Integer thresholds are 1 through
1,000,000,000,000; cost is at most 1,000,000,000; context percentage is at most
100. Values must be positive and finite. Unknown/duplicate fields, credential or
endpoint fields, unsafe files, and warning thresholds above matching hard
thresholds fail closed. `null` disables an inherited file threshold.

Precedence is per-run CLI/model-tool override, user-global file, then packaged
warn-only defaults. CLI starts use `--budget-enforcement warn-only|hard` and
repeatable `--budget-override LEVEL.SCOPE.METRIC=VALUE`; `=off` disables one
inherited threshold. Overrides cannot repeat a threshold and are bounded by the
60 possible level/scope/metric combinations. The Pi tool uses a native
`budgetOverrides` object. Dry-run
and confirmation metadata show the fully effective policy without payloads.
New broker databases retain that numeric/enum policy. Both `warn-only` and the
compatibility `hard` mode are observational: no configured threshold changes
workflow routing, blocks a tool, or interrupts a provider response. Missing
provider values remain unavailable and never produce invented threshold facts.

The worker bridge observes assignment-scope `provider_calls`, `context_tokens`,
and `context_percent` at supported Pi tool boundaries. Provider calls are
counted from the accepted assignment baseline. The first proven warning adds one
bounded instruction to the next non-report tool result. A crossed hard threshold
records a higher-severity fact but leaves every sequential or parallel tool,
including `orchestrator_report`, available. Facts are immutable assignment-local
numeric metadata visible in status, the dashboard (`G~` warning or `G!` hard),
and Supervisor snapshots.

Direct native-TUI steering and authenticated `send` remain normal operator
choices; neither needs to bypass a budget. The operator decides whether to ask
for a report, continue, restart, or stop. There is no live budget-resume command
because thresholds never pause work. A worker restart restores warning/hard
markers from its exact Pi session and cannot silently reset the assignment
provider-call count.

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
- repeatable `--worker-skill ROLE=/absolute/path/SKILL.md`: explicit reviewed per-role skill opt-in
- `--with-probe` and optional `--probe-task[-file]`
- `--with-playwright` and optional `--playwright-task[-file]`
- `--with-django-expert` and optional `--django-task[-file]`
- `--rpc-workers`: headless RPC event panes instead of interactive TUI panes
- `--attach`
- `--dry-run`
- `--skip-model-check`
- `--budget-enforcement warn-only|hard`
- repeatable `--budget-override LEVEL.SCOPE.METRIC=VALUE` (`=off` disables one)

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

Worker skill discovery is disabled by default for both TUI and RPC starts. The
CLI option above and the model tool's `workerSkills` object are the only new-run
skill opt-in surfaces. A selected role must be enabled; each Markdown file must
be a non-symlink readable UTF-8 file no larger than 256 KiB, with at most eight
skills per role. The start preview lists exact paths and private manifest
metadata binds each path to its reviewed SHA-256 digest. Worker launch and
restart fail closed if a selected file disappears or changes. Skills cannot
expand a role's tool allowlist, so read-only roles stay without `edit`/`write`.

Workers replace Pi's general coding prompt plus appended role prompt with one
lean role prompt. The stable common prefix contains active-tool guidance before
role/project-specific authority and safety rules. Pi still appends governing
`AGENTS.md`/`CLAUDE.md` context and any explicitly selected skill. The
model-free built-prompt fixture for Pi 0.84.1 records 5,000 before and 2,479
after normalized reviewer characters (50.4%); these are serialized prompt-size
proxies, not provider tokens, cost, cache efficiency, or production acceptance.

#### Worker result-volume limits

The worker bridge, and not ordinary Pi, applies these fixed orchestration-only
limits in both TUI and RPC modes before a result reaches the next provider call:

| Tool | Input default/cap | Emitted result cap | Retention/guidance |
|---|---|---|---|
| `read` | `limit=400`; explicit larger limits become 400 | 16 KiB and 400 lines, head | next targeted `offset` and `limit<=400` |
| `grep` | `limit=40`; `context<=2` | 16 KiB and 240 lines, head | refine pattern/path/glob; use `read` for exact lines |
| `bash` | unchanged | 24 KiB and 400 lines, bounded head + failure diagnostics + tail | mode-`0600` full-output file or Pi's existing path; inspect a targeted slice |

The byte cap includes the continuation notice. UTF-8 truncation does not split a
multibyte character. The bash diagnostic excerpt recognizes bounded failure,
error, assertion, fatal, panic, and `not ok` lines so a large successful-looking
prefix or tail does not hide safety-critical test evidence; the command ending
is retained as well.

Private Pi session entries record schema version, assignment ID, tool/direction
enums, truncation/input-cap booleans, and numeric source/emitted line and byte
counts. If the immediately following tool is another read page or a refined
grep, a second metadata entry records only that classification. Paths, search
patterns, commands, result bodies, logs, and full-output contents are not copied
into metadata, SQLite, status, the dashboard, or Supervisor output.

The checked synthetic result-volume baseline reports 144,491 before versus
89,733 after serialized UTF-8 provider-context bytes (37.9% reduction), while
read and grep pagination add two provider calls across the three scenarios:

```bash
node scripts/result-volume-baseline.mjs --check
```

This is a deterministic context-size and call-count proxy, not provider tokens,
billing, quality evidence, or production-wire acceptance. Tune a future policy
only with this benchmark plus retained metadata and real provider/quality
evidence; missing provider data remains unavailable.

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
soft-budget warnings, assignment guardrail markers, a bounded metadata-event
rail, and attach/status/stop help. Healthy/success is green, active is cyan,
attention/budget is yellow,
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

Checks Pi, Python, tmux, tmux extended-key settings, model and budget
configuration paths, the effective budget policy, and configured model
availability without a provider request.

### `supervisor ...`

Supervisor API v2 reads retained state without tmux runtime observation:

```bash
pi-tmux-agents --json supervisor capabilities
pi-tmux-agents --json supervisor sessions
pi-tmux-agents --json supervisor runs SESSION
pi-tmux-agents --json supervisor snapshot SESSION --run RUN_ID
pi-tmux-agents --json supervisor usage SESSION --run RUN_ID --limit 100
pi-tmux-agents --json supervisor events SESSION --run RUN_ID \
  --cursor implementer=0 --cursor reviewer=0 --limit 50
```

Host liveness is `not_observed`; retained PIDs do not imply a running process.
`supervisor usage` is a bounded metadata-only page grouped by run, role, round,
and assignment kind. Each role contains separately labeled cumulative usage and
immutable assignment-local deltas. Input, cache read, cache write, output,
optional reasoning, provider-call count, provider-reported cost, operational
tokens, and context pressure remain separate fields. Legacy assignments expose
usage as unavailable rather than estimated. `--limit` selects the latest retained
assignments across the run and the response reports when older results were
truncated.

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
so completion is legible even before an observer is attached. For current
broker runs, status adds one bounded latest-assignment usage line per role, and
the dashboard token cell uses `cumulative/+latest-assignment` without adding a
new payload hierarchy.

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

- provider-call count;
- input and output tokens;
- cache-read and cache-write tokens;
- reasoning tokens when exposed;
- total cost;
- current context tokens/window/percentage;
- peak observed context tokens for each assignment when available.

At each accepted assignment boundary the worker records a numeric cumulative
baseline outside model context. Its terminating report carries current
cumulative usage and the assignment-local delta. Report metadata, cumulative
role usage, and one immutable assignment usage result commit together before
any specialist/reviewer/next-round routing. Duplicate reports cannot overwrite
that result, and restart recovery reuses the recorded baseline rather than
starting from zero.

Unavailable values remain unavailable; no provider token estimate is invented.
Retained reports created before assignment accounting expose usage as unavailable.
Status and Supervisor API expose cumulative per-role/total usage, each role's
latest assignment usage, and current context occupancy without payload bodies.
The packaged policy preserves the existing soft role/run operational-token
warnings. Assignment provider-call and context-pressure thresholds are observed
inside the worker at tool boundaries. Warning and hard levels differ only in
visible severity; neither blocks non-report tools nor downstream assignments.
Budget configuration cannot silently skip required review, mark a workflow
ready, or otherwise alter routing.

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
