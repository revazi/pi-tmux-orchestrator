# Pi Tmux Orchestrator

[![npm version](https://img.shields.io/npm/v/pi-tmux-orchestrator.svg)](https://www.npmjs.com/package/pi-tmux-orchestrator)
[![npm downloads](https://img.shields.io/npm/dm/pi-tmux-orchestrator.svg)](https://www.npmjs.com/package/pi-tmux-orchestrator)
[![CI](https://github.com/revazi/pi-tmux-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/revazi/pi-tmux-orchestrator/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE.md)

A [Pi](https://github.com/earendil-works/pi) package for coordinating coding
agents in a monitorable tmux grid.

- One implementer writes and one independent reviewer is always required.
- Configure each orchestration's models, thinking profile, flow, budgets, skills,
  workspace hints, and optional built-in specialists.
- Native Pi workers remain visible and directly steerable in a grid that adapts
  to the roles enabled for that run.
- An event-driven broker handles structured coordination and recovery.
- Durable orchestration state is bounded and metadata-only.

![Pi Tmux Orchestrator native worker grid and broker dashboard](https://raw.githubusercontent.com/revazi/pi-tmux-orchestrator/main/assets/pi-tmux-orchestrator-demo.png)

## Install

Requirements: Pi, Python 3.11+, tmux 3.2+, and macOS or Linux.

```bash
pi install npm:pi-tmux-orchestrator
```

Run once without installing:

```bash
pi -e npm:pi-tmux-orchestrator
```

Pi packages execute with your user permissions. Inspect packages before
installing them.

## Quick start

Start Pi inside tmux from the project you want to change:

```bash
tmux new -s coding
cd /absolute/path/to/project
pi
```

Then use either:

```text
/or-start Describe the change you want
```

or natural language:

```text
Describe the change you want. Use the orchestrator.
```

Review the preview and confirm. The default run starts one implementer and the
mandatory reviewer. No configuration file is required.

Open `/or-dashboard` to inspect or attach to runs. When attached to the worker
grid, press the tmux prefix followed by `L` to return to the same Pi session.

## Pi commands

The extension intentionally exposes only five commands:

| Command | Purpose |
|---|---|
| `/or-dashboard` | List, inspect, attach/watch, run doctor, or confirm stop |
| `/or-models [query]` | Find exact provider/model IDs |
| `/or-start [task]` | Preview, confirm, and start work |
| `/or-send [session]` | Send private guidance to one role |
| `/or-stop [session]` | Select and confirm stopping a run |

The dashboard is keyboard-driven:

| Key | Action |
|---|---|
| arrows or `j`/`k` | Select a run |
| Enter | Watch future transitions and attach |
| `d` | Run current-project doctor explicitly |
| `r` | Refresh the session list |
| `x` | Request confirmed stop |
| `?` | Show help |
| `q` or Escape | Close |

Opening or refreshing the dashboard never runs doctor and never starts
background polling. Attaching does not replay an already-completed outcome into
the invoking Pi; use explicit watch behavior when that Pi should assess an
existing outcome.

## How it works

Tmux hosts the worker panes but does not transport workflow messages. Each run
has an owner-only Unix-socket broker that authenticates role bridges, accepts
bounded typed reports, and schedules the mandatory review.

The invoking Pi remains the parent supervisor. It receives event-driven
completion or attention updates while each worker keeps its normal durable Pi
session. The broker dashboard refreshes assignment-bound thinking, streaming,
tool, reporting, and finalized-usage metadata directly from worker events; it
does not wait for handoff. Crashes and ambiguous delivery fail to `uncertain`
rather than blindly replaying work.

The package supports interactive native Pi panes and explicit headless RPC
workers through the same broker protocol. New runs use manifest v5 and
`broker-v1`; retained older runs remain readable.

## Configure orchestrations

Every start can choose exact role models and thinking levels, a `single` or
`phased` implementation flow, optional probe/Playwright/Django specialists,
observational budgets, explicitly reviewed worker skills, and the experimental
workspace capsule. Use `/or-start`, natural language, the model tool, or the
terminal CLI; explicit run options take precedence.

Reusable defaults are user-global, never project-local:

```text
~/.pi/agent/tmux-orchestrator.json
```

Packaged profiles change only Pi thinking levels:

- `economy`
- `balanced`
- `thorough` — compatibility default

Profiles do not change models, tools, role authority, mandatory review,
routing, or budget behavior.

Version-3 configuration can apply exact defaults to canonical project paths:

```json
{
  "version": 3,
  "defaultProfile": "balanced",
  "projects": [
    {
      "directory": "/absolute/canonical/path/from/pwd-P",
      "profile": "thorough",
      "implementationFlow": "phased",
      "specialists": ["probe"],
      "workspaceCapsule": false
    }
  ]
}
```

Project directories must already exist and exactly match `pwd -P`; there are no
globs, prefix matches, repository-name matches, or symlink components. Explicit
run options override an exact project mapping.

Pi remains authoritative for provider authentication. The orchestrator does not
read or copy provider credentials. Model policy, custom profiles, specialist
activation, observational budgets, worker skills, and workspace capsules are
documented in the [complete usage reference](references/usage.md).

## Upgrading to 0.9

Version 0.9 removed duplicate long-form commands and separate helper commands:

| Before | Now |
|---|---|
| `/orchestrator-dashboard` | `/or-dashboard` |
| `/orchestrator-models` | `/or-models` |
| `/orchestrator-start` | `/or-start` |
| `/orchestrator-send` | `/or-send` |
| `/orchestrator-stop` | `/or-stop` |
| list/status/help/about/doctor/watch/attach helpers | `/or-dashboard` |
| supervisor/restart helpers | `pi-tmux-agents` or the model tool |

Finish or stop active runs, update, and restart Pi:

```bash
pi update npm:pi-tmux-orchestrator
```

Existing manifest v1-v4 runs remain readable. The mandatory reviewer,
one-writer policy, and `thorough` compatibility profile are unchanged.

See the [v0.9.0 release notes](https://github.com/revazi/pi-tmux-orchestrator/releases/tag/v0.9.0) and
[migration discussion archive](https://github.com/revazi/pi-tmux-orchestrator/issues/82).
If migration is blocked, stop active 0.9 runs and roll back:

```bash
pi remove npm:pi-tmux-orchestrator
pi install npm:pi-tmux-orchestrator@0.8.1
```

## Terminal CLI

The Python CLI provides the complete operational surface:

```bash
pi-tmux-agents list
pi-tmux-agents status SESSION
pi-tmux-agents attach SESSION
pi-tmux-agents send SESSION --role implementer --message-file /tmp/message.txt
pi-tmux-agents restart SESSION --role implementer --yes
pi-tmux-agents stop SESSION --yes
```

Run `pi-tmux-agents --help` for all commands, JSON output, Supervisor API,
controller, profile, model, specialist, and headless-worker options.

## Safety

- The implementer is the only writer; reviewer and specialist roles are
  read-only but are not OS sandboxes.
- Project trust, start, restart, and stop retain explicit confirmation
  boundaries.
- Existing tmux sessions are never replaced and operations use exact targets.
- Workflow, prompt, report, message, diff, log, provider, and credential bodies
  stay out of durable/public orchestration metadata.
- Provider usage and cost are shown only when Pi/provider metadata supplies
  them; synthetic benchmarks are not billing or quality claims.

See [SECURITY.md](SECURITY.md) for the complete security model.

## Documentation

- [Complete operator and CLI usage](references/usage.md)
- [Coordination protocol and state boundaries](references/protocol-v1.md)
- [Dashboard design](references/dashboard-design.md)
- [Pre-release artifact testing](references/prerelease-testing.md)
- [Changelog](CHANGELOG.md)

## Development

```bash
python -m pip install ruff==0.11.11
scripts/test.sh
```

The default test suite is model-free and isolates package, Pi, and npm state
from real authentication.

Created and maintained by [Revaz Zakalashvili](https://github.com/revazi).
Licensed under the [MIT License](LICENSE.md).
