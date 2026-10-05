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
generations, assignment-boundary state, assignment-bound live activity phase
and pulse sequence, and bounded assignment guardrail facts, but never task,
parent context capsule, assignment, run-state capsule, report, prompt, message,
provider response, diff, or log bodies.
Authenticated parent-observer report bodies are ephemeral in broker memory and
may become durable only in Pi session history.

## Configuration boundary

Execution profiles and exact per-project mappings are control-plane policy, not
a new transport or wire-protocol version. The CLI resolves the strict
user-global configuration before starting tmux or the broker. The resulting
workflow still uses protocol version 1, the same role ACLs, the same one-writer
implementer authority, and mandatory reviewer routing.

Profiles contribute only effective per-role Pi thinking levels. Project mappings
may contribute model defaults/role overrides, `single|phased` flow, configured
built-in read-only specialists, and the workspace-capsule default. No mapping can
grant project trust, add a writer, remove review, force a specialist, change
worker tools/authority, or supply prompt or skill bodies. Explicit run options
remain outside the wire protocol and take precedence before the run is created.

Manifest v5 retains bounded provenance so status and Supervisor reads can
explain the resolved policy without copying the configuration body:

- `execution_profile`: selected name, `packaged|custom` kind, and
  `per-run|project|user-global|packaged-default` source;
- `orchestration_config`: canonical external configuration path and schema
  version;
- `project_config`: whether an exact canonical directory matched, selected
  profile/flow/specialist/workspace defaults, booleans indicating model
  defaults, and role names with overrides.

The manifest does not retain the custom-profile map, unmatched project entries,
credentials, endpoints, prompts, skills, or configuration file body. Older
manifest versions keep their compatibility behavior and report these newer
provenance fields as unavailable. Profile/project configuration therefore
changes deterministic startup resolution and bounded manifest metadata, but not
frame shape, authentication, report ACLs, or delivery/recovery semantics.

## Accepted planner evidence (Unreleased)

Planning **v6** extends v5 with required `evidence` **v2** and an explicit
orchestration-wide provider-composition decision, without changing manifest
v10/v11 or launch-assignment authority. Planning v1–v5 validation/read contracts
remain intact. Planning v5 requires evidence v1 and reads with provider-composition
decision authority unavailable; v6 requires evidence v2 (no omitted/downgraded axis). Retained static/legacy reads project `{version: 1, status:
"unavailable"}` as evidence; this read projection is not written into old records.
Supervisor capabilities advertise evidence v2/planning v6, exact snapshots, and summary
collections. Protocol observer frames are unchanged: the parent attaches evidence
from the validated start/status envelope and includes it in final actionable
content alongside (never instead of) immutable worker assignments.

Evidence v2 has exactly `version`, `source`, `catalog`, `eligibility`, `decisions`,
`provider_comparison`, and `provider_composition` (v1 lacks the last field). Source is `typesafe_choice` or `pi_selection`. The catalog
is the exact sorted public provider/model/thinking-level/capability projection
supplied to the planner, with strict capability/status/declared-cost schemas and
no arbitrary fields. Eligibility follows retained candidate-role order, with
sorted unique exact provider/model identities and matching counts. Its canonical
facts and scope digest must reconstruct the retained candidate-set binding.

Each ordered decision has exactly `axis`, `role`, `authority`, `selected`,
`confidence`, `selected_probability`, `options`, `alternatives`. Axes are roster,
model, thinking (in lock-role order), then task_intent, then composition only when
the fully fixed worker plan actually had a suitability question, then the required
v2 `provider_composition` axis. The existing `composition` suitability axis is
not renamed or reinterpreted. Model/thinking
questions for omitted eligible roles are evidence of questions, not assignments.
Authority is fixed or planner. Each option has exactly `id` (identity SHA-256),
`identity`, `probability`; selected/alternatives reference those exact IDs.

