# Coordination protocol v1

Pi Tmux Orchestrator `0.5.0` and later use one private local broker for every
newly started orchestration. This protocol replaces report files, readiness markers,
mailbox payload files, relay polling, and tmux key injection as workflow
coordination mechanisms.

## Boundary

- Transport: owner-only Unix-domain stream socket in a per-user private temporary socket directory. The filename is a deterministic hash of the canonical run path, avoiding platform Unix-socket path-length limits.
- Framing: four-byte unsigned big-endian length followed by UTF-8 JSON.
- Maximum frame size: 256 KiB.
- Protocol version: integer `1` in every frame.
- Authentication: independent random 128-bit role tokens and a separate control
  token, stored in mode-`0600` files under a mode-`0700` run directory.
- Authorization: a role may update only its own lifecycle and submit only its
  role-specific report kind. Worker `hello` includes the positive broker-issued
  role generation; stale generations are rejected. The broker alone routes work.
- Peer identity: the broker rejects a different local UID where the platform
  exposes peer credentials.

The control-plane SQLite database stores tokens and metadata, including role
generations, assignment-boundary state, and bounded assignment guardrail facts,
but never task, parent context capsule, assignment, run-state capsule, report,
prompt, message, provider response, diff, or log bodies.
Authenticated parent-observer report bodies are ephemeral in broker memory and
may become durable only in Pi session history.

## Worker lifecycle

Workers authenticate with `hello`, then report one of:

- `idle`: connected with no provider work running;
- `active`: provider work is running;
- `waiting`: the agent settled with an active assignment but no accepted report;
- `uncertain`: the bridge cannot prove the prior transition.

A socket close records `disconnected`. A worker settling with an active
assignment and no report marks the workflow `needs_attention`. Retained PIDs
never imply liveness.

Every worker receives baseline context with no model trigger. Baseline may
include one parent-authored context capsule of at most 12 KiB. The capsule is a
structured recap of task-relevant state and decisions, never an automatic copy
of the parent transcript. It crosses the same private ephemeral startup boundary
as the task, whose file is deleted after baseline delivery. The rendered
per-role baseline remains only in live broker memory and the worker's Pi session
so an explicitly confirmed generation handover can replay it.

The broker then creates assignments according to the workflow. Workers never
poll and must end the turn when there is no active assignment.

## Workflow state machine

1. All enabled bridges connect.
2. Broker delivers baseline context without triggering turns.
3. Broker triggers the implementer and optional initial probe.
4. An implementer `implementation` report updates the rolling run-state capsule
   and triggers each enabled round specialist.
5. Every accepted report replaces recipient evidence with one capsule bounded
   to 16 KiB of UTF-8 containing only the latest accepted report per role;
   recipients are not given an accumulating sequence of historical report bodies.
   If a recipient has an active assignment, replacement is deferred and
   coalesced to the latest capsule until its next assignment boundary.
6. After all required evidence exists, broker triggers the reviewer once.
7. `changes_requested` updates the rolling capsule and triggers the next
   implementer round.
8. Before every downstream assignment, hard mode compares authoritative retained
   run, role, and triggering-assignment usage with non-overridden thresholds.
   A proven limit records bounded facts and moves the workflow to
   `budget_exhausted`; no assignment is created until an authenticated operator
   override is accepted. Missing values cannot trigger a gate.
9. Accepting each new assignment makes one context boundary effective and emits
   one body-free `context_boundary` metadata event. Provider calls within that
   assignment do not emit reset events. The bridge counts those calls from the
   boundary and evaluates configured assignment provider-call/context-pressure
   thresholds before each tool executes.
10. A warning crossing is recorded once and appended as one bounded instruction
    to the next non-report tool result. In explicit hard mode, a hard crossing
    permits `orchestrator_report` but blocks every discovery or mutation tool in
    the assistant batch with `terminate: true`. The provider response already in
    progress is not interrupted. If the worker settles without reporting, the
    existing `needs_attention` transition applies.
11. `approved` marks the workflow `ready`; it does not wake the implementer just
   to acknowledge approval. A budget-stopped path cannot skip required review
   and become ready.

Only the implementer has normal write tools. Other roles are read-only.

## Parent observer

A run started through the Pi extension may create one or more read-only parent
observers. The starting Pi attaches automatically, and a Pi may explicitly
watch a compatible existing run through the package extension. An observer:

