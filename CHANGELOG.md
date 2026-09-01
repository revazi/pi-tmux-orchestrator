# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added

### Changed

## 0.9.2 - 2026-09-01

### Changed

- Removed the package-wide `Ctrl+Shift+G` dashboard shortcut, which conflicted with Pi's built-in `tui.altScreen.searchPrevious` binding; use `/or-dashboard` to open the overlay

## 0.9.1 - 2026-09-01

### Changed

- Reduced the README to a concise installation, quick-start, migration, safety, and documentation guide while keeping complete operational detail in the packaged references

## 0.9.0 - 2026-08-31

### Breaking

- Removed duplicate `/orchestrator-*` names and separate list/status/help/about/doctor/watch/attach slash commands; migrate to `/or-dashboard`, `/or-models`, `/or-start`, `/or-send`, and `/or-stop`, use the dashboard's keyboard actions for inspection and explicit doctor, and use the CLI or model tool for supervisor/restart operations

### Added

- Added a Pi-native running-orchestration dashboard overlay with compact provider usage, explicit on-demand current-project doctor, concise help, a Tasklight-style About footer, keyboard attach/watch and confirmed stop, bounded session-only refresh, and a non-TUI fallback
- Added strict version-3 user-global per-project orchestration mappings for exact canonical directories, with project profile/model/flow/specialist/workspace defaults, explicit-run precedence, manifest-v5 provenance, and bounded doctor/start/status/Supervisor visibility
- Added persistent local pre-release package staging with commit/tree/tarball provenance, isolated provider-free Pi discovery, a disposable staged-package TUI runner, and a manual acceptance/rollback guide that keeps real-auth provider testing explicit without publishing or replacing the installed package
- Added an opt-in ephemeral cold-assignment workspace-capsule experiment with strict canonical Git/instruction identity validation, bounded allowlisted discovery hints, shared TUI/RPC delivery, fail-closed replay, no cross-run persistence, and a checked model-free proxy benchmark that makes no provider savings or correctness claim
- Added checked-in simple, medium, and multi-round model-free token-efficiency fixtures that measure provider-visible context growth, assignment-boundary reduction, provider-call count, and tool-result volume
- Added a bounded metadata-only retained usage analyzer that aggregates public broker token categories and provider-reported cost without reading Pi histories or workflow/provider bodies
- Added atomic per-assignment provider-call, token-category, cost, and context-occupancy usage results captured at report time before downstream routing, with immutable duplicate handling and restart-safe numeric boundaries
- Added bounded Supervisor API/CLI assignment-usage analytics grouped by run, role, round, and assignment kind, plus compact cumulative/latest-delta indicators in status and the broker dashboard
- Added a strict versioned user-global provider-usage budget policy with warning/hard run, role, and assignment thresholds, explicit CLI/model-tool per-run overrides, dry-run confirmation metadata, and retained numeric policy state
- Added restart-safe in-assignment provider-call and context-pressure visibility that warns once, records higher-severity threshold facts, leaves every tool and workflow route available, and retains only bounded assignment-local metadata
- Added explicit per-role worker skill opt-in with strict Markdown bounds, private digest-bound manifest metadata, confirmation paths, and restart-time change detection
- Added a model-free actual-Pi built-prompt fixture covering skill discovery, explicit skills, governing context files, active read-only tools, and normalized serialized prompt size
- Added orchestration-only read, grep, and bash input/result caps with UTF-8-safe continuation guidance, preserved bash failure diagnostics, mode-`0600` full-output paths, and metadata-only truncation/immediate-follow-up observations
- Added a checked synthetic result-volume benchmark covering context reduction and additional read/grep pagination calls without making provider token, cost, or quality claims
- Added deterministic immutable `economy`, `balanced`, and `thorough` thinking profiles, strict user-global custom profile mappings, explicit per-run selection and override precedence, and bounded manifest/status/Supervisor metadata
- Added a bounded implementer `plan` report contract with assignment-kind matching, temporary read-only active tools, restart-safe policy restoration, rolling run-state projection, and metadata-only durable acceptance
- Added explicit `single` and `phased` implementation flows; phased complex work crosses from read-only inspect/plan into a distinct same-round implementation assignment while repair rounds retain the direct implementation path
- Added conservative versioned probe, Playwright, and Django activation gates with explicit force overrides, reviewer-visible bounded decision evidence, and metadata-only schema-v7 retention