Identity forms are `{role,inclusion}` for roster;
`{role,provider,model,thinking:null,facts}` for the independent model axis;
`{role,thinking,models:[{provider,model,facts}]}` for the independent thinking axis
with all exact supporting eligible models; `{intent}` for the recommendation;
`{decision:accept|reject}` for suitability; and
`{provider_composition:single_provider|mixed_provider|no_material_preference,facts}`
for the run-wide composition decision (facts hashes the canonical option facts
below). Fact digests reference exact
catalog entries. Final role/model/thinking tuples must separately match immutable
accepted assignments and locks. There is no joint probability or synthesized
conditional probability.

Direct Choice confidence and every option probability are finite `[0,1]` metadata;
the probability sum must be within `1e-6` of one without renormalization. Selected
confidence is independent of selected probability. Top-k is exactly
`min(3, option_count-1)` nonselected IDs ordered by descending probability and
ascending canonical identity for ties. Canonical identities sort lexically by
role/provider/model, role/thinking, role/include-or-omit, intent, or suitability;
fact hashes and provider catalog position do not decide ties. Options themselves
use that canonical identity order. Fixed/singleton axes and Pi selections have
null confidence/selected_probability/option probabilities and empty alternatives;
Pi selections must never masquerade as Jev one-hot distributions.

`provider_comparison` is exactly `{state:homogeneous|mixed,
rationale:rationale_unavailable}` derived from selected launch providers. It is
explicitly unavailable rationale, never Jev reasoning or a measured/causal
cost/quality/reliability claim.

Evidence-v2 `provider_composition` contains exactly `{feasible,roles}`. `feasible`
is the canonical nonempty subset of `[single_provider,mixed_provider]`. Each
role fact is exactly `{role,inclusion,candidates}` in eligible lock-role order
(excluded roles are absent); inclusion is `true` for mandatory/locked inclusion
or `null` for optional inclusion. Each candidate is exactly
`{provider,model,thinking_levels,facts}`, sorted by exact provider/model identity,
with supported levels filtered by thinking locks and `facts` the exact canonical
catalog-entry digest. Eligibility and model locks restrict candidate identities.
All facts are reconstructed from the validated catalog/eligibility/locks, not
trusted as a planner assertion. No assignment-product enumeration is needed:
single requires a common provider across required roles; with at least two
required workers, mixed is feasible when the union of includable role providers
has more than one member. Optional/custom omission can permit single, while
inclusion can make mixed possible. Questioned but omitted roles do not count
in the final assignment.

When both are feasible the existing one authorized TypeSafe request includes one
`provider_composition` Choice with the three strict keys above. Pi fallback uses
the identical bounded `answers.provider_composition` string, never probabilities.
When composition is unique, there is no composition question; its evidence axis
has one feasible option and fixed authority with all probabilities null. Explicit
locks cannot be overridden. Final single requires exactly one assigned provider,
final mixed at least two; neutral permits either feasible assignment. Malformed,
omitted, extra, or inconsistent answers fail before preview, not only launch.
The provider/model-name-neutral instruction permits only supplied canonical
capabilities, declared catalog cost hints, task/context constraints, role contracts,
and locks; it forbids name-based quality/reliability/recency/latency/billing
inference and any presumption that diversity improves outcomes. No #195
material-support gate, support assertion, rationale body, or extra call is added.

Evidence is at most 224 KiB compact UTF-8 JSON, 100 catalog entries, 13 candidate
roles, 42 decisions, and three top alternatives. Planning-file reads are at most
256 KiB; only planning-v5/v6 manifests admit up to 1 MiB serialized JSON, while
static/legacy manifest limits remain 64 KiB. Full exact status/snapshot evidence
is bounded; collections replace it with `{version,projection:"summary",source,
decision_binding,decision_count,provider_comparison,provider_composition}`.
The summary's `provider_composition` is `{state:"unavailable"}` for v1 evidence;
v2 uses exactly `{state:choice|fixed,selected,authority,confidence,
selected_probability,feasible,alternatives:[{composition,probability}]}`. Confidence
and top alternatives follow the same independent-axis/fixed/Pi rules; these are
not joint confidence or calibrated outcome probabilities. Dashboard shows this audit
summary; exact snapshots/status provide the full evidence.

