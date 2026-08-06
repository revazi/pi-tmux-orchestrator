# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added

- Added an unreleased private `0.4.0-dev.0` Pi package candidate with one thin JavaScript extension, the existing root skill, the Python CLI bin, zero owned runtime dependencies, and deterministic tarball verification
- Added schema-v1 `--json` output for doctor, list, status, start/dry-run, send, restart, and stop; attach now returns a clear `interactive_only` JSON error
- Added `tmux_orchestrator` tool actions for doctor/list/status/start/send plus `/orchestrate`, `/orchestrations`, and confirmed `/orchestrator-stop` commands
- Added model-free Node tests, disposable package installation, isolated Pi extension loading, and Node 22.19/24 CI coverage

### Changed

- Updated the skill to prefer extension controls when available while retaining the standalone CLI fallback
- Kept the Python/tmux CLI authoritative; the extension delegates only through JSON and does not bridge child Pi TUIs
- Removed the unnecessary Pi peer dependency because the zero-import extension requires no owned or peer dependency tree
- Strengthened the disposable Pi smoke to use strict JSONL RPC `get_commands` discovery and assert the exact extension-command/root-skill surface without prompting a provider
- Made unexpected public JSON failures return one generic `internal_error` envelope, fixed command attribution on adversarial parser errors, clarified workflow-only read policy with retained bash, and returned custom tool rendering to Pi's safe fallback
- Limited package OS metadata to tested macOS/Linux platforms, made verification enforce the exact manifest/control surface and packed allowlist, and made successful Pi RPC discovery require empty stderr
- Updated every CI action to a full commit SHA on its Node 24-compatible v7 release

- Hardened private state creation and writes against symlink redirection without changing pre-existing ancestor permissions, added canonical state-root containment, and made manifest replacement atomic with unique private temporary files
- Added strict schema-v1 manifest validation for fields, roles, pane IDs, trust, canonical project/coordination paths, and contained role paths before orchestration actions
- Made relay delivery loss-resistant with report/marker validation, required specialist/review enums, per-recipient retry state, and global completion only after every enabled recipient succeeds
- Changed status and monitor output to report coordination file names and byte sizes without content previews
- Added diagnosable failed-start state and exact partial-session cleanup
- Applied exact tmux session/window targets to all operations on existing orchestrations so a vanished target cannot fall through to a prefix match

### Security

- Added explicit parent-trust checks and per-run confirmation before child `--approve`; extension start is rejected when interactive confirmation is unavailable
- Added mode-`0600` temporary-file transfer and `finally` cleanup for task, specialist, and message bodies so those bodies do not enter argv or tool-visible metadata
- Added bounded JSON errors/arrays, subprocess-error sanitization, package exclusion checks, and full-SHA CI action pins
- Added regressions for state symlinks and containment, manifest tampering, relay races and retry deduplication, metadata redaction, startup recovery, and provider-free six-pane cleanup

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