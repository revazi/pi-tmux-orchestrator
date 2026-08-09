# Contributing

## Workflow

1. Sync local `main`.
2. Create a focused `feature/`, `fix/`, or `chore/` branch.
3. Keep changes small and update relevant documentation.
4. Run with the CI-pinned Ruff version:

   ```bash
   python -m pip install ruff==0.11.11
   scripts/test.sh
   ```

5. Open a pull request and report actual verification results.
6. Wait for CI and review before merge.
7. Merge with squash only.

Do not push feature work directly to `main` after the initial repository bootstrap.

## Design expectations

- Preserve a single-writer model: reviewer, probe, Playwright, and Django roles remain read-only.
- Keep Python dependencies at zero unless a concrete requirement justifies one.
- Preserve tmux ownership of worker/controller hosting and live-session operations.
- Keep target repository files separate from orchestration state.
- Retain explicit confirmation for trust bypass, role restart, and session stop.
- Keep human and schema-v1 JSON behavior aligned; errors, arrays, and status output must remain bounded and free of private payloads.
- Keep the Pi extension thin: it may delegate to the bundled Python CLI but must not duplicate state transitions or bridge child Pi TUIs. Canonical slash commands and compatibility aliases must share handlers.
- Add tests that reproduce the behavior being corrected.

## Testing

`scripts/test.sh` runs:

- Ruff lint and format checks
- Python syntax validation
- Standard-library unit tests, including retained-state supervisor API cursors and tmux-independent reads
- Skill frontmatter checks
- Shell syntax checks
- A model-free tmux functional smoke covering all roles, relay markers, and exact session targeting
- CLI help and an all-role dry-run
- Node built-in extension tests for the exact nine-command surface, shared aliases, delegation, trust/confirmation, bounded errors, and private send cancellation/cleanup/redaction
- deterministic manifest and `npm pack --dry-run --json` checks
- exact inspection and disposable installation of the actual 29-file modular tarball, including MIT/author metadata
- isolated `pi install` of the npm-installed package root followed by RPC `get_commands` discovery without `--extension`, requiring exact package provenance
- an offline `npm publish --dry-run` with empty isolated npm configuration

The test suite must never issue a provider request or inspect real Pi/npm authentication files. Never run `install.sh` against the real home while developing; use `PI_AGENT_HOME` or `PI_CODING_AGENT_DIR` pointing at a temporary directory. Tests must not publish, tag, or claim npm-registry, Pi-gallery, Git-package update, or rollback acceptance.

## Release preparation

Follow [RELEASE.md](RELEASE.md) for the human-controlled release checklist. Local package checks and publication dry-runs never publish the package. The MIT license and Revaz Zakalashvili author metadata are explicitly owner-authorized; do not change the license or public identity/contact metadata without another explicit owner decision.

## Security reports

Do not open public issues containing credentials, private task text, session logs, or provider payloads. Use GitHub private security reporting or contact the repository owner privately.