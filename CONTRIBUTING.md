# Contributing

## Workflow

1. Sync local `main`.
2. Create a focused `feature/`, `fix/`, or `chore/` branch.
3. Keep changes small and update relevant documentation.
4. Run:

   ```bash
   scripts/test.sh
   ```

5. Open a pull request and report actual verification results.
6. Wait for CI and review before merge.
7. Merge with squash only.

Do not push feature work directly to `main` after the initial repository bootstrap.

## Design expectations

- Preserve a single-writer model: reviewer, probe, Playwright, and Django roles remain read-only.
- Keep Python dependencies at zero unless a concrete requirement justifies one.
- Keep target repository files separate from orchestration state.
- Retain explicit confirmation for trust bypass, role restart, and session stop.
- Keep human and schema-v1 JSON behavior aligned; errors, arrays, and status output must remain bounded and free of private payloads.
- Keep the Pi extension thin: it may delegate to the bundled Python CLI but must not duplicate state transitions or bridge child Pi TUIs.
- Add tests that reproduce the behavior being corrected.

## Testing

`scripts/test.sh` runs:

- Python syntax validation
- Standard-library unit tests
- Skill frontmatter checks
- Shell syntax checks
- A model-free tmux functional smoke covering all roles, relay markers, and exact session targeting
- CLI help and an all-role dry-run
- Node built-in extension tests for registration, trust, confirmation, cancellation, and private-file cleanup
- deterministic `npm pack --dry-run --json` contents and version checks
- a disposable tarball install and isolated Pi RPC `get_commands` discovery smoke

The test suite must never issue a provider request or inspect real Pi authentication files. Never run `install.sh` against the real home while developing the private candidate; use `PI_AGENT_HOME` or `PI_CODING_AGENT_DIR` pointing at a temporary directory. Do not publish, tag, or claim Git-package update/rollback acceptance.

## Security reports

Do not open public issues containing credentials, private task text, session logs, or provider payloads. Use GitHub private security reporting or contact the repository owner privately.