### Changed

- Changed the Pi dashboard to never run doctor on open or session refresh; doctor is now an explicit `d` action, while the About footer loads version, repository, issues, npm, contribution, and cached startup update metadata locally
- Reduced doctor/start model-catalog subprocesses to one per distinct provider within each CLI operation
- Changed broker shutdown to handle tmux pane hangup signals gracefully so private Unix sockets are removed when a grid stops
- Changed all provider-usage budget thresholds to observational metadata: hard-configured runs no longer block tools or downstream assignments and no budget-resume control is exposed
- Changed TUI and RPC workers to disable automatic skill discovery and use one lean custom role prompt while preserving governing `AGENTS.md`/`CLAUDE.md` discovery, role authority, tool guidance, and final reporting
- Preserved the pre-profile xhigh/high packaged thinking behavior as the `thorough` compatibility default until fixed-benchmark provider usage and quality evidence supports a different default

### Fixed

- Prevented dashboard/model-tool attach from replaying an existing initial ready, uncertain, or needs-attention outcome as a new task in the invoking Pi after tmux prefix then `L`; future transitions remain supervised, explicit watch retains existing-outcome supervision, and returning does not change the Pi's project context

## 0.8.1 - 2026-08-20

### Added

- Added one metadata-only context-boundary event per distinct new assignment and a regression proving multiple assistant/tool turns remain provider-visible throughout the current assignment
- Added broker-generation restart handover that replays the bounded in-memory baseline and latest coalesced run-state capsule, including evidence deferred during the active assignment, before recovery

### Changed

- Exclude completed assignment assistant/tool turns only at the next assignment boundary while preserving current-assignment turns, complete Pi JSONL history, latest structured run state, direct user/operator messages, cumulative usage, and current context occupancy
- Defer and coalesce rolling run-state replacement for roles with active assignments, delivering the latest capsule at their next assignment boundary

### Fixed

- Fail a same-broker replacement disconnect closed to metadata-only role/workflow `uncertain` while preserving the expected old-generation disconnect during restart preparation
- Fail a broker-prepared handover closed through authenticated broker control when local tmux respawn fails, rather than leaving the role in `restarting`
- Clarify that Pi invokes context projection for every provider request while the pruning policy changes only at distinct assignment boundaries, and that restart reopens the exact Pi session and JSONL history

## 0.8.0 - 2026-08-19

### Added

- Added an optional 12 KiB structured parent context capsule for task-relevant state, decisions, constraints, acceptance criteria, paths, evidence, open questions, and out-of-scope items without copying the parent transcript or making another model request
- Added one 16 KiB UTF-8-bounded rolling run-state capsule that replaces historical per-report evidence with the latest accepted report per role
- Added a deterministic two-round context-efficiency regression that currently reduces serialized provider-visible message characters from 99,170 to 8,678 (91.2%) and enforces at least a 50% reduction without claiming provider-specific token savings

### Changed

- Exclude completed assignment assistant/tool turns from future provider context while preserving the complete Pi worker session history, latest structured run state, direct user messages, and isolation boundaries

## 0.7.1 - 2026-08-19

### Fixed

- Deliver parent observer updates with steer semantics so an active parent sees lifecycle or terminal broker events before it can continue a sleep/status polling loop, while non-terminal progress remains non-triggering when idle
- Explicitly require watching parents to end their turn and rely on observer events instead of sleeping or repeatedly polling status/tmux

## 0.7.0 - 2026-08-19

### Added

- Added an event-driven broker/status terminal dashboard with explicit state/color semantics, adaptive full/compact/narrow layouts, per-role configured model and actual usage/context data, soft-budget warnings, bounded metadata events, and operator guidance
- Added deterministic rendering, sanitization, plain/`NO_COLOR`, refresh-hook, and real-tmux dashboard coverage
- Added a reviewable operator design reference with an ASCII wireframe, hierarchy, semantic tokens, responsive contract, plain-mode behavior, and intentional omissions

