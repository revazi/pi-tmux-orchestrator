# Coordination protocol v1

Pi Tmux Orchestrator `0.5.0` uses one private local broker for every newly
started orchestration. This protocol replaces report files, readiness markers,
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
  role-specific report kind. The broker alone routes work.
- Peer identity: the broker rejects a different local UID where the platform
  exposes peer credentials.

The control-plane SQLite database stores tokens and metadata but never task,
assignment, report, prompt, message, provider response, diff, or log bodies.

## Worker lifecycle

Workers authenticate with `hello`, then report one of:

- `idle`: connected with no provider work running;
- `active`: provider work is running;
- `waiting`: the agent settled with an active assignment but no accepted report;
- `uncertain`: the bridge cannot prove the prior transition.

A socket close records `disconnected`. Retained PIDs never imply liveness.

Every worker receives baseline context once with no model trigger. The broker
then creates assignments according to the workflow. Workers never poll and
must end the turn when there is no active assignment.

## Workflow state machine

1. All enabled bridges connect.
2. Broker delivers baseline context without triggering turns.
3. Broker triggers the implementer and optional initial probe.
4. An implementer `implementation` report triggers each enabled round
   specialist.
5. Specialist evidence is delivered to implementer and reviewer without
   waking them unnecessarily.
6. After all required evidence exists, broker triggers the reviewer once.
7. `changes_requested` delivers one bounded review and triggers the next
   implementer round.
8. `approved` marks the workflow `ready`; it does not wake the implementer just
   to acknowledge approval.

Only the implementer has normal write tools. Other roles are read-only.

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

- Bridge custom entries retain accepted delivery IDs outside LLM context.
- Replayed delivery IDs are acknowledged as duplicates.
- One report is accepted per assignment.
- Operator control command retries deduplicate matching action/role/delivery
  metadata; conflicting reuse is rejected. Supervisor API v2 exposes retained
  command metadata without message bodies.
- A broken connection in an unprovable delivery window becomes `uncertain`.
- Uncertain delivery requires explicit operator retry.
- The protocol does not claim exactly-once delivery.

Report bodies remain durable in the submitting Pi session's tool result, while
delivery context remains in recipient Pi sessions. SQLite retains only report
shape/count/verdict metadata.

## Token accounting

The bridge sums actual provider-reported assistant usage from the Pi session:
input, output, cache read, cache write, optional reasoning, and total cost. It
also reports Pi's current context usage when available. Missing provider data
remains unavailable; the broker does not invent estimates.

Soft role and run token thresholds are metadata warnings. A future hard budget
may prevent a new assignment, but cannot stop an already-started provider
response at an exact token boundary.

## Compatibility

Retained `0.4.x` manifests remain readable and operable through compatibility
code. Every manifest created by `0.5.0` is version `3` with
`coordination: "broker-v1"`; there is no option or fallback that starts the
legacy file coordination protocol.