- authenticates with the separate control token and same-user socket boundary;
- sends a strict `observe` hello and sends no frames after authentication;
- receives lifecycle and workflow-state frames plus accepted structured report
  bodies, bounded numeric assignment usage, and metadata-only hard-budget facts;
- produces bounded lifecycle/report-received progress in the watching Pi while
  keeping raw assistant/tool output in tmux;
- receives a bounded in-memory replay of up to 100 reports from the current
  broker process before its initial workflow snapshot; the snapshot includes a
  metadata-only report count and replay-completeness flag so loss across a
  broker restart fails closed as uncertain;
- never receives task, assignment, operator-message, provider, diff, or log
  bodies.

Observers are presentation/supervision clients, not workflow writers. Slow or
disconnected observers are dropped without blocking routing. Their report
bodies are not written to SQLite, event journals, status, registries, or the
Supervisor API. A connected parent Pi places bounded completion or attention
updates in its own Pi session and remains responsible for interpreting results
and choosing operator follow-up. Switching the tmux client into native worker panes
does not close the parent observer; the parent Pi keeps running in its original
pane and receives updates for display when the user returns. Client switching
is presentation-only and does not change detached operation or broker workflow
state.

## Structured reports

The worker bridge exposes one terminating tool: `orchestrator_report`.

Common bounded fields:

- `kind`
- `summary` (2,000 characters)
- `changed_paths` (implementer only)
- `checks`
- `findings`
- `risks`
- `limitations`
- role-specific `verdict`

Arrays contain at most 50 entries; individual entries contain at most 500
characters. The total canonical report is at most 32 KiB. Agents inspect the
shared worktree instead of copying diffs or logs into reports.

Valid report/verdict combinations:

| Role | Kind | Verdict |
|---|---|---|
| implementer | `implementation` | none |
| reviewer | `review` | `approved`, `changes_requested` |
| probe | `probe` | none |
| Playwright | `playwright` | `pass`, `fail` |
| Django | `django` | `advisory_approved`, `issues_found` |

The tool returns `terminate: true`; the agent must not emit another response,
sleep, or poll after reporting.

## Delivery and recovery

Delivery IDs, assignment IDs, report IDs, and command IDs are 32-character
lowercase hexadecimal values.

- Bridge custom entries retain accepted delivery IDs and numeric cumulative
  usage at each assignment boundary outside LLM context. They retain no task,
  prompt, report, message, provider, diff, or log body.
- Pi invokes the bridge's context-projection hook for every provider request.
  Every projection within one active assignment retains all of that assignment's
  assistant/tool turns; completed turns leave provider context only when the next
  distinct assignment boundary changes the pruning policy.
- At that boundary, the bridge projects the latest baseline, latest delivered
  run-state capsule, new assignment, direct user/operator messages, and new
  assignment turns. Replaying the same assignment during confirmed handover is
  not a new pruning boundary.
- Direct user messages and non-orchestrator custom messages are not discarded by
  this projection.
- Replayed delivery IDs are acknowledged as duplicates.
- Warning delivery and warning/hard trigger markers are retained as body-free
  custom Pi entries. Restarting the exact worker session restores them and the
  cumulative assignment baseline, so warning delivery and provider-call counts
  do not reset silently.
- A worker `guardrail` frame contains only assignment ID, level, metric,
  observed value, and configured threshold. The broker verifies role ownership
  and the retained effective policy, then immutably stores at most one warning
  and one hard fact per assignment. Matching retries are duplicates.
- One report is accepted per assignment. Current bridges attach cumulative and
  boundary-delta provider usage to the report request. The broker validates and
  stores that numeric snapshot in the same transaction as report acceptance,
  before any downstream routing. A duplicate report receives a duplicate
  acknowledgement and cannot replace the first usage result; legacy reports
  without a snapshot remain accepted with assignment usage unavailable.
- Operator control command retries deduplicate matching action/role/delivery
  metadata; conflicting reuse is rejected. Supervisor API v2 exposes retained
  command metadata without message bodies.
- `budget_override` is a body-free authenticated control command accepted only
  for a live `budget_exhausted` workflow with its pending route still in broker
  memory and all roles connected. It durably marks the exact gate identity
  overridden before resuming. A matching command retry cannot route twice.
