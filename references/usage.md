# Usage reference

## Commands

### `start`

Creates a detached tmux session with an implementer, reviewer, optional probe, and relay/status monitor.

Required:

- `--task TEXT` or `--task-file PATH`

Common options:

- `--project PATH`: defaults to the current directory
- `--session NAME`: defaults to `pi-<project>-agents`
- `--approve-project`: passes Pi's `--approve` flag to child sessions; use only after project trust is established
- `--with-probe`: adds the optional read-only probe pane
- `--probe-task TEXT` or `--probe-task-file PATH`: focused probe instructions
- `--attach`: switches/attaches after startup
- `--dry-run`: validates commands, models, project, and configuration without creating files or panes
- `--skip-model-check`: skips catalog availability validation for custom/dynamic model setups

Role model options follow this pattern:

```text
--implementer-provider PROVIDER
--implementer-model MODEL
--implementer-thinking LEVEL
--reviewer-provider PROVIDER
--reviewer-model MODEL
--reviewer-thinking LEVEL
--probe-provider PROVIDER
--probe-model MODEL
--probe-thinking LEVEL
```

Thinking levels: `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`.

### `list`

Lists sessions created by this skill. Unrelated tmux sessions are ignored.

### `status [SESSION]`

Shows role panes, process state, project path, coordination directory, and handoff files. If the session is omitted, the command uses the current project's conventional session when unambiguous.

### `attach [SESSION]`

Inside tmux, switches the current client. Outside tmux, attaches normally.

### `send SESSION --role ROLE --message TEXT`

Sends a steering message to an agent. Use `--message-file` for longer instructions. The message is submitted immediately; Pi queues it safely if the agent is currently working.

### `restart SESSION --role ROLE ... --yes`

Restarts one role with optional new provider/model/thinking settings. Project files and mailbox state remain; the Pi role conversation is fresh. This is useful after provider denial or a requested model change.

### `stop SESSION --yes`

Kills only the selected orchestration's tmux session. Coordination records remain under `~/.pi/agent/orchestrations/` for audit and diagnosis.

### `doctor`

Checks Pi, Python, tmux, tmux extended-key settings, and default model availability without making provider requests.

## Handoff protocol

The coordination directory is external to the repository and mode `0700`.

1. Implementer writes `handoff-N.md` and `handoff-N.ready`.
2. Relay submits a notification to the reviewer pane.
3. Reviewer writes `review-N.md`; its first line is `APPROVED` or `CHANGES_REQUESTED`, then creates `review-N.ready`.
4. Relay submits the result to the implementer.
5. Requested changes produce another numbered round.
6. Approval causes the implementer to write `implementation-ready.md` and stop before push/merge unless the task and repository workflow explicitly authorize more.
7. Optional probe writes `probe.md` and `probe.ready`; relay informs both agents.

The relay transports file paths and state transitions, not source documents or provider payloads.

## Security boundaries

- Only the implementer receives Pi's normal write tools.
- Reviewer and probe are launched without `edit` or `write`; they retain `bash` for tests, so role prompts also explicitly prohibit tracked modifications.
- The orchestrator never reads or copies Pi authentication files.
- Model validation uses `pi --list-models`, which checks configured availability but sends no model request.
- Avoid task text on the command line when it contains sensitive project information; use `--task-file`.
- Never place credentials, raw career documents, private customer data, prompts, provider responses, or raw provider errors in coordination files.
- `--approve-project` bypasses child-session trust prompts and must be limited to a project already inspected and trusted.

## tmux navigation

```text
Ctrl-b q       show pane numbers
Ctrl-b arrows  move between panes
Ctrl-b z       zoom/unzoom current pane
```

Recommended `~/.tmux.conf` for tmux 3.5+:

```tmux
set -g extended-keys on
set -g extended-keys-format csi-u
```

## Troubleshooting

### Provider error in one role

Use `restart` to select another available model without rebuilding the grid:

```bash
pi-tmux-agents restart SESSION --role implementer \
  --provider openai-codex --model gpt-5.6-sol --thinking xhigh --yes
```

### Existing session name

Inspect it with `status`, stop it explicitly, or pass another `--session` name. The tool never replaces an existing tmux session automatically.

### Relay pane exited

Project agents continue running, but automatic notifications stop. Restarting relay is intentionally conservative; send messages manually with `send`, or stop and create a new orchestration after preserving work.

### Child session asks for project trust

Approve it interactively in each pane, or stop and restart with `--approve-project` after confirming the project is trusted.