Fact/catalog and evidence digests use `SHA256(canonical_json({encoding:
"binary64-v1",value:encoded_metadata}))`: every JSON number becomes
`{"$number":"16 lowercase hex digits of IEEE-754 binary64 big-endian"}` (zero
normalizes negative zero); arrays retain order, object keys sort, booleans/strings/
null remain unchanged. This is domain-separated from existing metadata digests
and avoids cross-runtime decimal formatting differences. Option IDs continue to
use existing canonical identity metadata digests (composition ties sort by exact
composition key). Planning-v5/v6 decision metadata
binds the **evidence digest**, roles, scopes, locks, and intent; the accepted start
binding also binds that digest and the candidate-set/policy/input/config bindings.
Malformed, duplicate, oversize, inconsistent, fact-mismatched, non-normalized, or
stale metadata is rejected before launch. Digests are integrity/staleness checks,
not provider signatures. No private bodies, credentials, endpoints, raw responses,
errors, hidden reasoning, report/diff/log text, or arbitrary explanatory prose are
part of this contract. This cannot diagnose the unretained failed #174 response.

## Task-intent admission and compatibility (Unreleased)

Admission is outside broker protocol v1; wire authentication, assignments,
read-only role ACLs, report kinds, and mandatory reviewer routing are unchanged.
Model-tool `taskIntent`, CLI `--task-intent`, and slash selection accept exactly
`change|investigation|review|advisory`. Only effective `change` can launch. Stage 1
non-change requests redirect to the parent or cancel before creating any tmux,
broker, workers, manifest, or worker provider request. They do not fabricate
implementation evidence or define an advisory report/assignment schema. Static
starts make no classifier call. The existing authorized dynamic request includes
one bounded intent recommendation question; no second request is added. Explicit
operator intent wins over that recommendation. Non-change recommendations with
omitted intent also redirect/cancel; explicit change plus conflicting
recommendation requires the normal final coding confirmation, displaying both.

New built-in manifests are v10; custom-role manifests are v11. Both require
`task_intent` v1 and `planning` (null for static, a validated record for dynamic).
Intent has exact fields `version` (integer 1), `operator` (enum or null for
omission), `recommendation` (enum or null), `effective` (enum), and `source`
(`operator|planner|default`). Source/effective must match precedence. A retained
launched manifest must have effective change; manifest and planning intent must
agree. No private task, rationale, provider body, or additional authority is
retained. Supervisor reads expose this metadata as `task_intent`; v1–v9 manifests
remain readable and project intent as null/unavailable, never inferred from
implementation reports.

Planning v4 extends v3 scopes/locks/pool metadata with the exact intent object.
Its decision digest covers roles, scopes, locks, and intent; the accepted start
binding also covers intent, and launch checks the operator field against the
fresh CLI input. Node verifies CLI preview metadata and the bound record before
confirmation, revalidates policy/catalog bindings, and refuses intent changes
after confirmation. Legacy planning v1 remains read-only; v2/v3 admission stays
compatible for omitted intent only, with no intent recommendation evidence.
Explicit intent requires a fresh v4/v5/v6 dynamic preview, not attaching an enum to a
legacy decision. Older package readers fail closed on new manifest versions;
use the matching installed package for controls. There is no hot upgrade of a
live broker and no broker wire-version bump.

## Worker lifecycle

Workers authenticate with `hello`, then report one of:

- `idle`: connected with no provider work running;
- `active`: provider work is running;
- `waiting`: the agent settled with an active assignment but no accepted report;
- `uncertain`: the bridge cannot prove the prior transition.

A socket close records `disconnected`. A worker settling with an active
assignment and no report/attention receives one bounded automatic recovery turn;
explicit attention, interrupted recovery, or the next unreported settlement
marks `needs_attention`. Retained PIDs never imply liveness.

