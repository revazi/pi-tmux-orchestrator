---
name: tmux-agent-orchestrator
description: Starts and coordinates multiple Pi coding agents in a monitorable tmux grid with one writer, an independent reviewer, optional probe, file-based handoffs, model selection, status, messaging, restart, and cleanup commands. Use when the user asks to delegate work across Pi agents, run implementer/reviewer loops, use parallel model probes, or monitor agents in tmux across any project.
compatibility: Requires Pi, Python 3, and tmux. tmux 3.5+ with extended-keys csi-u is recommended.
---

# Tmux Agent Orchestrator

Use the bundled `pi-tmux-agents` command instead of hand-writing tmux panes, prompts, or relay scripts.

## Operating rules

1. Resolve the project directory and read its governing instructions before launch.
2. Treat the user's orchestration request as permission to create external orchestration state, but do not approve an unfamiliar project automatically.
3. Keep one writer: only the implementer may edit project files. Reviewer and probe are read-only roles with verification access.
4. Put the complete task and acceptance criteria in a task file. Do not put credentials, private documents, provider responses, or other secrets in task or handoff files.
5. Use a probe only when the task benefits from independent integration, contract, security, or runtime investigation.
6. Never claim a probe is wire-equivalent to a production provider unless it actually exercises that exact boundary.
7. Do not push, merge, publish, spend provider credits outside the launched Pi sessions, or perform destructive cleanup unless explicitly approved.

## Start a grid

Write the agreed task to a temporary file, then run:

```bash
pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md \
  --approve-project
```

Default roles and models:

- implementer: `openai-codex/gpt-5.6-sol`, `xhigh`
- reviewer: `openai-codex/gpt-5.4`, `high`
- relay/status monitor

Add an independent probe when needed:

```bash
pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md \
  --with-probe \
  --probe-task-file /tmp/pi-agent-probe.md \
  --approve-project
```

Use `--attach` only when the user wants the current terminal switched immediately. Otherwise report the generated session name and let the user attach when ready.

## Operate an existing grid

```bash
pi-tmux-agents list
pi-tmux-agents status SESSION
pi-tmux-agents attach SESSION
pi-tmux-agents send SESSION --role implementer --message "Prioritize the failing regression."
pi-tmux-agents restart SESSION --role implementer \
  --provider openai-codex --model gpt-5.6-sol --thinking xhigh --yes
pi-tmux-agents stop SESSION --yes
```

`restart` preserves filesystem changes and coordination state but starts a fresh Pi conversation for that role.

## Before launching

Run a safe preview when model availability or layout is uncertain:

```bash
pi-tmux-agents doctor
pi-tmux-agents start --project "$PWD" --task-file /tmp/pi-agent-task.md --dry-run
```

See [references/usage.md](references/usage.md) for all options, handoff behavior, security boundaries, and troubleshooting.