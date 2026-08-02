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

- Preserve a single-writer model.
- Keep Python dependencies at zero unless a concrete requirement justifies one.
- Keep target repository files separate from orchestration state.
- Retain explicit confirmation for trust bypass, role restart, and session stop.
- Keep errors and status output bounded and free of private payloads.
- Add tests that reproduce the behavior being corrected.

## Testing

`scripts/test.sh` runs:

- Python syntax validation
- Standard-library unit tests
- Skill frontmatter checks
- Shell syntax checks
- A model-free tmux functional smoke
- CLI help and dry-run checks

The test suite must never issue a provider request or inspect Pi authentication files.

## Security reports

Do not open public issues containing credentials, private task text, session logs, or provider payloads. Use GitHub private security reporting or contact the repository owner privately.