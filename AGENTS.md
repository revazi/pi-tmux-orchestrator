# AGENTS.md

## Project

Pi Tmux Orchestrator

## Goal

Provide a small, reusable Pi skill and modular standard-library Python CLI/control plane for launching and coordinating coding agents in tmux across unrelated projects.

## Architecture

- `SKILL.md`: Pi Agent Skill entry point
- `pi_tmux_orchestrator/`: authoritative dependency-free Python orchestration package
- `pi_tmux_orchestrator/supervisor_api.py`: versioned tmux-independent durable-state client boundary
- `pi_tmux_orchestrator/supervisor_commands.py`: thin CLI adapters for the supervisor API
- `bin/pi-tmux-agents`: thin executable launcher
- `references/usage.md`: detailed operator guidance
- `install.sh`: global Pi skill installation
- `tests/`: model-free regressions

## Engineering principles

- Keep the implementation dependency-free and inspectable.
- Prefer one explicit writer and independent read-only reviewers.
- Keep coordination state outside target repositories.
- Keep supervisor reads independent from tmux runtime observation; never infer liveness from retained PIDs.
- Treat project instructions and explicit trust as mandatory boundaries.
- Never access or copy Pi/provider credentials.
- Never put private project payloads in handoff or status output.
- Never claim synthetic probes are production wire acceptance.
- Fail safely rather than replace an existing tmux session.
- Require confirmation flags for role restarts and session termination.

## Working rules

- Start changes from an up-to-date `main` on a focused branch.
- Make the smallest useful change.
- Update README/reference docs when CLI behavior changes.
- Add regression coverage for parser, prompts, state, grid, or relay changes.
- Run `scripts/test.sh` before committing.
- Do not publish packages or make the repository public without explicit approval.
- Do not push secrets, generated Pi sessions, coordination state, or private task content.
- Use squash merges for pull requests.

## Compatibility

- Python 3.11+
- Pi available as `pi`
- tmux 3.2+
- tmux 3.5+ recommended for `extended-keys-format csi-u`

## Non-goals

- Distributed workers
- Cloud orchestration
- Shared credentials
- Autonomous merging or deployment
- Multiple concurrent writers in one worktree
- Provider-specific production API emulation