While an assignment is active, the bridge sends throttled, assignment-bound
`progress` frames for real Pi turn, assistant-stream, tool, and report-tool
events. Frames contain only the phase enum, a request and assignment identity,
and optional finalized provider usage. The broker updates the role's live phase
and monotonic pulse sequence immediately and refreshes the dashboard without
waiting for a report or workflow handoff. It clears live activity when the role
settles, disconnects, or changes assignment. Raw messages, thinking, tool input,
tool result, and provider bodies are never included.

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
12. An authenticated operator message to the implementer after `ready` queues
   the latest run state and message without triggering an unassigned turn,
   advances exactly one round, creates an `implementation` assignment, and
   requires normal specialist routing and mandatory review again. Retry with the
   same command ID is idempotent. Other role messages during an active workflow
   retain normal steering semantics. During `needs_attention`, only a waiting
   role that still owns an active assignment accepts a triggered send; other
   targets are rejected without changing workflow state or starting an
   unassigned provider turn. The authenticated send resumes that owner; ordinary lifecycle frames cannot
   reopen a waiting attention boundary.

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
is presentation-only and does not change detached operation, broker workflow
state, or the invoking Pi's project context. Dashboard/model-tool attach treats
an initial pre-existing `ready`, `uncertain`, or `needs_attention` snapshot as
bounded non-triggering progress: it does not replay historical reports as a new
parent task. A later actionable transition still triggers normal supervision.
Explicit `watch` retains existing-outcome supervision semantics.

## Structured reports

The worker bridge exposes terminating `orchestrator_report` and non-completing
`orchestrator_attention` tools. Only an accepted report satisfies an assignment.

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

## Custom specialist contract boundary

Protocol validators accept a bounded custom role map only from their trusted
caller. Peers cannot add a contract field or self-register through broker-v1.
Custom reports normalize using the selected built-in read-only specialist schema,
while retaining the original custom identity on the wire. No custom binding may
map to implementer or reviewer authority. With no explicit map, custom roles are
rejected exactly as before. Manifest v6 added bounded custom definition metadata
and a pinned registry path; manifest v7 adds strict selection/thinking/activation
source metadata and contract-bound deterministic activation. Retained v6 custom
runs keep their original always-run behavior. Reads remain metadata-only with fresh
verification at bootstrap/restart. Existing v1–v6 manifests remain supported;
ordinary starts still write v5. Explicit registered custom selections use production broker
routing in both worker presentations. The parent observer receives selected
identities/contracts only from the authoritative CLI public role projection
(`specialist_contract` for custom roles), never from snapshot/report fields.
Missing legacy metadata keeps validation
built-in-only. Duplicate snapshot roles are rejected, and restart/handover states
remain explicit. Custom observer reports must match the bound read-only contract;
parent report bounds prioritize built-in writer/reviewer evidence. Activation
records are not new worker wire authority: public projections derive their bounded
`per-run-force|deterministic-contract-rule` source from retained metadata
(`legacy-always-run` is limited to retained v6 custom runs), and rule IDs remain
identity-prefixed. The direct broker restart path also
revalidates custom resources before generation mutation.
See [custom role bootstrap and surfaces](custom-roles.md).

## Delivery and recovery

Broker schema 10 retains `worker_context_policy` as version 1 plus explicit
per-role `overrides` (`retain` or `prune`). Omitted roles use default `prune`.
Both worker launchers resolve this metadata before starting Pi; ambient environment
cannot override it. Missing policy in schema 10 fails closed, while older schemas
fall back to pruning. No broker-v1 wire fields, review boundaries, session identity,
or usage counters change. Historical approval is never reused as current approval.

Retained roles also receive bounded investigation-reuse hints inside the existing
run-state body, not new broker-v1 wire fields. Receipts remain in memory only and
compare Git/file metadata at report acceptance and assignment/handover delivery.
Broker-mediated operator guidance invalidates earlier receipts. Missing, changed,
or unobservable inputs require reinspection; unchanged metadata is not content
identity or verification authority. Default-prune runs perform no such observations.