### Fixed

- Forced the first successful dashboard refresh after temporary metadata unavailability to repaint even when broker state is unchanged
- Added portable event-driven `SIGWINCH` refresh so tmux resize/zoom recomputes the dashboard layout without waiting for another workflow transition

### Security

- Kept dashboard output metadata-only, control-byte sanitized, width-bounded, and free of workflow/provider bodies while restoring cursor state on broker exit or error

## 0.6.3 - 2026-08-14

### Added

- Added `/or-*` short aliases for every canonical `/orchestrator-*` slash command, sharing the exact same handlers and safety checks
- Added a non-blocking, time-bounded startup notice when npm has a newer release, plus `/orchestrator-about` and `/or-about` for version, update, and project details
- Added `PI_TMUX_ORCHESTRATOR_DISABLE_UPDATE_NOTICE=1` to disable startup update notices
- Added strict user-global provider/model/thinking defaults with per-role overrides, custom config-path support, and explicit CLI precedence
- Added bounded `/orchestrator-models` and `/or-models` metadata discovery plus natural-language model-tool support for the parent model or exact all-role/per-role overrides
- Added an interactive running-orchestration selector when Pi slash commands omit the session for status, watch, attach, send, or stop

### Fixed

- Queued non-triggering task baseline and evidence context as `followUp` before assignment turns, preventing workers from acting on generic role instructions without the requested task

## 0.6.2 - 2026-08-14

### Added

- Added `/orchestrator-attach` and the model tool's `attach` action to ensure parent observation and switch an in-tmux parent client into the exact live worker grid
- Added and regression-tested a repeatable tmux detach/return path (`prefix`, then `L`) that preserves both the invoking Pi pane and live worker panes

### Changed

- Made native interactive Pi TUI panes—with Pi highlighting, tool rendering, and direct input editors—the package command default; plain RPC panes remain explicit headless automation only
- Clarified that the invoking Pi is the parent supervisor, normal starts create no separate parent Pi/window/controller, `watch` subscribes to updates, and `attach` enters the live worker grid
- Constrained each worker's report-tool schema to its valid role kind, changed-path, and verdict fields, avoiding preventable visible validation retries

## 0.6.1 - 2026-08-14

### Fixed

- Added `/orchestrator-watch` and the model tool's `watch` action so a parent Pi can attach to lifecycle and final-report updates for an existing compatible run
- Made attached parents show bounded lifecycle and report-received progress while keeping raw assistant/tool output in tmux
- Made model-tool status results include workflow and role states so ready, active, waiting, and uncertain runs are immediately distinguishable

## 0.6.0 - 2026-08-14

Parent-supervision and worker-visibility release.

### Added

- Added an authenticated read-only broker observer that returns bounded structured completion, attention, and uncertainty updates to the Pi session that started the run
- Added bounded in-memory report replay for live observers without placing report bodies in SQLite, status, journals, registries, or Supervisor API output
- Added explicit `needs_attention` workflow transitions when a worker settles with an active assignment but no report
- Added workflow-level `uncertain` propagation for ambiguous delivery, reconnect, lifecycle, and report-routing transitions without blind assignment replay
- Added model-free coverage for parent report delivery and visible RPC assistant/tool output

### Changed

- Changed the invoking Pi into the primary interactive supervisor while keeping tmux as the live worker view and the broker as deterministic transport/state
- Changed RPC panes to render assistant progress, tool inputs, and tool outputs instead of tool-name placeholders
- Clarified that the persistent project-neutral controller is optional and can serve as the parent Pi for cross-project operation

## 0.5.1 - 2026-08-14

### Fixed

- Removed unsolicited Pi status-line, widget, and title chrome from the package extension; orchestrator UI now appears only as explicit command notifications and results.

## 0.5.0 - 2026-08-13

Event-driven structured coordination release.

### Added

