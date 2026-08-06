# Security

## Supported version

The latest commit on `main` is supported while the project remains pre-1.0.

## Reporting

Report security issues through a private GitHub Security Advisory when available, or contact the repository owner privately. Do not include credentials, raw provider responses, private source documents, or unrelated session logs.

## Trust boundaries

Pi Tmux Orchestrator:

- launches local Pi processes under the current operating-system account
- uses the current user's configured Pi providers without reading authentication files
- stores orchestration state outside target repositories
- passes role prompts as file attachments rather than full command-line text
- validates model availability with `pi --list-models`, which does not send a model request
- exposes a thin extension that invokes only the bundled Python CLI through versioned JSON mode
- does not provide sandboxing between agents running as the same user

Only the implementer is given normal Pi write tools. Reviewer, technical probe, Playwright tester, and Django expert omit `edit` and `write`, but retain `bash` to run tests. Their read-only policy is therefore a workflow boundary, not an operating-system sandbox. Playwright artifacts and test data are limited by prompt contract to ignored or external temporary paths, and the tester must clean up local servers and browser processes.

## Private data

Never put these values in task, handoff, review, probe, status, test, or issue content:

- API keys, OAuth tokens, Keychain values, or authentication files
- private résumé, customer, production, or business data
- raw provider requests or responses
- system prompts or proprietary prompt payloads
- private endpoints
- unbounded raw errors

Coordination directories are created with mode `0700` and files with mode `0600`. The configured state root and each session/run directory must be non-symlink directories, canonical coordination paths must remain under that root, and state files consumed by the orchestrator must be regular non-symlink files. Writes use no-follow opens where available, descriptor checks, and private permissions. Manifest updates use unique temporary files and atomic replacement.

Schema-v1 manifests are bounded and strictly validated for required fields, known roles, role configuration types, pane IDs, trust type, canonical project/coordination paths, exact session/window identity, and contained prompt/session paths before an existing orchestration is acted upon. Validation inspects prompt metadata without reading prompt or report bodies.

Status, monitor, and JSON output expose only bounded metadata such as coordination file names and byte sizes. They do not preview task, message, prompt, report, specialist, or provider payload bodies. The extension writes those input bodies to unique mode-`0600` files in a private temporary directory, passes only file paths in argument arrays, and removes the directory in `finally`, including cancellation and errors. Temporary cleanup reduces persistence but is not a secure-erasure claim. Operators remain responsible for local backups, screen recording, shell history, tmux server access, temporary-storage behavior, and other processes running as the same user.

The `0.4.0` source package declares no dependencies or peers, owns no runtime tree, does not bundle Pi, and excludes tests, CI, state, sessions, credentials, caches, generated sessions, and development-only files from its deterministic pack list. Its scoped manifest requests public access, but publication remains a manual operator action; package metadata, a tarball, and dry-runs do not prove registry or gallery availability. `UNLICENSED` is preserved and grants no new software license.

Extension tests use mocks plus isolated RPC `get_commands` discovery from a disposable installation of the actual tarball. The acceptance smoke replaces Pi/npm homes and configuration, strips inherited credential environment variables from child processes, uses offline npm operations and a loopback dry-run registry, sends no prompt, and issues no provider request. These controls prevent test use of the configured Pi/npm authentication locations; they are not an operating-system sandbox or secure-erasure claim.

## Relay delivery boundary

The relay requires each ready marker's matching report to be a regular non-empty file. Playwright, Django, and reviewer reports also require their documented first-line enums. Invalid or missing reports remain pending. Successful transport is recorded per marker and enabled recipient so retries do not intentionally duplicate already successful recipients; a marker is globally complete only after all intended recipients succeed.

A successful tmux `send-keys` operation proves only that tmux accepted the keystrokes. It does not prove Pi received, processed, or acknowledged the notice. Same-user processes can inspect or alter tmux and coordination state; these checks are hardening, not same-user sandboxing.

## Project trust

`--approve-project` passes Pi's trust bypass to child sessions. Use it only after inspecting and trusting the target project. Without it, each child Pi session must obtain trust interactively. The extension rejects this bypass unless the parent context reports the project trusted and the user separately confirms it for that run. The parent decision is never represented as automatically applying to children. Extension starts are rejected outside the interactive TUI when confirmation is unavailable.

## Destructive operations

The CLI:

- refuses to replace an existing tmux session
- uses exact tmux targets for every operation on an existing session/window, including attach, status, stop, and failure cleanup
- requires `--yes` to restart a role
- requires `--yes` to stop a session
- retains coordination records after session termination
- kills a partially created tmux session after startup failure and leaves a private `FAILED` startup state when safe

It does not push, merge, publish, deploy, or clean target repositories.
