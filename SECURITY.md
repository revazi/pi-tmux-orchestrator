# Security

## Supported version

The latest commit on `main` is supported while the project remains pre-1.0.

## Reporting

Use a private GitHub Security Advisory or contact the repository owner privately.
Do not include credentials, provider responses, private source documents, or
unrelated session logs.

## Trust and process boundaries

Pi Tmux Orchestrator launches local Pi and Python processes under the current
operating-system account. It uses configured Pi providers without reading or
copying authentication files. It is not an operating-system sandbox between
same-user workers.

Only the implementer receives normal Pi write tools. Reviewer, probe,
Playwright, and Django roles omit `edit` and `write` but retain `bash`; their
read-only rule is a workflow boundary. The broker validates each role's report
kind and disallows changed paths from read-only reports.

Project trust is explicit. Parent trust never transfers automatically to child
Pi processes. `--approve-project` is permitted only after target inspection and
separate confirmation. RPC presentation uses Pi's saved/global trust behavior
because it cannot display a startup trust prompt.

Worker provider/model/thinking policy comes only from bounded explicit
arguments or the strict user-global orchestrator configuration outside target
repositories. The configuration accepts identifiers and thinking levels only;
credential, endpoint, header, and arbitrary extra fields are rejected. Pi's own
model registry and authentication remain authoritative. Bounded model discovery
returns no authentication material.

The separate version-1 custom specialist registry is user-global only and has no
project/CLI/profile/model override. It accepts exact description/rule and
path/SHA-256 resource metadata only—never provider, model, thinking, credential,
endpoint, command, tool, writer, reviewer, or authority fields. IDs cannot
collide with built-in roles/aliases, authority/control identities, commands, or
reserved prefixes. Every custom contract is immutable read-only supplemental
evidence metadata. This version exposes no launch, broker, tmux, worker,
reviewer-satisfaction, profile, or activation path for a custom ID.

Custom registry resources must be canonical current-user-owned regular Markdown
files with no symlink component, no unsafe write/execute mode, bounded sizes,
safe UTF-8, and exact digests; private prompts require no group/world access.
Registry and resources inside the target repository reject before tmux/provider
activity. Bodies are read ephemerally for validation, are never returned in
public/resolved output, and do not enter manifests, SQLite, status, dashboards,
Supervisor API, RPC registries/journals, project files, errors, or logs.

## Broker boundary

Every run created by `0.5.0` or later uses one local Unix-domain socket:

- private run directory mode `0700`;
- socket, database, manifests, and token files mode `0600`;
- independent random 128-bit role tokens plus a separate control token;
- role-scoped report/lifecycle permissions;
- read-only parent observers authenticated with the control token;
- same-user peer credential checking where supported;
- four-byte length-prefixed JSON frames bounded to 256 KiB;
- bounded report fields, arrays, and canonical encoded size;
- no TCP listener, network service, cloud queue, or runtime dependency.

A socket owner running under the same account can inspect or alter local process
state. Permissions and peer checks are hardening, not protection from a
compromised same-user process.

## Private data and persistence

Never place credentials, private customer/career/production data, raw provider
requests/responses, system prompts, private endpoints, unbounded errors, copied
diffs, or logs in task or report fields.

The metadata-only SQLite database retains:

- workflow and role lifecycle, worker generations, and body-free context-boundary events;
- assignment/report IDs and report shape counts;
- verdicts and bounded statuses;
- command/delivery IDs and state;
- provider-reported token/cost totals and context pressure.

It does not retain task, parent-context-capsule, assignment, run-state-capsule,
report, prompt, message, provider, diff, or log bodies. Task and optional bounded
parent context enter through one transient private startup file deleted after
authenticated baseline delivery. Rendered role baselines and run-state capsules
remain only in live broker memory and recipient Pi sessions for confirmed
generation-handover replay. Bounded latest accepted report bodies may exist only
in live broker memory for rolling-state rendering and authenticated parent
observer replay. Report bodies remain in Pi's own protected session tool
results; recipient context and parent completion/attention updates remain in
their complete Pi JSONL sessions. Pi
session files and local backups remain the operator's responsibility.

The package extension transfers start/message input through unique private
temporary files so payloads do not enter subprocess argv, status, details,
notifications, or widgets. Cleanup is not a secure-erasure claim.

