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
3. Broker triggers the optional initial probe. The retained
   `implementation_flow` is either `single`, which triggers implementer
   `implementation`, or `phased`, which triggers implementer `plan`.
4. A phased plan report updates and delivers the rolling run-state capsule, then
   creates a distinct same-round implementer `implementation` assignment. An
   implementation report applies fixed per-role activation rules before waking
   configured round specialists. Repair rounds always start directly as
   implementation with latest review evidence.
5. Every accepted report and activation decision replaces recipient evidence with one capsule bounded
   to 16 KiB of UTF-8 containing only the latest accepted report per role;
   recipients are not given an accumulating sequence of historical report bodies.
   If a recipient has an active assignment, replacement is deferred and
   coalesced to the latest capsule until its next assignment boundary.
6. After all activated specialist reports exist and exact deterministic skips
   are recorded, broker triggers the reviewer once. Forced activation can be
   satisfied only by that role's report.
7. `changes_requested` updates the rolling capsule and triggers the next
   implementer round.
8. Budget policy never participates in routing: warning or hard thresholds do
   not suppress, defer, or replace any required assignment.
9. Accepting each new assignment makes one context boundary effective and emits
   one body-free `context_boundary` metadata event. Provider calls within that
   assignment do not emit reset events. The bridge counts those calls from the
   boundary and evaluates configured assignment provider-call/context-pressure
   thresholds before each tool executes.
10. A warning crossing is recorded once and appended as one bounded instruction
    to the next non-report tool result. A hard crossing records a higher-severity
    fact only. Sequential and parallel tools, including `orchestrator_report`,
    remain available and normal workflow routing continues.
11. `approved` marks the workflow `ready`; it does not wake the implementer just
   to acknowledge approval. Budget metadata cannot skip required review or make
   a workflow ready.

Only the implementer has normal write tools. Other roles are read-only. When the
broker creates an implementer `plan` assignment, the shared bridge temporarily
removes `edit` and `write` from that worker's active tools and blocks any other
non-plan tool call. Normal implementer tools are restored only after the plan
assignment terminates. As with every workflow-read-only role, retained `bash`
access is not an OS sandbox; the prompt and protocol still prohibit modification.
The report tool returns `terminate: true`. If the same-role implementation
assignment arrives before the plan response resolves, the bridge recognizes the
new assignment ID and does not clear it while terminating the completed plan
turn. The new assignment boundary therefore prunes inspection assistant/tool
turns without an acknowledgement-only model turn.

## Specialist activation

Configured `probe`, `playwright`, and `django` roles are evaluated by fixed
versioned predicates with no provider request. Initial probe uses the ephemeral
task; round decisions use the validated implementation `changed_paths`. Empty,
malformed, unknown, and potentially high-risk evidence selects `run`. Only clear
documentation-only evidence, plus clearly frontend-only paths for Django, may
select `skipped`. An explicit forced specialist always selects `run`.

Schema-v7 durable rows contain only role, round, `run|skipped`, rule ID, forced
boolean, and timestamp. Events omit task/path bodies. Reviewer run state renders
that metadata with `required`, `reported`, or `not-required`; a configured role
has to have an exact decision, and each `run` decision has to have a same-round
report before review. Probe and browser reports remain local/synthetic evidence,
not production acceptance.

## Parent observer

A run started through the Pi extension may create one or more read-only parent
observers. The starting Pi attaches automatically, and a Pi may explicitly
watch a compatible existing run through the package extension. An observer:

- authenticates with the separate control token and same-user socket boundary;
- sends a strict `observe` hello and sends no frames after authentication;
- receives lifecycle and workflow-state frames plus accepted structured report
  bodies and bounded numeric assignment usage;
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

Implementation, review, and specialist reports use these common bounded fields:

- `kind`
- `summary` (2,000 characters)
- `changed_paths` (implementer implementation only)
- `checks`
- `findings`
- `risks`
- `limitations`
- role-specific `verdict`

Those arrays contain at most 50 entries; individual entries contain at most 500
characters. An implementer `plan` report instead requires exactly:

- `kind: "plan"`
- `summary` (1,000 characters)
- `relevant_paths`
- `relevant_symbols`
- `intended_changes`
- `required_checks`
- `risks`
- `open_questions`

Each plan array contains at most 12 strings of at most 300 characters.
`relevant_paths` must be bounded relative paths. A plan cannot include or claim
`changed_paths`, executed `checks`, `findings`, `limitations`, approval, or any
`verdict`. It is accepted only from the implementer for an active assignment
whose retained kind is also `plan`.

Every total canonical report remains at most 32 KiB. Agents inspect the shared
worktree instead of copying diffs or logs into reports.

Valid report/verdict combinations:

| Role | Kind | Verdict |
|---|---|---|
| implementer | `plan` | none; plan fields only |
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
- One report is accepted per assignment, and its kind must match the retained
  assignment kind. Current bridges attach cumulative and boundary-delta provider
  usage to the report request. The broker validates and
  stores that numeric snapshot in the same transaction as report acceptance,
  before any downstream routing. A duplicate report receives a duplicate
  acknowledgement and cannot replace the first usage result; legacy reports
  without a snapshot remain accepted with assignment usage unavailable.
- Operator control command retries deduplicate matching action/role/delivery
  metadata; conflicting reuse is rejected. Supervisor API v2 exposes retained
  command metadata without message bodies.
- Explicit restart is an authenticated broker control command that advances the
  role generation. The replacement bridge must authenticate with that exact
  generation. Before recovering an accepted active assignment, the broker
  replays the bounded per-role baseline and materializes the latest coalesced
  run-state capsule, including any replacement deferred while that assignment
  was active. It then rotates the assignment delivery ID to trigger confirmed
  recovery without creating another assignment boundary. A recovered plan
  assignment reapplies the plan active-tool restriction before another provider
  turn.
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
Both warn-only and compatibility hard modes are observational. Assignment
`provider_calls`, `context_tokens`, and `context_percent` thresholds are checked
by the worker at tool boundaries. A warning may add one bounded instruction; a
hard threshold records only a higher-severity metadata fact. No threshold blocks
sequential or parallel tools, pauses downstream assignments, or changes review
routing. Direct operator steering remains available, and no budget override is
needed because budget policy never stops work.

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
without starting a new run. Broker SQLite schema v1-v4 migrates through the
metadata-only guardrail schema, schema v5 adds retained
`implementation_flow=single`, and schema v6 migrates to schema v7 with an empty
forced-specialist set and activation table; runs without version-1 budget metadata
retain packaged warn-only behavior. Manifest v3 introduced
`coordination: "broker-v1"`; manifest v4 adds only bounded selected-profile
metadata, and manifest v5 adds the external configuration path/schema plus the
bounded exact-project mapping selection while retaining that protocol. Earlier
manifests report project configuration as unavailable. All remain readable, and there is no
option or fallback that starts the legacy file coordination protocol.
