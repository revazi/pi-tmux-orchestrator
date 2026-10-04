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

## Model-tool recovery (Unreleased)

The bounded `tmux_orchestrator` tool also supports exact-session `stop` and
exact-role `restart`/`abort`, through the authoritative CLI and broker. Stop and
restart each ask for **separate interactive TUI or RPC confirmation** tied to the
selected run; project trust or a prior start approval cannot authorize them.
Abort requests cancellation, not completion. A restart acknowledgement is not a
completed handover or completed task; duplicate restart retries never respawn
again and report uncertain completion. After inspecting a stuck handover, request
a fresh restart with a **new command ID and separate confirmation**; an uncertain
role state does not block recovery. Abort still requires a connected worker, and
restart still requires the broker's live in-memory baseline and resource checks.

Start may supply an exact `session` (1–128 ASCII letters, digits, `_`, `.`, `-`; `.`/`..` are reserved).
A collision returns metadata-only `data.collision`: exact session, safely
available retained workflow/role states, and valid next actions. Inspect status,
attach in a tmux TUI, separately confirm stop, or explicitly choose another start
session. Ordinary non-orchestrator sessions cannot be stopped through this tool.
Nothing automatically stops, replaces, renames, or reuses existing work.

Controls accept optional exact `run` and 32-character lowercase hexadecimal
`commandId`. Keep both when retrying an uncertain command; stop/restart bind the
confirmed run automatically. A confirmed same-ID stop retry can reconcile an
interrupted receipt: it stops only the original live run, or completes after a
successful observation that the session is absent. Concurrent retries fail
uncertain without waiting. A completed stop receipt can be replayed after
retention without killing a new same-name session; unavailable observations and
replacement sessions never authorize a kill or claim completion. Retained state is never proof
of a live broker. The five slash commands and package version 0.11.1 are unchanged.
See [recovery usage](references/usage.md#model-tool-recovery-controls) and
[compatibility](references/protocol-v1.md#compatibility).

## Safety and configuration

Tmux hosts panes; an authenticated local broker transports typed workflow reports. Ambiguous delivery fails closed rather than blindly replaying work. Worker prompts and project payloads are not retained in broker metadata. Stop and start actions require confirmation; review authority cannot be removed.

Workers may end a blocked assignment with typed `orchestrator_attention`
(`clarification`, `blocked`, `tool_failure`, or `report_failure`). Optional summary
and question are bounded, live-parent UI-only, never stored or replayed. Parent
updates retain only assignment/attempt/settlement metadata. An unreported
settlement gets one private system reminder and at most one extra provider
request; a second settlement becomes actionable `needs_attention`. Reconnect or
restart never renews that allowance. Acknowledgement or a reminder is **not task
completion**; only an accepted final report satisfies the assignment. Send
follow-up only to the waiting role owning it. Explicit resume authority survives
reconnect/restart separately from the consumed automatic allowance. See the [protocol](references/protocol-v1.md#worker-attention-and-report-recovery).


Runs can select exact role models and thinking, single/phased flow, optional built-in specialists, budgets, reviewed skills, and worker-context policy. Model-guided planning is opt-in and separately confirmed. Details and strict user-global configuration examples are in the [usage guide](references/usage.md); protocol and custom specialist details are in [protocol](references/protocol-v1.md) and [custom roles](references/custom-roles.md).

## Upgrade to 0.11.1

Version 0.11.1 requires operator-approved exact worker models for opt-in dynamic
planning; ordinary static starts keep their deterministic path. Finish or stop
active runs and preserve your external configuration before replacing the
package. Choose a migration below, then update with
`pi update npm:pi-tmux-orchestrator` (or reinstall with
`pi install npm:pi-tmux-orchestrator`) and restart Pi. Open `/or-dashboard`, use
`d` for an explicit configuration check, and generate a fresh planning preview
before confirming a new dynamic run. Existing retained runs remain readable;
never resume an in-flight run across versions.

### Task intent: orchestration or a direct parent answer?

Supply operator intent through model-tool `taskIntent`, terminal `--task-intent`,
or the `/or-start` intent selector: `change`, `investigation`, `review`, or
`advisory` (exact lowercase values). Use `change` when repository implementation
needs a writer and independent review. For explanations, investigation, or a
review without edits, the parent can usually answer directly.

This is Stage 1 admission, **not a new advisory workflow or chat router**.
Explicit non-change intent offers direct-parent redirection or cancellation in
Pi; the CLI returns a clear `direct-parent` notice. Neither option creates tmux,
broker, worker, manifest, or worker provider calls. Even `--dynamic-plan --yes`
cannot convert non-change work into implementation; a fresh explicit `change`
request is required. No task is automatically sent back as a new parent turn.

Omitting intent preserves ordinary static change starts without classification.
The one authorized dynamic-planning call also recommends a bounded intent;
that recommendation is previewed, digest-bound, and subordinate to explicit
operator intent. With omitted intent, a non-change recommendation redirects or
cancels rather than launching. With explicit `change`, a conflicting
recommendation remains visible in the final launch confirmation. Launched work
retains one writer and mandatory review; phased inspect/plan is still a precursor
to implementation, not an investigation-only workflow. Only bounded intent
metadata is retained, never task, rationale, or provider bodies.

### Approved worker pools for dynamic planning

Dynamic planning axes are independently selectable with model-tool
`planningScopes: ["topology", "models", "thinking"]`, repeatable terminal
`--planning-scope`, or `/or-start --plan-scopes` (seven-choice TUI form).
For example, `planningScopes: ["topology"]` locks worker identities and thinking
to effective per-run/project/global/profile policy. Omission preserves explicit
`dynamicPlan=true` all-axis behavior; static defaults are unchanged. The one
preflight call and final launch confirmation remain separate. See
[independent scopes](references/usage.md#independent-planning-scopes).

Dynamic starts (`/or-start --plan`, `dynamicPlan=true`, or terminal
`--dynamic-plan`) no longer offer the full available catalog. Before upgrading,
finish active runs; then configure exact operator-approved worker identities in
external `~/.pi/agent/tmux-orchestrator.json` (or the absolute
`PI_TMUX_ORCHESTRATOR_CONFIG` path). Preserve existing fields, change the root
version to **5**, and add
`workerCandidates: {"version":1,"all":[{"provider":"provider-a","model":"exact-worker-a"}]}`.
Optional built-in `roles` pools replace `all` for that role. Pools have 1–32
identities each, at most five role pools and 100 distinct identities total.
Choose exact IDs from `/or-models`, not model-name quality or recency heuristics.

Exact run/project/global role locks override pools; custom roles keep fixed
bindings. **Fully locked alternative:** keep configuration versions 1–4 without
a pool and supply exact authoritative provider/model locks for **every
planner-eligible role**, including optional roles the planner might select;
explicitly disable optional roles you do not lock. Thinking-only settings
and packaged defaults are not exact locks. Otherwise planning fails before a
provider call with configuration guidance;
it never silently falls back to the catalog. Both TypeSafe Jev and Pi fallback
use the same approved scope and canonical capabilities. Confirmation and the
immediate start acknowledgement show pool source/count and exact selected
assignments, not configuration bodies. Ordinary static starts and configuration
versions 1–4 remain supported. Old retained planning records remain readable but
cannot launch a new run: create a fresh preview. See
[approved-pool configuration and migration](references/usage.md#approved-exact-worker-model-pools)
and [provider-free acceptance](references/prerelease-testing.md#automated-provider-free-planning-gate).

### Command map and rollback

The five commands are unchanged from 0.11.0. For older installations:

| Old command | Use in 0.11.1 |
|---|---|
| `/orchestrator-dashboard` | `/or-dashboard` |
| `/orchestrator-models` | `/or-models` |
| `/orchestrator-start` | `/or-start` |
| `/orchestrator-send` | `/or-send` |
| `/orchestrator-stop` | `/or-stop` |
| list/status/help/about/doctor/watch/attach helpers | `/or-dashboard` keyboard actions |
| supervisor/restart helpers | CLI or `tmux_orchestrator` model tool |

See the authoritative [0.11.1 release and migration notes](releases/v0.11.1.md),
the exact [GitHub Release](https://github.com/revazi/pi-tmux-orchestrator/releases/tag/v0.11.1),
and [migration announcement #202](https://github.com/revazi/pi-tmux-orchestrator/issues/202).
If migration is blocked, finish or stop active runs, run
`pi remove npm:pi-tmux-orchestrator`, then
`pi install npm:pi-tmux-orchestrator@0.11.0` and restart Pi. Preserve the original
installation until in-flight runs are safely finished or stopped; do not resume
them across versions. See [rollback steps](releases/v0.11.1.md#rollback) and the
[prior 0.11.0 release notes](releases/v0.11.0.md).

## Help and documentation

- [Detailed usage and configuration](references/usage.md)
- [Prerelease testing](references/prerelease-testing.md)
- [Dashboard design](references/dashboard-design.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [Support and bug reports](https://github.com/revazi/pi-tmux-orchestrator/issues)