State roots, session/run directories, manifests, tokens, and databases must be
canonical non-symlink paths with expected types. Private writes use no-follow
opens where available, descriptor checks, atomic replacement, and restrictive
permissions. Manifest v3 validates exact project/session/window identity,
known roles, pane IDs, session paths, transport presentation, and
`broker-v1` protocol identity.

## Delivery semantics

Broker/bridge acknowledgement proves acceptance, not task completion.

- Delivery and command IDs are 32-character lowercase hexadecimal values.
- Matching action/role/delivery command retries deduplicate.
- Conflicting metadata reuse is rejected.
- Bridge custom entries deduplicate session delivery and record body-free
  assignment boundaries without adding state to LLM context.
- Pi invokes context projection for every provider request, but each distinct
  new assignment emits the only metadata-only boundary event that changes the
  pruning policy; every projection within an assignment retains all current
  turns.
- Confirmed restart advances a broker generation, rejects stale bridges, replays
  the live in-memory baseline and latest coalesced run state (including a
  replacement deferred during the active assignment), and preserves the Pi
  conversation. Replaying the same assignment is not a new pruning boundary.
- A failed local respawn is reported through a body-free authenticated control
  command, and either that failure or a replacement disconnect marks role and
  workflow `uncertain` with metadata-only handover evidence.
- A crash in any other unprovable delivery or handover window becomes `uncertain`.
- Uncertain work is not blindly replayed and requires explicit retry.
- The project makes no exactly-once claim.

Idle agents end their turn. A settled worker with an active assignment but no
report moves the workflow to `needs_attention` for parent intervention. The
broker and bridge use OS event delivery; agents do not poll files, sockets, or
tmux and never use sleeps for lifecycle. Bounded timeouts detect failures
rather than schedule workflow work.

## Token data

Cumulative token and cost totals come from complete Pi/provider assistant
usage. Current provider-context occupancy is reported separately when available.
Missing values are not estimated. Soft budgets can warn or prevent a future
assignment, but cannot stop an already-started provider response at an exact
token boundary.

## Metadata APIs

Status, the broker dashboard, JSON output, journals, widgets, and Supervisor
API return only bounded metadata. The dashboard selects only workflow, role,
assignment-shape, configured model, usage/context, and event metadata; it never
renders task, context-capsule, prompt, message, report, diff, log, credential,
token-secret, or provider-response bodies. Dynamic terminal text is
control-byte sanitized and width-bounded before display. Supervisor API v2 reads retained state without
tmux and reports host runtime as `not_observed`; retained PIDs never imply
liveness. Directory scans and pages remain bounded.

Retained `0.4.x` runs remain readable and operable through compatibility code.
No newly started run can select their file-relay or mailbox payload protocol.

## Destructive operations

The CLI:

- never replaces an existing tmux session;
- uses exact tmux targets;
- validates reserved controller identity before attach/stop;
- permits extension-driven worker-grid attach only as an exact `switch-client` when the parent Pi is already inside tmux; it never injects keys or replaces the parent process;
- requires `--yes` for role restart and grid stop;
- requires `--confirm` for controller stop;
- records ambiguous delivery as `uncertain`;
- retains Pi sessions and metadata after tmux termination;
- kills partial tmux startup and records private failure state where safe.

It does not push, merge, publish, deploy, or clean target repositories.

## Package and tests

In a regular interactive parent Pi session, the extension makes one
best-effort HTTPS request to the public npm registry's package metadata endpoint
to detect a newer release. The request is time-bounded, sends no project or
orchestration data, stores only the validated version in process memory, and is
skipped in worker/controller sessions. Set
`PI_TMUX_ORCHESTRATOR_DISABLE_UPDATE_NOTICE=1` to disable it.

The npm package declares no dependencies or lifecycle scripts and does not
bundle Pi. Deterministic package checks exclude tests, CI files, generated
sessions/state, credentials, caches, `node_modules`, and authentication data.
Model-free acceptance uses isolated Pi/npm homes, offline package operations,
and no provider prompt. `scripts/unreleased-extension-smoke.sh` verifies and
packs the checked-out source, installs the exact tarball into disposable npm/Pi
homes, proves installed-artifact extension/tool/command/skill registration and
RPC provenance, and prints only an explicit offline/update-disabled isolated TUI
command with blackhole proxy settings (not an OS network sandbox); it never
modifies the real Pi home or global package.
