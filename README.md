# Pi Tmux Orchestrator

A reusable [Pi](https://github.com/badlogic/pi-mono) skill and dependency-free Python CLI for coordinating coding agents in monitorable tmux grids.

It turns the recurring “implementer + reviewer + optional specialist” setup into one command, with durable handoffs and explicit safety boundaries.

## What it provides

- One primary implementer with normal Pi coding tools
- One independent reviewer with read/verification tools
- An optional read-only integration, contract, security, or runtime probe
- An optional read-only Playwright tester for real local browser behavior
- An optional read-only senior Django expert for framework-specific review
- A live relay/status pane that routes all specialist reports
- Numbered implementation, specialist, and review rounds
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
│ Optional probe               │ Optional Playwright tester   │
│ openai-codex/gpt-5.4-mini    │ openai-codex/gpt-5.4         │
│ high                         │ high                         │
├──────────────────────────────┼──────────────────────────────┤
│ Optional Django expert       │ Relay + status               │
│ openai-codex/gpt-5.4         │ handoffs and health          │
│ high                         │                              │
└──────────────────────────────┴──────────────────────────────┘
```

The default grid has implementer, reviewer, and monitor panes. Optional roles add panes, and tmux tiles the resulting grid.

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

Ask for Playwright or Django review only when the task benefits from those specialists:

```text
/skill:tmux-agent-orchestrator Add a Playwright tester and Django expert to the implementer/reviewer workflow for this task.
```

The skill writes the agreed task to files and invokes the CLI rather than rebuilding prompts and tmux panes manually.

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

Add browser and Django specialists with focused task files:

```bash
pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md \
  --with-playwright \
  --playwright-task-file /tmp/pi-agent-playwright.md \
  --with-django-expert \
  --django-task-file /tmp/pi-agent-django.md \
  --approve-project \
  --attach
```

Each role supports matching `--ROLE-provider`, `--ROLE-model`, and `--ROLE-thinking` overrides. Playwright and Django are also valid roles for `send` and `restart`.

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
2. The relay notifies the reviewer and enabled Playwright/Django specialists.
3. Playwright writes `playwright-N.md` (`PASS` or `FAIL`) and `playwright-N.ready`.
4. Django writes `django-review-N.md` (`ADVISORY_APPROVED` or `ISSUES_FOUND`) and `django-review-N.ready`.
5. The reviewer waits for enabled specialist reports, then writes `review-N.md` (`APPROVED` or `CHANGES_REQUESTED`) and `review-N.ready`.
6. The relay routes specialist reports to implementer/reviewer and the review to the implementer.
7. Requested changes start another numbered round with fresh specialist reports.
8. Approval produces `implementation-ready.md` and stops before push or merge unless explicitly authorized.
9. An optional probe writes `probe.md` and `probe.ready`; the relay informs implementer and reviewer.

Coordination records live under:

```text
~/.pi/agent/orchestrations/<session>/<run>/
```

They are created with private permissions and remain outside the target repository.

## Safety model

- Only the implementer receives Pi's normal write tools.
- Reviewer, probe, Playwright tester, and Django expert are launched without `edit` and `write`.
- Read-only roles retain `bash` for verification, so prompts also prohibit tracked modifications.
- Playwright artifacts and test data are restricted to ignored or external temporary paths, with bounded process cleanup.
- Child sessions read the target project's governing instructions before acting.
- `--approve-project` is explicit because it bypasses child trust prompts.
- Existing tmux sessions are never replaced; existence checks target exact names so prefix collisions do not block a distinct session.
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

The functional smoke creates a temporary six-pane tmux grid with five fake sleeping agents plus monitor, verifies exact session-name handling and every relay marker, and removes the sessions. It sends no provider request.

## Project status

Current source-controlled baseline: `0.3.0`, reconciled from the installed local skill. This is source recovery, not a package or public release.

This repository is not published to npm or PyPI and does not distribute credentials, model access, or provider configuration.