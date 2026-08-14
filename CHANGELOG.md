# Changelog

All notable changes to this project are documented here.

## [Unreleased]

_No changes yet._

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