Delivery IDs, assignment IDs, report IDs, and command IDs are 32-character
lowercase hexadecimal values.

- Bridge custom entries retain accepted delivery IDs and numeric cumulative
  usage at each assignment boundary outside LLM context. They retain no task,
  prompt, report, message, provider, diff, or log body.
- Pi invokes the bridge's context-projection hook for every provider request.
  Every projection within one active assignment retains all of that assignment's
  assistant/tool turns. Default `prune` removes completed turns at the next distinct
  assignment boundary; explicit per-role `retain` preserves prior assignments and
  all their assistant/tool exchanges. Both modes replace superseded capsules.
- With default `prune`, the bridge projects the latest baseline, latest delivered
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
  Current TUI/RPC bridges also attach an optional `runtime_identity` sibling
  taken from the Pi process model/thinking configuration, never from model-authored
  report arguments. Exact-key unions remain compatible with usage-only and
  identity-free reports. The broker compares `provider`, `model`, and `thinking`
  against immutable per-generation launch metadata held by the broker and
  fans out `runtime_identity_status` as `matching`, `omitted`, `unavailable`, or
  `conflicting`. The broker also adds a bounded `authoritative_assignment` to its
  ephemeral observer report event. A confirmed restart refreshes that role's
  provider/model/thinking from current manifest metadata before the new
  generation can report, so attached parents stay aligned without reading disk
  at report acceptance. Malformed or
  spoofed extra identity fields are rejected. Workers cannot author either
  broker-derived field. The comparison never mutates public role assignments,
  Supervisor/status/dashboard launch fields, or SQLite metadata. Observer
  snapshots remain `{role,state}` only. Free-form report prose is never parsed
  for identity.
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

A separate CLI-selected version-1 `continuation_policy` may cap additional
implementation rounds. The broker checks the count of distinct implementer
implementation rounds after round 1 in the same transaction that creates an
assignment. At the cap it instead records `pending_repair_round`, emits
`continuation_paused`, and broadcasts the existing `needs_attention` state.
No new worker/observer frame is introduced. Status exposes the metadata-only
policy, count, pending round, and reason. Legacy absence means disabled.
Broker schema 9 adds this enforcement contract; older live brokers reject the
new schema instead of silently ignoring a retained cap. Forward migration from
schema 8 installs a disabled policy without resetting assignment counts.

Authenticated control action `continue` uses the existing control envelope with
role `implementer`, null message/delivery, and an operator-provided command ID.
Only a safely paused run with no active assignments can accept it. It authorizes
one additional repair round, persists the updated cap and command identity, and
marks routing intent before side effects. During this transient intent, observer
snapshots project `active` using the existing wire vocabulary; a broker restart
converts interrupted routing to `uncertain` before serving observers.
Duplicates never extend authorization
or replay work. Interruption leaves uncertain routing/delivery, not approval.
This action is distinct from observational budget settings and cannot satisfy
reviewer acceptance. Its CLI requires explicit `--yes` and `--command-id`.

A deterministic synthetic two-round regression separately measures serialized
provider-visible message characters across the assignment projection. CI
requires at least a 50% reduction and currently observes 99,170 before versus
8,678 after (91.2%). This serialized-character metric is a reproducible
context-size proxy, not provider-specific token savings or production-wire
acceptance.

## Bounded model-tool recovery projection (Unreleased)

No worker/control wire version or durable broker schema changes. Model-tool
`stop`, `restart`, `abort`, optional start `session`, exact control `run`, and
32-lowercase-hex `commandId` adapt authoritative CLI paths. Stop/restart require
separate interactive TUI/RPC approval bound to that exact run; abort needs an
exact enabled role. Fresh controls require matching live tmux hosting. Existing
role tokens and generation/handover checks remain authoritative; retained reads
are not liveness. Matching broker IDs replay acknowledgement only. The CLI skips
respawn for duplicate restart acknowledgement and projects `completion=uncertain`,
`duplicate=true`, `restarted=false`; a successful first tmux respawn projects
`completion=respawned`, never workflow completion. After inspection, a fresh
command ID can recover `restarting`/`recovering`/`uncertain`: restart still
revalidates resources and requires the in-memory baseline, but can advance a
new generation after the old client disconnects. Abort still requires a connected
client. Role state alone is not a recovery refusal. A fresh model-tool restart
always requires separate confirmation; no duplicate automatically respawns.

