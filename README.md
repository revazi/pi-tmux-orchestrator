# Pi Tmux Orchestrator

[![npm version](https://img.shields.io/npm/v/pi-tmux-orchestrator.svg)](https://www.npmjs.com/package/pi-tmux-orchestrator) [![npm downloads](https://img.shields.io/npm/dm/pi-tmux-orchestrator.svg)](https://www.npmjs.com/package/pi-tmux-orchestrator) [![CI](https://github.com/revazi/pi-tmux-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/revazi/pi-tmux-orchestrator/actions/workflows/ci.yml) [![MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE.md)

Coordinate coding agents in visible tmux panes from [Pi](https://github.com/earendil-works/pi). Each run has one implementer and a mandatory independent reviewer, with structured broker coordination and durable metadata-only state.

## Install

Requirements: Pi, Python 3.11+, tmux 3.2+, macOS or Linux. Worker adapters are verified against Pi 0.87.1.

```sh
pi install npm:pi-tmux-orchestrator
```

Try without installation: `pi -e npm:pi-tmux-orchestrator`. Packages run with your user permissions; inspect before installing.

## Quick start

Start Pi in tmux, from the project to change:

```sh
tmux new -s coding
cd /absolute/path/to/project
pi
```

Then run `/or-start Describe the change` or ask naturally to use the orchestrator. Review the preview and confirm. Defaults need no configuration. Use `/or-dashboard` to inspect runs; when attached, press tmux prefix then `L` to return to Pi.

## Commands

| Command | Use |
|---|---|
| `/or-dashboard` | Inspect, attach/watch, run doctor explicitly, or confirm stop |
| `/or-models [query]` | Find exact provider/model IDs |
| `/or-start [task]` | Preview, confirm, and start |
| `/or-send [session]` | Send private guidance to a run |
| `/or-stop [session]` | Select and confirm stopping a run |

The dashboard supports arrows or `j`/`k`, Enter to attach/watch, `d` for doctor, `r` to refresh, `x` for confirmed stop, `?` for help, and `q`/Escape to close. Opening it does not run doctor or poll in the background.

## Safety and configuration

Tmux hosts panes; an authenticated local broker transports typed workflow reports. Ambiguous delivery fails closed rather than blindly replaying work. Worker prompts and project payloads are not retained in broker metadata. Stop and start actions require confirmation; review authority cannot be removed.

Runs can select exact role models and thinking, single/phased flow, optional built-in specialists, budgets, reviewed skills, and worker-context policy. Model-guided planning is opt-in and separately confirmed. Details and strict user-global configuration examples are in the [usage guide](references/usage.md); protocol and custom specialist details are in [protocol](references/protocol-v1.md) and [custom roles](references/custom-roles.md).

## Upgrade to 0.11.0

Update with `pi update npm:pi-tmux-orchestrator` (or reinstall with `pi install npm:pi-tmux-orchestrator`). Finish or stop active runs before replacing the package. The 0.11.0 release adds opt-in bounded model-guided preflight planning, direct TypeSafe Jev decision support, capability-aware candidate selection, and clearer authoritative worker assignment/progress reporting. Planning makes an additional provider call only when explicitly enabled and confirmed; ordinary starts retain the deterministic path. Existing retained runs remain readable; new ordinary and custom-role manifest formats remain v5 and v7 respectively, over broker-v1.

See the complete [0.11.0 release and migration notes](releases/v0.11.0.md). If upgrade is blocked, remove the package and reinstall the prior exact version, `npm:pi-tmux-orchestrator@0.10.0`, then start Pi again. Do not resume in-flight runs across versions; preserve their original installation until safely finished or stopped. [Rollback guidance](releases/v0.10.0.md#rollback).

## Help and documentation

- [Detailed usage and configuration](references/usage.md)
- [Prerelease testing](references/prerelease-testing.md)
- [Dashboard design](references/dashboard-design.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [Support and bug reports](https://github.com/revazi/pi-tmux-orchestrator/issues)