- Explicit restart is an authenticated broker control command that advances the
  role generation. The replacement bridge must authenticate with that exact
  generation. Before recovering an accepted active assignment, the broker
  replays the bounded per-role baseline and materializes the latest coalesced
  run-state capsule, including any replacement deferred while that assignment
  was active. It then rotates the assignment delivery ID to trigger confirmed
  recovery without creating another assignment boundary.
- If local worker respawn fails after restart preparation is acknowledged, the
  CLI submits a body-free authenticated `restart_failed` control command. The
  broker authoritatively marks the role and workflow `uncertain` and records a
  metadata-only `worker_handover_uncertain` event.
- A replacement disconnect or broken connection in an unprovable delivery or
  generation-handover window becomes `uncertain`; delivering/uncertain
  assignments are never blindly replayed. Broker restart cannot reconstruct
  private in-memory capsules and
  therefore fails an interrupted handover closed as uncertain.
- Uncertain delivery requires explicit operator retry.
- The protocol does not claim exactly-once delivery.

Report bodies remain durable in the submitting Pi session's tool result, while
delivery context remains in recipient Pi sessions. When a parent observer is
attached, returned structured reports also become part of the parent Pi
session. SQLite retains only report shape/count/verdict metadata and numeric
cumulative/assignment usage; it never stores report or provider bodies.

## Token accounting

The bridge sums actual provider-reported assistant usage across the complete Pi
session: provider-call count, input, output, cache read, cache write, optional
reasoning, and total cost. At assignment acceptance it records a numeric
cumulative baseline outside model context. `orchestrator_report` submits both the
current cumulative snapshot and its delta from that baseline, including current
context occupancy and the peak observed context tokens when available. The
broker commits report metadata, immutable assignment usage, and current
cumulative role usage atomically before routing. Missing provider data
and pre-upgrade assignment usage remain unavailable; the broker does not invent
estimates. Input, cache activity, output, optional reasoning, current/peak
context occupancy, and cost remain distinct categories. Supervisor API v2
advertises a bounded assignment-usage page through capabilities and exposes it
through `supervisor usage`. Results group one retained run by role, round, and
assignment kind while labeling cumulative and assignment-local usage separately.
Legacy results remain unavailable. The development-only `operational_tokens`
aggregate sums input, output, cache read, and cache write for comparison; it is
not a billing unit. Provider-reported cost is the only cost authority.

Every new run retains its effective strict budget policy as numeric/enum broker
metadata. Version 1 supports warning and hard thresholds at run, role, and
assignment scope for provider calls, distinct token categories, provider-reported
cost, and context occupancy. The packaged policy preserves the existing soft
role/run operational-token warnings and defines no hard defaults. Per-run
CLI/model-tool overrides take precedence over the user-global external file.
Warn-only mode remains non-blocking. Assignment `provider_calls`,
`context_tokens`, and `context_percent` warning thresholds are enforced by the
worker at tool boundaries. Explicit hard mode makes matching hard thresholds
terminating for every non-report tool call while leaving `orchestrator_report`
available. Parallel tool preflight therefore yields all-terminating blocked
results, except that a report call may execute and terminate normally. Direct
operator steering can request that report but does not bypass the guardrail;
the live `budget_override` control applies only to a downstream
`budget_exhausted` route. More tool work requires a newly confirmed run with a
higher or disabled assignment threshold.

Hard mode also gates a downstream assignment after the active report and its
actual usage commit atomically. The active report remains accepted. An
assignment-local override identity includes
its assignment ID, while role/run overrides cover that threshold for the
remainder of the live run. Broker interruption while gated or resuming becomes
`uncertain` because the private pending route cannot be reconstructed. No budget can stop
an already-started provider response at an exact token boundary.

A deterministic synthetic two-round regression separately measures serialized
provider-visible message characters across the assignment projection. CI
requires at least a 50% reduction and currently observes 99,170 before versus
8,678 after (91.2%). This serialized-character metric is a reproducible
context-size proxy, not provider-specific token savings or production-wire
acceptance.

## Compatibility

Retained `0.4.x` manifests remain readable and operable through compatibility
code. Live parent observation requires a broker process from `0.6.0` or later;
older live runs remain metadata-readable but cannot gain observer support
without starting a new run. Broker SQLite schema v1-v4 migrates to metadata-only
schema v5 when a current broker starts; runs without version-1 budget metadata
retain packaged warn-only behavior. Every manifest created by `0.5.0` or later is version `3` with
`coordination: "broker-v1"`; there is no option or fallback that starts the
legacy file coordination protocol.