- Added one dependency-free Python broker per orchestration over an owner-only Unix socket with strict length-prefixed JSON, independent role/control authentication, role ACLs, bounded schemas, idempotent IDs, and crash-`uncertain` semantics
- Added one Pi worker bridge shared by TUI and RPC presentation, using custom messages for delivery, non-context custom entries for deduplication, lifecycle hooks, abort, and a terminating `orchestrator_report` tool
- Added metadata-only SQLite workflow, assignment, report-shape, lifecycle, command, event, usage, context-pressure, and soft-budget state
- Added structured implementation, review, probe, Playwright, and Django reports with bounded summaries, paths, checks, findings, verdicts, risks, and limitations
- Added Supervisor API v2 broker snapshots and event pages while preserving tmux-independent retained-state reads
- Added protocol, state, ACL, payload-redaction, no-polling, no-sleep, prompt, and bridge regression coverage

### Changed

- Changed every newly started run to manifest v3 and `coordination: broker-v1`; no option or fallback starts file-based coordination
- Changed tmux from the workflow transport to the monitorable host for the broker and Pi workers
- Changed TUI and `--rpc-workers` into presentation choices over one coordination protocol
- Changed reviewer scheduling to wake only after required implementation/specialist evidence exists and removed approval-only implementer turns
- Changed idle instructions to end the turn and explicitly prohibit sleep and coordination polling
- Changed status and Supervisor API to expose provider-reported token/cost totals and context pressure without inventing estimates
- Retained compatibility readers and controls for existing `0.4.x` manifests without exposing legacy coordination for new runs

### Removed

- Removed Markdown handoffs/reviews/specialist payloads, `.ready` markers, relay polling, mailbox payload files, and tmux key injection from newly started workflow coordination
- Removed copied diffs/logs and unbounded prose reports from the inter-agent contract

## 0.4.2 - 2026-08-09

Package-identity migration; orchestration behavior and the 29-file artifact surface are unchanged.

### Changed

- Renamed the canonical npm package from `@revazi/pi-tmux-orchestrator` to the consistent unscoped name `pi-tmux-orchestrator`
- Updated npm badges, Pi installation and migration commands, deterministic package verification, and installed-artifact acceptance for the new package identity
- Retained the 0.4.0 and 0.4.1 Git tags and GitHub Releases as source history while retiring the obsolete scoped npm publication

## 0.4.1 - 2026-08-09

Documentation-only patch release; orchestration runtime, package name, license, and author metadata are unchanged.

### Changed

- Added the standard npm version, npm downloads, CI, and MIT license badges
- Focused the README and packaged usage/security documentation exclusively on Pi Tmux Orchestrator
- Simplified installation guidance and restored the normal clean-repository `npm publish --access public` maintainer workflow

## 0.4.0 - 2026-08-09

This is the first npm-published release of Pi Tmux Orchestrator.

### Added

- Added the `0.4.0` Pi package with one thin JavaScript extension, the existing root skill, the Python CLI bin, zero owned runtime dependencies, and deterministic tarball verification
- Added the owner-authorized canonical MIT license with 2026 Revaz Zakalashvili copyright and exact npm author metadata
- Added public scoped-package metadata, `pi-package` discovery, exact npm/Pi installation guidance, and a manual maintainer release checklist
- Added schema-v1 `--json` output for doctor, list, status, start/dry-run, send, restart, and stop; attach returns a clear `interactive_only` JSON error
- Added the canonical `/orchestrator-*` help/doctor/start/list/status/send/stop command family while retaining `/orchestrate` and `/orchestrations` as shared-handler aliases
- Added `tmux_orchestrator` tool actions for bounded doctor/list/status/start/send operations
- Added `controller start|status|attach|stop` for one persistent project-neutral Pi controller with a stable session ID, private state/session storage, exact tmux identity, duplicate refusal, and confirmed termination
- Added opt-in `--rpc-workers` with dependency-free per-role Pi RPC supervisors, private mailbox transport, correlated prompt/abort acknowledgements, steer/follow-up delivery, bounded queue/session state, and CLI `abort`
- Added stable per-role worker registries, supervisor generations, rotating metadata-only lifecycle journals, idempotent send/abort command IDs, deterministic relay IDs, crash-uncertain recovery, and retained `events` cursors
- Added Supervisor API v1 with bounded tmux-independent retained session/run discovery, worker snapshots, independent per-role event pages, exact command status, and exact-run RPC control targeting
- Added model-free Python and Node tests, actual-tarball inspection and installation, isolated Pi discovery from the installed artifact, an offline publication dry run, and Node 22.19/24 CI coverage

