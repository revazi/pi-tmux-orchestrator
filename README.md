# Pi Tmux Orchestrator

[![npm version](https://img.shields.io/npm/v/pi-tmux-orchestrator.svg)](https://www.npmjs.com/package/pi-tmux-orchestrator)
[![npm downloads](https://img.shields.io/npm/dm/pi-tmux-orchestrator.svg)](https://www.npmjs.com/package/pi-tmux-orchestrator)
[![CI](https://github.com/revazi/pi-tmux-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/revazi/pi-tmux-orchestrator/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE.md)

A [Pi](https://github.com/earendil-works/pi) extension, skill, and
dependency-free Python CLI for coordinating coding agents in monitorable tmux
grids.

## What it provides

- One implementer with normal Pi coding tools
- One independent reviewer with read/verification tools
- Optional technical probe, Playwright tester, and Django expert
- One event-driven owner-only Unix-socket broker per run
- The same worker-bridge protocol for interactive TUI and headless RPC workers
- Bounded typed reports through a terminating Pi tool
- No Markdown handoffs, readiness markers, mailbox payload files, relay polling,
  lifecycle sleeps, or tmux key injection in newly started runs
- Metadata-only SQLite state, durable Pi sessions, idempotent command IDs, and
  crash-`uncertain` semantics
- Actual provider token/cost accounting when Pi exposes it, plus context pressure
  and soft budgets
- A versioned JSON CLI and tmux-independent Supervisor API v2
- A persistent project-neutral controller Pi session
- Explicit project trust, one-writer policy, bounded output, and confirmations
  for restart/stop

## Grid

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
│ Optional Django expert       │ Broker + status              │
│ openai-codex/gpt-5.4         │ workflow, lifecycle, usage   │
│ high                         │                              │
└──────────────────────────────┴──────────────────────────────┘
```

Tmux hosts and displays the broker and workers. It is not the coordination
transport.

## Architecture

`bin/pi-tmux-agents` is a thin executable. The authoritative standard-library
implementation is `pi_tmux_orchestrator/`:

- CLI/controller/tmux host control
- strict manifests and private storage
- framed broker protocol and metadata-only SQLite state machine
- broker clients and TUI/RPC worker supervision
- role system prompts and worker bridge
- retained-run Supervisor API

The extension remains thin and delegates bounded argument arrays to the Python
JSON CLI. The broker is the only current-run metadata writer. Pi owns worker
conversation durability. Reviewer roles inspect the shared worktree directly
instead of receiving copied diffs or logs.

See [coordination protocol v1](references/protocol-v1.md) for schemas, role ACLs,
authentication, lifecycle, report limits, acknowledgements, retry, crash
recovery, and token accounting.

## Requirements

- Pi available as `pi`
- Python 3.11+
- tmux 3.2+; tmux 3.5+ recommended
- Node 22.19+ for package verification
- Ruff 0.11.11 for repository development checks only

Recommended tmux 3.5+ configuration:

```tmux
set -g extended-keys on
set -g extended-keys-format csi-u
```

## Installation

```bash
pi install npm:pi-tmux-orchestrator
```

One run without installation:

```bash
pi -e npm:pi-tmux-orchestrator
```

Reviewed Git commit or local checkout:

```bash
pi install git:github.com/revazi/pi-tmux-orchestrator@<reviewed-full-commit>
pi install /absolute/path/to/pi-tmux-orchestrator
```

Pi packages execute with the current user's permissions. Inspect source before
installation. This package has no runtime dependency tree and is MIT licensed.

If an old installation uses the removed scoped npm identity:

```bash
pi remove npm:@revazi/pi-tmux-orchestrator
pi install npm:pi-tmux-orchestrator
```

## Start from Pi

The package exposes:

- `/orchestrator-help`
- `/orchestrator-doctor`
- `/orchestrator-start [task]`
- `/orchestrator-list`
- `/orchestrator-status [session]`
- `/orchestrator-send [session]`
- `/orchestrator-stop [session]`
- `/orchestrate` and `/orchestrations` compatibility aliases

The `tmux_orchestrator` model tool provides bounded `doctor`, `list`, `status`,
`start`, and `send` actions. Start requires interactive confirmation. Parent
project trust is never inherited by child Pi sessions; child `--approve` needs
separate confirmation.

## Start from the terminal

```bash
cat > /tmp/pi-agent-task.md <<'TASK'
Implement the requested change, add focused tests, run verification, and stop
after independent review approval.
TASK

pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md \
  --attach
```

Add specialists:

```bash
pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md \
  --with-probe --probe-task-file /tmp/pi-agent-probe.md \
  --with-playwright --playwright-task-file /tmp/pi-agent-playwright.md \
  --with-django-expert --django-task-file /tmp/pi-agent-django.md
```

Use `--rpc-workers` for headless RPC event panes. TUI remains the default. Both
presentations use the same broker and bridge; `--rpc-workers` is not a legacy
coordination mode.

Use `--approve-project` only after inspecting and trusting the target project.
RPC workers otherwise apply Pi's saved/global trust behavior and cannot display
startup trust dialogs.

## Event-driven workflow

1. Bridges connect and authenticate independently.
2. Broker stores baseline context in each Pi session without waking idle roles.
3. Only implementer and optional initial probe are triggered.
4. Implementer submits a bounded `implementation` report through
   `orchestrator_report`; the tool terminates the turn.
5. Enabled specialists inspect the worktree and submit typed evidence.
6. Broker supplies all evidence to reviewer and wakes reviewer exactly once.
7. `changes_requested` starts the next implementation round.
8. `approved` marks the workflow ready without an acknowledgement-only model
   turn.

Idle workers end their turns. They do not sleep or poll. A worker settling
without a report becomes `waiting`/needs attention rather than entering an
unlimited reminder loop.

## Manage grids

```bash
pi-tmux-agents list
pi-tmux-agents status SESSION
pi-tmux-agents attach SESSION
pi-tmux-agents send SESSION --role implementer --message-file /tmp/message.txt
pi-tmux-agents send SESSION --role reviewer --delivery follow-up \
  --command-id 0123456789abcdef0123456789abcdef \
  --message-file /tmp/review-message.txt
pi-tmux-agents abort SESSION --role implementer
pi-tmux-agents restart SESSION --role implementer --yes
pi-tmux-agents stop SESSION --yes
```

Acknowledgement means acceptance, not completion. Matching role/action/delivery
command IDs deduplicate; conflicting reuse is rejected. A crash in an
unprovable delivery window becomes `uncertain`; there is no exactly-once claim.

## Supervisor API v2

```bash
pi-tmux-agents --json supervisor capabilities
pi-tmux-agents --json supervisor sessions
pi-tmux-agents --json supervisor runs SESSION
pi-tmux-agents --json supervisor snapshot SESSION --run RUN_ID
pi-tmux-agents --json supervisor events SESSION --run RUN_ID \
  --cursor implementer=0 --cursor reviewer=0 --limit 50
pi-tmux-agents --json supervisor command SESSION --run RUN_ID \
  --role reviewer --command-id COMMAND_ID
```

Retained-state reads do not query tmux and never infer liveness from retained
PIDs. Host runtime is reported as `not_observed`. Snapshot/status include role
lifecycle, workflow round/state, actual provider usage totals when available,
and context pressure without workflow payload bodies.

## Durable state and compatibility

Run state is private and external to target repositories:

```text
~/.pi/agent/orchestrations/<session>/<run>/
```

Files are retained for manifests, authentication, Pi sessions, metadata-only
SQLite, and a transient startup payload deleted after broker ingestion. Newly
started workers never create or poll task/handoff/review/specialist payload
files or readiness markers.

Retained `0.4.x` runs remain readable and operable through compatibility code.
Every `0.5.0` start creates manifest v3 with `coordination: "broker-v1"`; there
is no selectable legacy fallback.

## Token policy

The bridge sums actual Pi/provider-reported input, output, cache-read,
cache-write, optional reasoning tokens, and cost. It exposes current context
usage when available. Missing data remains unavailable; the orchestrator does
not invent estimates.

Structural savings include no waiting turns, no polling, no copied diffs/logs,
one reviewer wake after all evidence, no approval acknowledgement turn, and
terminating report calls. Soft role/run budgets warn before additional work. No
budget can stop an already-started provider response at an exact token.

## Persistent controller

```bash
pi-tmux-agents controller start
pi-tmux-agents controller status
pi-tmux-agents controller attach
pi-tmux-agents controller stop --confirm
```

The controller uses stable Pi session ID
`pi-tmux-orchestrator-controller-v1`, a private project-neutral workspace, and
no `edit`/`write` tools. Every target project must be explicit. Duplicate or
unmarked reserved tmux names are refused.

## Safety model

- One writer; independent read-only reviewers retain `bash` and are not an OS sandbox.
- Exact tmux targets; existing sessions are never replaced.
- Private canonical non-symlink state paths and strict schemas.
- Owner-only local socket with per-role and control authentication.
- Role-specific structured report ACLs and bounded frames/fields.
- Metadata-only status, SQLite, journals, widgets, and Supervisor API.
- No Pi/provider credential access or copying.
- No synthetic-as-production acceptance claims.
- Explicit trust, restart, abort, and stop boundaries.
- Failure ambiguity is `uncertain`, never blind replay.

See [SECURITY.md](SECURITY.md) and [usage reference](references/usage.md).

## JSON CLI

Place `--json` before or after a public command for one schema-v1 envelope:

```json
{"schema_version":"1","command":"status","success":true,"data":{},"error":null}
```

The outer JSON envelope remains version 1; Supervisor API versioning is
independent. Payload bodies are never returned.

## Author and license

Created and maintained by [Revaz Zakalashvili](https://github.com/revazi).
Contact: [revaz.zakalashvili@gmail.com](mailto:revaz.zakalashvili@gmail.com).
Licensed under the [MIT License](LICENSE.md).

## Development

```bash
python -m pip install ruff==0.11.11
scripts/test.sh
```

Checks are model-free and isolate package/Pi/npm state from real authentication.