Stop is local tmux lifecycle, not a new broker action. Optional idempotent stop
requires exact `--run` and stores at most 4096 private body-free receipts under
`stop-receipts/`. Exclusive creation precedes kill; interrupted claims remain
uncertain until explicitly confirmed same-ID reconciliation. A nonblocking
POSIX directory lock serializes claim/read/kill/completion, including different
IDs, and releases on process exit. A single successful tmux observation binds
name, immutable session ID, and exact run before kill; a later same-name
replacement cannot match the kill target. A successful absence observation can
complete an interrupted receipt without another kill. Unavailable observations
never prove absence; a live replacement fails `broker_not_live` without mutation.
Completed receipts replay without inspecting/killing any later same-name session.
Old uncertain/completed receipt shapes remain readable; no migration is required.
`command_id`, `run_id`, `duplicate`, `completion`, and `stop_attempted` are
additive CLI output fields. Validated control-error metadata also carries
`command_status` and `retry=same_command_id|new_command_id|inspect_exact_run`:
unknown delivery is reconciled with the same ID, while a stored uncertain broker
refusal needs a fresh ID after resolving the blocker. Abort's historical `aborted` boolean denotes a
request; new `abort_requested`, `completion=not_observed`, and
`workflow_completed=false` distinguish acknowledgement from termination.
Transport failure before connection is unavailable; loss/timeout after connection
is uncertain. Model recovery errors expose fixed messages/identity metadata,
not raw transport/process/provider errors. No automatic destructive retry occurs.

Schema-v1 start collision errors add `error.code=session_collision` and
`data.collision`: exact session, validated marked-run flag, optional run ID,
retained-state provenance (not liveness), optional workflow state/round/flow,
bounded role identity/state/generation, and valid next-action descriptors.
Optional reads fail closed without emitting raw errors or private bodies. Only
valid orchestration metadata offers status/confirmed stop (attach additionally
requires a broker-capable run); other tmux
sessions offer a different explicit start name. No automatic stop, replacement,
rename, or reuse is permitted.

## Compatibility

The recovery additions keep package version 0.11.1, JSON schema-v1, broker-v1,
and Supervisor API v2. The five slash commands are unchanged. Session names now
share a 1–128-character ASCII letters/digits/`_`/`.`/`-` bound across CLI and tool;
existing generated names fit; `.`/`..` are reserved. Legacy names exceeding it must be managed outside
the bounded surface. Older retained records remain readable; missing workflow
state stays unavailable. Idempotent restart requires manifest v3+ broker hosting;
legacy non-idempotent confirmed CLI restart remains available. No retained read
can authorize a new live operation; preserve in-flight installations rather than
assuming hot-upgrade compatibility.

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

## Worker attention and report recovery

The additive broker-v1 worker frames use the existing authenticated base keys
`version,type,role,token,id` and strict exact-key validation:

- `attention`: adds `assignment_id` and `attention`, a strict object requiring
  `reason` from `clarification|blocked|tool_failure|report_failure`; optional
  `summary`/`question` are nonempty strings of at most 500 Unicode code points,
  excluding C0/C1 controls and unpaired surrogates. No other fields are accepted.
- `rejected_report`: adds `assignment_id`. Includes failures observed by
  Pi's report `tool_result` schema/execute path and broker validation failures;
  never includes raw arguments, errors, or provider text. The bridge pins schema
  results to the emitting assistant's assignment/epoch; a late result cannot
  classify a newer assignment as rejected.
- `settlement`: adds `assignment_id`. The bridge journals a stable opaque
  request ID before transport; duplicate settlement and reconnect retry do not
  increment the counter again or schedule another nudge.
