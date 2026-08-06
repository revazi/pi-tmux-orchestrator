# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Changed

- Hardened private state creation and writes against symlink redirection without changing pre-existing ancestor permissions, added canonical state-root containment, and made manifest replacement atomic with unique private temporary files
- Added strict schema-v1 manifest validation for fields, roles, pane IDs, trust, canonical project/coordination paths, and contained role paths before orchestration actions
- Made relay delivery loss-resistant with report/marker validation, required specialist/review enums, per-recipient retry state, and global completion only after every enabled recipient succeeds
- Changed status and monitor output to report coordination file names and byte sizes without content previews
- Added diagnosable failed-start state and exact partial-session cleanup
- Applied exact tmux session/window targets to all operations on existing orchestrations so a vanished target cannot fall through to a prefix match

### Security

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