### Changed

- Replaced the monolithic script with the authoritative modular `pi_tmux_orchestrator/` package and a thin `bin/pi-tmux-agents` launcher; repository Python lint/format checks now use pinned Ruff 0.11.11
- Updated the skill to prefer extension controls when available while retaining the standalone CLI fallback
- Kept the Python/tmux CLI authoritative; the extension delegates only through JSON and does not bridge child Pi TUIs
- Removed the unnecessary Pi peer dependency because the zero-import extension requires no owned or peer dependency tree
- Expanded deterministic package acceptance to enforce MIT/author metadata and an exact 29-file modular artifact
- Changed Pi discovery acceptance to install the npm-installed tarball root through isolated `pi install`, launch RPC without `--extension`, and require exact package provenance for nine extension commands plus the root skill
- Documented immediately usable local-path and public Git package installation while keeping npm installation conditional on verified registry publication
- Made unexpected public JSON failures return one generic `internal_error` envelope, fixed command attribution on adversarial parser errors, clarified workflow-only read policy with retained bash, and returned custom tool rendering to Pi's safe fallback
- Limited package OS metadata to tested macOS/Linux platforms, made verification enforce the exact public manifest/control surface and packed allowlist, and made successful Pi RPC discovery require exact installed-package source paths and empty stderr
- Updated every CI action to a full commit SHA on its Node 24-compatible v7 release
- Hardened private state creation and writes against symlink redirection without changing pre-existing ancestor permissions, added canonical state-root containment, and made manifest replacement atomic with unique private temporary files
- Added strict schema-v1 manifest validation for fields, roles, pane IDs, trust, canonical project/coordination paths, and contained role paths before orchestration actions
- Made relay delivery loss-resistant with report/marker validation, required specialist/review enums, per-recipient retry state, and global completion only after every enabled recipient succeeds
- Changed status and monitor output to report coordination file names and byte sizes without content previews
- Added diagnosable failed-start state and exact partial-session cleanup
- Applied exact tmux session/window targets to all operations on existing orchestrations so a vanished target cannot fall through to a prefix match
- Expanded the real tmux smoke to five synthetic RPC workers, relay acknowledgements, follow-up delivery, abort, private cleanup, process-residue checks, and Supervisor API coverage
- Documented tmux ownership of worker/controller hosting and the durable Supervisor API read boundary

### Security

- Added explicit parent-trust checks and per-run confirmation before child `--approve`; extension start is rejected when interactive confirmation is unavailable
- Added mode-`0600` temporary-file transfer and `finally` cleanup for task, specialist, and message bodies so those bodies do not enter argv or tool-visible metadata
- Added bounded JSON errors/arrays, subprocess-error sanitization, package exclusion checks, isolated Pi/npm homes and credential-free child environments for package acceptance, and full-SHA CI action pins
- Added regressions for state symlinks and containment, manifest tampering, relay races and retry deduplication, metadata redaction, startup recovery, controller identity, RPC crash ambiguity, and provider-free six-pane cleanup

## 0.3.0 - 2026-08-04

### Added

- Optional read-only Playwright tester with per-round browser reports
- Optional read-only senior Django expert with per-round framework reviews
- Specialist task files, model overrides, prompts, coordination records, messaging, restart support, and relay routing

### Changed

- Tmux session existence checks now target exact names, so prefix-collision sessions are not mistaken for the requested session
- Reviewer and implementer coordination contracts now include enabled specialist reports
- Source documentation and model-free regressions now represent the installed v0.3.0 baseline

This version reflects a local installed skill baseline. It was not published to npm or PyPI and is not a claim of public release.

## 0.1.0 - 2026-08-02

### Added

- Global Pi skill for tmux agent orchestration
- Implementer and independent reviewer workflow
- Optional read-only technical probe
- Live relay and status pane
- Numbered handoff and review rounds
- Per-role provider, model, and thinking configuration
- Role messaging and safe restart commands
- Private external coordination state
- Project trust controls
- Model availability doctor and dry-run support
- Model-free tmux functional smoke
- Global installation script and operator documentation