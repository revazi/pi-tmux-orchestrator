# Pi Tmux Orchestrator

A reusable [Pi](https://github.com/badlogic/pi-mono) skill and dependency-free Python CLI for coordinating coding agents in monitorable tmux grids.

It turns the recurring “implementer + reviewer + optional specialist” setup into one command, with durable handoffs and explicit safety boundaries.

## What it provides

- One primary implementer with normal Pi coding tools
- One independent reviewer with read/verification tools
- An optional read-only integration, contract, security, or runtime probe
- A live relay/status pane
- Numbered implementation and review rounds
- Configurable provider, model, and thinking level per role
- Role messaging and model restart commands
- External coordination state that does not pollute project repositories
- Model-free dry runs and functional smoke tests

## Default grid

```text
┌──────────────────────────────┬──────────────────────────────┐
│ Implementer                  │ Reviewer                     │
│ openai-codex/gpt-5.6-sol     │ openai-codex/gpt-5.4         │
│ xhigh                        │ high                         │
├──────────────────────────────┼──────────────────────────────┤
│ Optional probe               │ Relay + status               │
│ openai-codex/gpt-5.4-mini    │ handoffs and health          │
│ high                         │                              │
└──────────────────────────────┴──────────────────────────────┘
```

Without a probe, the grid contains implementer, reviewer, and monitor panes.

## Requirements

- Pi coding agent available as `pi`
- Python 3.11+
- tmux 3.2+; tmux 3.5+ is recommended
- A project already inspected and trusted before using `--approve-project`

Recommended `~/.tmux.conf` for tmux 3.5+:

```tmux
set -g extended-keys on
set -g extended-keys-format csi-u
```

## Installation

Clone this private repository, then install the global skill and CLI:

```bash
git clone https://github.com/revazi/pi-tmux-orchestrator.git
cd pi-tmux-orchestrator
./install.sh
```

This installs:

```text
~/.pi/agent/skills/tmux-agent-orchestrator/
~/.pi/agent/bin/pi-tmux-agents
```

`~/.pi/agent/bin` must be on `PATH`. New Pi sessions discover the skill automatically.

## Start from Pi

In a new Pi session:

```text
/skill:tmux-agent-orchestrator Start an implementer and reviewer for the current task and attach.
```

Ask for a probe when useful:

```text
/skill:tmux-agent-orchestrator Start an implementer, reviewer, and independent API contract probe for this task.
```

The skill writes the agreed task to a file and invokes the CLI rather than rebuilding prompts and tmux panes manually.

## Start from the terminal

Create a task file:

```bash
cat > /tmp/pi-agent-task.md <<'TASK'
Implement the requested change, add focused tests, run project verification, and stop after independent review approval.
TASK
```

Start the default two-agent workflow:

```bash
pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md \
  --approve-project \
  --attach
```

Add a focused probe:

```bash
cat > /tmp/pi-agent-probe.md <<'PROBE'
Independently inspect the provider request and response contract using synthetic data only. Report concrete gaps and limitations.
PROBE

pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md \
  --with-probe \
  --probe-task-file /tmp/pi-agent-probe.md \
  --approve-project \
  --attach
```

## Manage grids

```bash
pi-tmux-agents list
pi-tmux-agents status SESSION
pi-tmux-agents attach SESSION
pi-tmux-agents send SESSION --role implementer --message "Prioritize the failing regression."
pi-tmux-agents restart SESSION --role implementer \
  --provider openai-codex --model gpt-5.6-sol --thinking xhigh --yes
pi-tmux-agents stop SESSION --yes
```

A restart preserves project files and coordination state but starts a fresh Pi conversation for that role.

## Handoff flow

1. The implementer writes `handoff-N.md` and `handoff-N.ready`.
2. The relay notifies the reviewer.
3. The reviewer writes `review-N.md`, beginning with `APPROVED` or `CHANGES_REQUESTED`, and creates `review-N.ready`.
4. The relay notifies the implementer.
5. Requested changes start another numbered round.
6. Approval produces `implementation-ready.md` and stops before push or merge unless explicitly authorized.
7. An optional probe writes `probe.md` and `probe.ready`; the relay informs both main agents.

Coordination records live under:

```text
~/.pi/agent/orchestrations/<session>/<run>/
```

They are created with private permissions and remain outside the target repository.

## Safety model

- Only the implementer receives Pi's normal write tools.
- Reviewer and probe are launched without `edit` and `write`.
- Reviewer and probe retain `bash` for verification, so prompts also prohibit tracked modifications.
- Child sessions read the target project's governing instructions before acting.
- `--approve-project` is explicit because it bypasses child trust prompts.
- The orchestrator never reads or copies Pi authentication files.
- `pi --list-models` validates availability without making a model request.
- Role prompts are attached by file rather than exposed as command-line payloads.
- Handoffs must not contain credentials, private documents, prompts, provider payloads, endpoints, or raw errors.

See [SECURITY.md](SECURITY.md) and [references/usage.md](references/usage.md) for details.

## Development

Run all local checks:

```bash
scripts/test.sh
```

The functional smoke creates a temporary tmux grid with fake sleeping agents, verifies marker relay, and removes the session. It sends no provider request.

## Project status

Initial private release: `0.1.0`.

This repository is not published to PyPI and does not distribute credentials, model access, or provider configuration.