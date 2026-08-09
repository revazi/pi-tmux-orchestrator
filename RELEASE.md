# Maintainer release checklist

This checklist prepares a human-controlled release. It does not authorize publication, tagging, pushing, or making the repository public. Local package checks establish publish-ready mechanics only; they do not establish npm-registry, Pi-gallery, update, rollback, or production acceptance.

## 1. Owner, author, and license confirmation

- [ ] Obtain explicit owner authorization for the release.
- [ ] Confirm `LICENSE.md` is the canonical MIT text with `Copyright (c) 2026 Revaz Zakalashvili` and `package.json` reports license `MIT`.
- [ ] Confirm npm author metadata is exactly Revaz Zakalashvili, `revaz.zakalashvili@gmail.com`, and `https://github.com/revazi`; do not change the license or public identity/contact metadata without another explicit owner decision.
- [ ] Confirm the public repository/package content is appropriate for distribution under the MIT License.

## 2. Clean release source

- [ ] Start from an up-to-date, clean `main`, not a feature worktree:

  ```bash
  git switch main
  git pull --ff-only
  test -z "$(git status --porcelain)"
  ```

- [ ] Confirm `package.json`, `VERSION`, `pi_tmux_orchestrator/constants.py`, tests, documentation, and the intended `v0.4.1` tag all use `0.4.1`.
- [ ] Confirm `CHANGELOG.md` has no pending release content: `[Unreleased]` is empty and every intended change appears under the dated `0.4.1` entry.
- [ ] Confirm no unexpected prerelease strings remain: `git grep '0\.4\.1-dev'` should return no matches.

## 3. Full verification and artifact inspection

- [ ] Run `scripts/test.sh` with Ruff 0.11.11 on supported Node/Python versions and require CI success.
- [ ] Run `node scripts/verify-package.mjs` and `scripts/package-smoke.sh`.
- [ ] Retain and manually inspect a fresh tarball:

  ```bash
  ARTIFACT_DIR=$(mktemp -d)
  npm pack --ignore-scripts --pack-destination "$ARTIFACT_DIR"
  tar -tzf "$ARTIFACT_DIR"/*.tgz
  ```

- [ ] Confirm it contains exactly 29 files: the manifest, `LICENSE.md`, modular Python runtime, launcher, extension/skill resources, and operator documentation; no tests, CI, state, sessions, credentials, caches, generated sessions, private task content, or unrelated development files. Remove the disposable artifact directory after review.
- [ ] Confirm the packed/installed manifest reports the exact MIT/author metadata, the installed CLI reports `pi-tmux-agents 0.4.1`, and the owned npm dependency tree is empty.
- [ ] Confirm package acceptance uses isolated `pi install <local-package-root>` on the npm-installed tarball root, launches RPC without `--extension`, and discovers exactly nine extension commands plus `skill:tmux-agent-orchestrator` with package provenance.
- [ ] Confirm the slash surface omits attach/supervisor/restart, aliases share start/list handlers, start/stop confirmations remain mandatory, and interactive send never exposes message text outside its unique private file.
- [ ] Confirm supervisor API v1 reads retained sessions/runs/snapshots/events/commands without tmux, preserves per-role cursor gaps and bounds, labels host runtime `not_observed`, and exact-run send/abort retains private mailbox semantics.
- [ ] Confirm documentation presents worker/controller hosting and live-session operations as tmux-owned while retained-state reads remain available after tmux exits.
- [ ] Confirm local-path and public Git package installation guidance is current, while npm installation remains explicitly conditional on verified registry availability.
- [ ] Confirm the isolated offline `npm publish --dry-run` succeeds. It uses empty npm configuration, scripts disabled, offline mode, and a loopback registry; it is not a registry acceptance test.
- [ ] Record the clean source commit and inspect the generated tarball as release evidence. Remove the disposable artifact after review; normal publication runs from the approved repository root.

## 4. Human npm checks

These checks intentionally are not automated because they use the maintainer's network and npm identity.

- [ ] Run `npm whoami` and verify the expected human account.
- [ ] Verify that account controls the `@revazi` scope and may publish a public scoped package. For an existing package, verify its access with `npm access get status @revazi/pi-tmux-orchestrator`; for a first publication, verify scope ownership through npm's current CLI or website.
- [ ] Confirm npm account protections, current registry, intended `latest` tag, and `publishConfig.access: public`.
- [ ] Reinspect the exact tarball immediately before publication and confirm no secret, session, state, or private payload leakage.

## 5. Authorized publication and tag consistency

- [ ] Obtain final explicit human approval for the exact clean commit and package version.
- [ ] From that approved repository root, publish manually with the verified human npm identity. Never publish from tests or CI in this repository:

  ```bash
  git switch main
  git pull --ff-only
  test -z "$(git status --porcelain)"
  npm publish --access public --tag latest
  ```

- [ ] Verify the public registry reports exact name/version/license/repository metadata and record its `dist.integrity`; do not treat the local publish response alone as acceptance.
- [ ] Install `@revazi/pi-tmux-orchestrator@0.4.1` into disposable npm and Pi homes, rerun CLI/version and package-provenance discovery, and confirm no real provider request is made.
- [ ] Create and push annotated tag `v0.4.1` only after registry acceptance is verified. Confirm the tag points to the approved source commit and all authoritative versions match.
- [ ] Create the GitHub release from `v0.4.1` using the matching changelog entry, and verify both the release and public tarball links without changing package contents.
- [ ] Only after those checks describe `pi install npm:@revazi/pi-tmux-orchestrator@0.4.1` as available. npm publication does not by itself prove a separate Pi gallery listing.
