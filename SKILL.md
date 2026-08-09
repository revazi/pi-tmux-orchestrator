---
name: tmux-agent-orchestrator
description: Starts and coordinates multiple Pi coding agents from an optional persistent project-neutral Pi controller into a monitorable tmux grid with interactive TUI or acknowledged RPC workers, one writer, an independent reviewer, optional technical probe, Playwright tester, and Django expert, file-based handoffs, model selection, status, messaging, abort, restart, and cleanup commands. Use when the user asks to delegate work across Pi agents, run implementer/reviewer loops, request specialist Django or browser reviews, or monitor agents in tmux across any project.
compatibility: Requires Pi, Python 3, and tmux. tmux 3.5+ with extended-keys csi-u is recommended.
---

# Tmux Agent Orchestrator

Prefer the package extension's canonical `/orchestrator-help`, `/orchestrator-doctor`, `/orchestrator-start`, `/orchestrator-list`, `/orchestrator-status`, `/orchestrator-send`, and `/orchestrator-stop` commands when available. `/orchestrate` and `/orchestrations` remain start/list aliases, and the `tmux_orchestrator` tool remains available for bounded model-driven actions. All delegate to the bundled Python JSON CLI and preserve its trust, confirmation, metadata-only, and private-file boundaries. For ongoing cross-project management, use `pi-tmux-agents controller start|status|attach|stop` to host those controls in one persistent project-neutral Pi session. Otherwise use the standalone `pi-tmux-agents` CLI fallback instead of hand-writing tmux panes, prompts, or relay scripts.

## Operating rules

1. Resolve the project directory and read its governing instructions before launch.
2. Treat the user's orchestration request as permission to create external orchestration state, but do not approve an unfamiliar project automatically.
3. Keep one writer: only the implementer may edit project files. Reviewer, probe, Playwright tester, and Django expert are workflow-read-only roles; they retain verification access including `bash` and are not OS-sandboxed.
4. Put the complete task and acceptance criteria in a task file. Do not put credentials, private documents, provider responses, or other secrets in task or handoff files.
5. Use a probe only when the task benefits from independent integration, contract, security, or runtime investigation. Add Playwright only when a real local test application and user-visible browser behavior should be exercised. Add the Django expert for ORM, settings, lifecycle, database, migration, security, testing, or operational best-practice review.
6. Never claim a probe is wire-equivalent to a production provider unless it actually exercises that exact boundary. Never treat a browser smoke as complete semantic, security, or adapter evidence.
7. Do not push, merge, publish, spend provider credits outside the launched Pi sessions, or perform destructive cleanup unless explicitly approved.

## Start the persistent controller

When the user wants ongoing management independent of one target repository, use:

```bash
pi-tmux-agents controller start
pi-tmux-agents controller attach
```

The controller has a stable Pi session identity and private neutral workspace outside repositories. It must receive an explicit target project for every start, never inherits trust into that project, and has no `edit`/`write` tools. Do not replace a duplicate or unrelated reserved-name tmux session. Stop only with explicit approval and `pi-tmux-agents controller stop --confirm`; the controller conversation and worker grids are retained independently.

## Start a grid

With the extension, use `/orchestrator-start [task]` or call `tmux_orchestrator` with action `start`; both use private temporary files and confirm project, roles/models, TUI versus RPC worker transport, external state retention, and child trust policy before delegation. Controller-mode starts additionally require the explicit target project.

Choose `--rpc-workers` when the user wants correlated prompt acceptance, steering/follow-up queues, abort, and bounded RPC state instead of interactive child Pi TUIs. RPC mode cannot show startup trust prompts: use a saved Pi trust decision, an intentional global `defaultProjectTrust`, or separately confirmed `--approve-project`; the default `ask`/`never` policy loads context instructions but ignores project-local executable resources. For the standalone fallback, write the agreed task to a mode-`0600` temporary file, then run:

```bash
pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md
```

Default roles and models:

- implementer: `openai-codex/gpt-5.6-sol`, `xhigh`
- reviewer: `openai-codex/gpt-5.4`, `high`
- relay/status monitor

Add an independent technical probe when needed:

```bash
pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md \
  --with-probe \
  --probe-task-file /tmp/pi-agent-probe.md \
  --approve-project
```

Add an independent Playwright tester when needed:

```bash
pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md \
  --with-probe \
  --probe-task-file /tmp/pi-agent-probe.md \
  --with-playwright \
  --playwright-task-file /tmp/pi-agent-playwright.md \
  --approve-project
```

Add an independent Django expert alongside the browser tester:

```bash
pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md \
  --with-probe \
  --with-playwright \
  --playwright-task-file /tmp/pi-agent-playwright.md \
  --with-django-expert \
  --django-task-file /tmp/pi-agent-django.md \
  --approve-project
```

The Playwright and Django roles wait for each implementer handoff and report before reviewer approval. Use `--attach` only when the user wants the current terminal switched immediately. Otherwise report the generated session name and let the user attach when ready.

## Operate an existing grid

Prefer `/orchestrator-list`, `/orchestrator-status [session]`, `/orchestrator-send [session]`, and `/orchestrator-stop [session]`; `/orchestrator-help` summarizes the surface and `/orchestrator-doctor` checks prerequisites. Send obtains its message through Pi's editor and delegates only through a unique private file. Attach, RPC abort, and restart remain CLI-only because attach takes over the terminal, abort is transport-specific, and restart requires explicit confirmation/configuration. Standalone fallback:

```bash
pi-tmux-agents list
pi-tmux-agents status SESSION
pi-tmux-agents attach SESSION
pi-tmux-agents send SESSION --role implementer --message "Prioritize the failing regression."
pi-tmux-agents send SESSION --role reviewer --delivery follow-up \
  --message "Review after the current RPC run settles."
pi-tmux-agents abort SESSION --role implementer
pi-tmux-agents restart SESSION --role implementer \
  --provider openai-codex --model gpt-5.6-sol --thinking xhigh --yes
pi-tmux-agents stop SESSION --yes
```

`restart` preserves filesystem changes and coordination state but starts a fresh Pi conversation for that role. `follow-up` and `abort` require an RPC-worker grid. An RPC acknowledgement proves that Pi accepted or queued the command, not that the requested work completed.

## Before launching

The parent Pi trust decision never automatically applies to children. The extension permits `--approve-project` only for a parent-trusted project after a separate per-run confirmation. Otherwise TUI workers use native trust prompts; RPC workers use saved/global trust policy; the default `ask`/`never` policy safely ignores untrusted project-local executable resources without prompting while still loading context instructions.

Run a safe preview when model availability or layout is uncertain:

```bash
pi-tmux-agents doctor
pi-tmux-agents start --project "$PWD" --task-file /tmp/pi-agent-task.md --dry-run
```

See [references/usage.md](references/usage.md) for all options, handoff behavior, security boundaries, and troubleshooting.