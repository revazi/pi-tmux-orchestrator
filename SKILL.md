---
name: tmux-agent-orchestrator
description: Starts and coordinates multiple Pi coding agents in a monitorable tmux grid through an event-driven private broker, with one writer, independent review, optional technical probe, Playwright tester, Django expert, structured reports, token usage, messaging, restart, and cleanup. Use for delegated implementer/reviewer loops or specialist review across projects.
compatibility: Requires Pi, Python 3.11+, and tmux 3.2+. tmux 3.5+ with extended-keys csi-u is recommended.
---

# Pi Tmux Orchestrator

Prefer `/or-start`, `/or-list`, `/or-status`, `/or-watch`, `/or-attach`,
`/or-send`, and `/or-stop` when the package extension is available. The
canonical `/orchestrator-*` names remain equivalent. The bounded
`tmux_orchestrator` tool exposes the same authoritative control plane. New
starts are watched automatically; use its
`watch` action for an existing run so the parent receives lifecycle and final
updates. Pi slash commands with an omitted session list valid running
orchestrations for explicit selection; model-tool calls should continue to use
an exact session whenever multiple runs exist. Use `attach` when the user wants
to enter, navigate, or directly steer the worker panes; it switches the invoking
Pi's existing tmux client into the grid while keeping that Pi and its observer
alive. Prefix then `L` detaches from
the grid by returning to the same invoking Pi without stopping the workers. The
standalone `pi-tmux-agents` CLI fallback is authoritative; do not hand-build
panes, file handoffs, relay scripts, or polling loops.

## Operating rules

1. Resolve the target project and read its governing instructions before launch.
2. Never approve an unfamiliar project. Parent trust does not transfer to child Pi sessions.
3. Keep one writer: only the implementer may edit tracked files. Other roles are workflow-read-only but retain `bash` for verification and are not OS-sandboxed.
4. Keep credentials, private documents, provider bodies, raw errors, diffs, and logs out of tasks and structured reports.
5. Honor explicit user provider/model/thinking requests through `useParentModel` or `modelOverrides`. Use the bounded `models` action to resolve exact available IDs; never invent IDs or inspect credentials. Omitted overrides use the user's global configuration.
6. Use specialists only where their independent evidence is relevant.
6. Never claim a synthetic probe or browser smoke is production wire acceptance.
7. Do not push, merge, publish, deploy, or perform destructive cleanup without explicit authorization.
8. Idle agents end their turn. They never run sleeps or poll files, sockets, or tmux.

## Coordination model

Every new run uses one owner-only Unix-socket broker and the shared Pi worker
bridge. TUI and `--rpc-workers` are presentation choices over the same protocol.
There is no new-run file-coordination mode or fallback.

Workers submit bounded typed results through `orchestrator_report`, which ends
the assignment. Reviewers inspect the shared worktree directly. For a run
started through the package extension, the invoking Pi remains the parent
supervisor; no second parent Pi, parent window, or controller is started. Use
the tmux panes for live visibility, watch lifecycle progress in the invoking Pi,
then interpret the bounded structured completion or attention update returned
by the broker observer. `/orchestrator-watch SESSION` subscribes this Pi to a
compatible existing run without taking over the terminal;
`/orchestrator-attach SESSION` enters its native worker grid, and prefix then
`L` returns while leaving the grid live for later reattachment.
The broker stores metadata-only SQLite state and actual provider token totals
when Pi reports them; it does not persist task, report, prompt, message, diff,
or log bodies. See [references/protocol-v1.md](references/protocol-v1.md).

## Start a grid

Use the extension or a mode-`0600` temporary task file:

```bash
pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md
```

Default roles:

- implementer: user-configured provider/model/thinking, then packaged fallback
- reviewer: user-configured provider/model/thinking, then packaged fallback
- broker/status monitor

Global model policy is read from `~/.pi/agent/tmux-orchestrator.json` (or
`PI_TMUX_ORCHESTRATOR_CONFIG`). Explicit CLI or model-tool role overrides win.
Pi's own model registry and authentication remain authoritative; never place
credentials or endpoint secrets in orchestrator configuration.

Optional roles:

```bash
pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md \
  --with-probe --probe-task-file /tmp/pi-agent-probe.md \
  --with-playwright --playwright-task-file /tmp/pi-agent-playwright.md \
  --with-django-expert --django-task-file /tmp/pi-agent-django.md
```

Use `--rpc-workers` for headless RPC event panes that show assistant progress
plus bounded tool inputs and outputs, but use it only after an explicit request
for headless presentation. Otherwise workers are native interactive Pi TUIs
with Pi's normal highlighting, tool rendering, and input editor. Both use broker delivery; neither uses report files, mailbox payload
files, polling, or tmux key injection for workflow transitions.
Use `--approve-project` only after separately inspecting and trusting the target.

## Operate a grid

```bash
pi-tmux-agents list
pi-tmux-agents status SESSION
pi-tmux-agents attach SESSION
pi-tmux-agents send SESSION --role implementer --message-file /tmp/message.txt
pi-tmux-agents abort SESSION --role implementer
pi-tmux-agents restart SESSION --role implementer --yes
pi-tmux-agents stop SESSION --yes
```

A command acknowledgement proves acceptance, not task completion. Optional
32-character lowercase hexadecimal command IDs provide retry-safe deduplication;
conflicting reuse is rejected and interrupted delivery may be `uncertain`.
Restart and stop require explicit confirmation flags.

Use Supervisor API v2 for retained metadata-only reads after tmux exits:

```bash
pi-tmux-agents --json supervisor snapshot SESSION --run RUN_ID
pi-tmux-agents --json supervisor events SESSION --run RUN_ID \
  --cursor implementer=0 --cursor reviewer=0 --limit 50
```

Retained `0.4.x` runs remain readable/operable, but no newly started run uses
their legacy file protocol.

## Persistent controller

For ongoing cross-project operation:

```bash
pi-tmux-agents controller start
pi-tmux-agents controller attach
```

The optional controller has a fixed project-neutral Pi identity and can be the
parent Pi for cross-project operation. It starts only through an explicit
`controller start`; normal runs never create one. The invoking project Pi
remains the primary interactive parent otherwise. Every target project must be
explicit. Stop the controller only with `controller stop --confirm`; worker grids and
conversations are retained independently.

## Before launching

```bash
pi-tmux-agents doctor
pi-tmux-agents start --project "$PWD" --task-file /tmp/pi-agent-task.md --dry-run
```

See [references/usage.md](references/usage.md) for the full CLI and security
reference.
