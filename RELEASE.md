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

- [ ] Confirm `package.json`, `VERSION`, `pi_tmux_orchestrator/constants.py`, tests, documentation, and the intended `v0.4.0` tag all use `0.4.0`.
- [ ] Confirm no unexpected prerelease strings remain: `git grep '0\.4\.0-dev'` should return no matches.

## 3. Full verification and artifact inspection

- [ ] Run `scripts/test.sh` with Ruff 0.11.11 on supported Node/Python versions and require CI success.
- [ ] Run `node scripts/verify-package.mjs` and `scripts/package-smoke.sh`.
- [ ] Retain and manually inspect a fresh tarball:

  ```bash
  ARTIFACT_DIR=$(mktemp -d)
  npm pack --ignore-scripts --pack-destination "$ARTIFACT_DIR"
  tar -tzf "$ARTIFACT_DIR"/*.tgz
  ```

- [ ] Confirm it contains exactly 27 files: the manifest, `LICENSE.md`, modular Python runtime, launcher, extension/skill resources, and operator documentation; no tests, CI, state, sessions, credentials, caches, generated sessions, private task content, or unrelated development files. Remove the disposable artifact directory after review.
- [ ] Confirm the packed/installed manifest reports the exact MIT/author metadata, the installed CLI reports `pi-tmux-agents 0.4.0`, and the owned npm dependency tree is empty.
- [ ] Confirm package acceptance uses isolated `pi install <local-package-root>` on the npm-installed tarball root, launches RPC without `--extension`, and discovers exactly nine extension commands plus `skill:tmux-agent-orchestrator` with package provenance.
- [ ] Confirm the slash surface omits attach/restart, aliases share start/list handlers, start/stop confirmations remain mandatory, and interactive send never exposes message text outside its unique private file.
- [ ] Confirm local-path and public Git package installation guidance is current, while npm installation remains explicitly conditional on verified registry availability.
- [ ] Confirm the isolated offline `npm publish --dry-run` succeeds. It uses empty npm configuration, scripts disabled, offline mode, and a loopback registry; it is not a registry acceptance test.

## 4. Human npm checks

These checks intentionally are not automated because they use the maintainer's network and npm identity.

- [ ] Run `npm whoami` and verify the expected human account.
- [ ] Verify that account controls the `@revazi` scope and may publish a public scoped package. For an existing package, verify its access with `npm access get status @revazi/pi-tmux-orchestrator`; for a first publication, verify scope ownership through npm's current CLI or website.
- [ ] Confirm npm account protections, current registry, intended `latest` tag, and `publishConfig.access: public`.
- [ ] Reinspect the exact tarball immediately before publication and confirm no secret, session, state, or private payload leakage.

## 5. Authorized publication and tag consistency

- [ ] Obtain a final explicit human approval for the exact commit and tarball.
- [ ] Run any real `npm publish` only manually from that approved clean commit. Never run it from tests or CI in this repository.
- [ ] Create/push `v0.4.0` only when authorized, and verify the tag points to the exact published source commit and matches all authoritative versions.
- [ ] Only after registry verification describe `pi install npm:@revazi/pi-tmux-orchestrator@0.4.0` as available. Verify npm/Pi installation in a disposable home before making registry or gallery acceptance claims.