- `recovery_turn`: adds `assignment_id`. Before each recovery provider
  request the bridge claims the broker's durable one-use allowance. A repeated
  claim, retry, stale generation/assignment, or disconnected claim aborts before
  another request. This bounds provider requests, not merely agent runs.
- New `lifecycle` frames add `assignment_id` (null when unassigned). Legacy
  lifecycle shape remains readable, but cannot grant an automatic recovery turn.

These four signals also carry nullable `resume_id`, the body-free identity of
the latest explicit operator guidance. Omitted/null is accepted only before any
such guidance; a stale epoch is rejected without changing assignment state.
`report_reminder` carries the same epoch, and assignment recovery synchronizes
it even for duplicate deliveries. The worker journals it and binds settlement to
the run's starting assignment and epoch, so late pre-guidance settlement cannot
pause a resumed turn.

SQLite schema 12 retains per-assignment enums `report_attempt` (`none`, `rejected`,
`attention`, `accepted`), `reminder_state` (`none`, `reserved`, `used`), optional
bounded `attention_reason`/last phase, saturated `settlement_count` (0–2), and
last settlement identity, plus private nullable `resume_id` and boolean
`resume_authorized`. These two resume fields are not public projections and carry
no prose. Schema-11 migration defaults to no explicit authorization, never infers
it from old attention/reminder metadata. Legacy completed reports migrate to `accepted`;
legacy active assignments conservatively consume their recovery allowance.
Accepted reports retain the UNIQUE assignment constraint. Stale delivery acks
cannot resurrect a completed or replaced assignment.

First settlement without report/attention atomically reserves the reminder
before sending strict `report_reminder` with `version,type,id,assignment_id,resume_id`
(the two IDs equal the assignment ID). There is no private body in that frame:
the shared TUI/RPC bridge injects fixed system guidance for the one recovery run.
A second settlement becomes waiting/`needs_attention`. Explicit attention skips
the reminder and becomes actionable immediately. Scheduling/claim uncertainty
or reconnect/restart after reservation becomes actionable and consumes any
remaining allowance. A metadata-only `assignment_closed` frame (`version,type,assignment_id,report_id`)
reconciles an accepted report whose response was lost; the bridge journals only
the acceptance identity, ignores mismatched assignments, and never synthesizes
or resubmits a report. A recovered attention assignment is delivered with `trigger=false`
and `recovery_waiting=true`; no new unsolicited turn is granted. An explicitly
confirmed operator send is assignment-bound and may resume only its waiting
owner without replenishing the automatic allowance. Its durable explicit
authorization makes subsequent reconnect/handover use `recovery_waiting=false`,
not the exhausted automatic provider gate. A new attention or settlement clears
that authorization; a new explicit send changes the epoch. Duplicate/stale
pre-guidance signals cannot clear newer authorization or replay old prose.

Live observer `assignment_state` frames contain exactly
`version,type,session,role,state,assignment`. `assignment` is null or metadata
with `assignment_id,assignment_kind,settlement_count,report_attempt,
attention_reason,reminder_state,activity_phase`. Snapshot roles add the same
metadata as optional `assignment` for the active or latest completed assignment. Live `attention` frames contain exactly
`version,type,session,role,assignment_id,attention`. Their prose bypasses replay
buffers and all durable/public projections. The parent uses only an ephemeral
TUI/RPC UI notification, never `sendMessage`, appendEntry, model tool results,
or provider context. Worker `message_end` captures attention in memory and
returns Pi's `{ message: replacement }` contract to replace its arguments and
accompanying text/thinking in agent state before Pi persistence;
error results are replaced with fixed guidance. Reconnection replays only
metadata, not prose. No parent connected means no prose delivery.

Acknowledgement or an automatic report-recovery nudge is not task completion.
No report is synthesized; only exact-once accepted final reports route workflow
or fulfill specialist/reviewer evidence. These model-free contracts and probes
do not establish production provider-wire acceptance.
