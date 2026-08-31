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

- [ ] Set `RELEASE_VERSION=$(tr -d '\n' < VERSION)` and confirm `package.json`, `pi_tmux_orchestrator/constants.py`, tests, documentation, and intended tag `v$RELEASE_VERSION` all match.
- [ ] Confirm `CHANGELOG.md` has no pending release content: `[Unreleased]` is empty and every intended change appears under the dated `$RELEASE_VERSION` entry.
- [ ] Confirm no unexpected prerelease strings remain.

## 3. Full verification and artifact inspection

- [ ] Run `scripts/test.sh` with Ruff 0.11.11 on supported Node/Python versions and require CI success.
- [ ] Run `node scripts/verify-package.mjs` and `scripts/package-smoke.sh`.
- [ ] Retain and manually inspect a fresh tarball:

  ```bash
  ARTIFACT_DIR=$(mktemp -d)
  npm pack --ignore-scripts --pack-destination "$ARTIFACT_DIR"
  tar -tzf "$ARTIFACT_DIR"/*.tgz
  ```

- [ ] Confirm it exactly matches the deterministic allowlist: manifest, `LICENSE.md`, modular Python broker/control runtime, launcher, controller and worker extensions, skill/protocol/operator documentation; no tests, CI, state, sessions, credentials, caches, generated sessions, private task content, or unrelated development files. Remove the disposable artifact directory after review.
- [ ] Confirm the packed/installed manifest reports the exact MIT/author metadata, the installed CLI reports `pi-tmux-agents $RELEASE_VERSION`, and the owned npm dependency tree is empty.
- [ ] Confirm package acceptance uses isolated `pi install <local-package-root>` on the npm-installed tarball root, launches RPC without `--extension`, and discovers exactly `/or-dashboard`, `/or-models`, `/or-start`, `/or-send`, `/or-stop`, plus `skill:tmux-agent-orchestrator`, all with package provenance.
- [ ] Confirm the short-only slash surface omits supervisor/restart and removed duplicate aliases; the dashboard loads sessions without automatically running doctor, `d` runs bounded doctor on demand, Enter performs only an exact in-tmux watch/attach with the documented return key, `x` requires explicit exact-session stop confirmation, start/stop confirmations remain mandatory, and interactive send never exposes message text outside its unique private file.
- [ ] Confirm strict user-global model configuration accepts no credential/endpoint fields, precedence is explicit override → role → global → packaged fallback, available-model discovery is bounded/auth-free, and natural-language starts preserve exact user-requested provider/model/thinking values.
- [ ] Confirm Supervisor API v2 reads retained sessions/runs/snapshots/events without tmux, preserves cursor gaps/bounds, labels host runtime `not_observed`, exposes actual provider usage only, and broker send/abort retains idempotent acceptance-versus-completion semantics.
- [ ] Confirm every new manifest is v5/`broker-v1`, retained manifest v1-v4 runs remain readable, no legacy coordination option exists for new runs, workflow payloads stay out of SQLite/status, TUI/RPC share the bridge, agents do not poll or sleep, and tmux hosts/monitors rather than transports workflow messages.
- [ ] Confirm native Pi TUI panes are the interactive package-command default with normal highlighting, tool rendering, and direct editors; RPC panes remain explicit headless automation and show bounded assistant/tool input/tool output content.
- [ ] Confirm parent observer reports return only to the invoking Pi session, attach keeps that observer alive while switching the exact tmux client, `needs_attention` is event-driven, and observer report bodies never enter SQLite or metadata APIs.
- [ ] Confirm the optional 12 KiB parent context capsule uses private startup delivery without transcript copying or another model request, the 16 KiB UTF-8 rolling state keeps the latest report per role, and neither body enters SQLite or metadata APIs.
- [ ] Confirm provider-context projection preserves the baseline, latest run state, active assignment, direct user/operator messages, current-turn content, restart recovery, and complete Pi history while the deterministic multi-round fixture remains at least 50% smaller.
- [ ] Confirm the bounded startup update notice detects a synthetic newer npm version, recommends `pi update npm:pi-tmux-orchestrator`, honors its opt-out, and remains disabled in worker/controller sessions.
- [ ] Confirm npm, reviewed-Git-commit, and local-path installation guidance is current.
- [ ] Confirm the isolated offline `npm publish --dry-run` succeeds. It uses empty npm configuration, scripts disabled, offline mode, and a loopback registry; it is not a registry acceptance test.
- [ ] Record the clean source commit and inspect the generated tarball as release evidence. Remove the disposable artifact after review; normal publication runs from the approved repository root.

## 4. Breaking-change communication

- [ ] Confirm the npm README has a prominent, version-specific upgrade section with the old-to-new command map, safe update sequence, compatibility facts, and rollback link.
- [ ] Confirm `CHANGELOG.md` labels removals under `Breaking`, rather than leaving them buried under general changes.
- [ ] Review `releases/v$RELEASE_VERSION.md` as the authoritative GitHub Release body. It must include migration steps, dashboard controls, profiles, exact per-project configuration, compatibility/safety facts, rollback, and support links without provider-cost or quality claims.
- [ ] Publish a public pre-release announcement issue linking the migration guide and pin it when repository permissions and pin capacity permit. Once the reviewed, versioned migration docs link back to it, the issue may close as a durable discussion archive rather than remain the release-state tracker.
- [ ] Allow a reasonable migration-notice window before moving the `latest` tag when timing permits. For an urgent release, explicitly record why the window was shortened.
- [ ] Prepare a post-release follow-up for the announcement archive with the exact GitHub Release, npm version, tag commit, and verified install command; GitHub permits maintainers to comment on a closed issue.

## 5. Trusted publishing setup

This is a one-time maintainer setup, not a per-release token workflow.

- [ ] In npm package settings, configure a GitHub Actions trusted publisher for repository `revazi/pi-tmux-orchestrator`, workflow file `release.yml`, and environment `npm`.
- [ ] Confirm the GitHub `npm` environment exists and the release job has only `contents: write` and `id-token: write` permissions.
- [ ] Confirm npm account protections, intended `latest` tag, and `publishConfig.access: public`.
- [ ] Do not add a long-lived npm token when trusted publishing is available.

## 6. Authorized connected release

- [ ] Obtain final explicit human approval for the exact clean commit and package version.
- [ ] Create and push one annotated version tag from that approved commit:

  ```bash
  git switch main
  git pull --ff-only
  test -z "$(git status --porcelain)"
  RELEASE_VERSION=$(tr -d '\n' < VERSION)
  git tag -a "v$RELEASE_VERSION" -m "Pi Tmux Orchestrator v$RELEASE_VERSION"
  git push origin "v$RELEASE_VERSION"
  ```

- [ ] Require the tag-triggered `Release` workflow to rerun all verification, publish the repository root to npm with OIDC provenance, verify exact registry name/version/commit/integrity, and then create the matching GitHub Release.
- [ ] If npm already contains that exact version from the exact tagged commit, a workflow rerun may skip publication and create the missing GitHub Release. Any metadata mismatch must fail closed.
- [ ] Verify npm `latest`, `dist.integrity`, and `gitHead`; then install the exact version into disposable npm and Pi homes and rerun CLI/version and package-provenance discovery without a provider request.
- [ ] Verify the GitHub Release tag and target commit match npm `gitHead`. Manual npm publication is recovery-only and must be followed by the exact matching tag and GitHub Release.
- [ ] Add the verified release/npm/install details to the public announcement archive even if it is already closed; reopen it only when migration support requires active issue tracking.
