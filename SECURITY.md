# Security

## Supported version

The latest commit on `main` is supported while the project remains pre-1.0.

## Reporting

Report security issues through a private GitHub Security Advisory when available, or contact the repository owner privately. Do not include credentials, raw provider responses, private source documents, or unrelated session logs.

## Trust boundaries

Pi Tmux Orchestrator:

- launches local Pi processes under the current operating-system account
- uses the current user's configured Pi providers without reading authentication files
- stores orchestration and controller state outside target repositories
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

Coordination and controller directories are created with mode `0700` and orchestrator-owned files with mode `0600`. The configured roots and each session/run directory must be non-symlink directories, canonical coordination paths must remain under the orchestration root, and state files consumed by the orchestrator must be regular non-symlink files. Writes use no-follow opens where available, descriptor checks, and private permissions. Manifest and controller-state updates use unique temporary files and atomic replacement. The controller launches Pi under `umask 077`, so conversation files remain protected by both private file creation and their private parent directory.

Schema-v1 manifests are bounded and strictly validated for required fields, known roles, role configuration types, pane IDs, trust type, canonical project/coordination paths, exact session/window identity, and contained prompt/session paths before an existing orchestration is acted upon. Validation inspects prompt metadata without reading prompt or report bodies.

Status, monitor, and JSON output expose only bounded metadata such as coordination file names and byte sizes. They do not preview task, message, prompt, report, specialist, or provider payload bodies. The extension writes those input bodies to unique mode-`0600` files in a private temporary directory, passes only file paths in argument arrays, and removes the directory in `finally`, including cancellation and errors. Temporary cleanup reduces persistence but is not a secure-erasure claim. Operators remain responsible for local backups, screen recording, shell history, tmux server access, temporary-storage behavior, and other processes running as the same user.

The `0.4.0` source package declares no dependencies or peers, owns no runtime tree, does not bundle Pi, and limits its deterministic 10-file artifact to the manifest, canonical MIT license, runtime resources, and operator documentation. Tests, CI, state, sessions, credentials, caches, generated sessions, and development-only files remain excluded. Its scoped manifest requests public access, but publication remains a manual operator action; package metadata, a tarball, and dry-runs do not prove registry or gallery availability. The owner-authorized license is MIT; see [LICENSE.md](LICENSE.md).

Extension tests use mocks plus package-provenance RPC `get_commands` discovery from a disposable installation of the actual tarball. The acceptance smoke npm-installs the tarball, points isolated `pi install <local-package-root>` at that package root, then launches RPC without `--extension` and requires exactly nine extension commands plus the root skill from that package path. It replaces Pi/npm homes and configuration, strips inherited credential environment variables from child processes, uses offline npm operations and a loopback dry-run registry, sends no prompt, and issues no provider request. These controls prevent test use of the configured Pi/npm authentication locations; they are not an operating-system sandbox or secure-erasure claim.

Slash help runs without a subprocess; doctor/list/status expose only bounded metadata. Slash start preserves interactive preview, parent-trust checking, child-bypass confirmation, and final start confirmation. Controller-mode starts additionally require an explicit target project rather than defaulting to the controller's neutral workspace. Controller identity environment variables are removed before every worker Pi exec so controller-only session gates cannot leak into role sessions. Slash send obtains session/role/message through Pi's built-in dialogs and transfers the message only through the existing unique mode-`0600` file path; message text is excluded from argv, status, details, notifications, and widgets and the temporary directory is removed in `finally`. Slash stop requires exact-session input and explicit confirmation before delegated `--yes`. Attach and restart remain terminal-only.

## Relay delivery boundary

The relay requires each ready marker's matching report to be a regular non-empty file. Playwright, Django, and reviewer reports also require their documented first-line enums. Invalid or missing reports remain pending. Successful transport is recorded per marker and enabled recipient so retries do not intentionally duplicate already successful recipients; a marker is globally complete only after all intended recipients succeed.

A successful tmux `send-keys` operation proves only that tmux accepted the keystrokes. It does not prove Pi received, processed, or acknowledged the notice. Same-user processes can inspect or alter tmux and coordination state; these checks are hardening, not same-user sandboxing.

## Project trust

`--approve-project` passes Pi's trust bypass to child sessions. Use it only after inspecting and trusting the target project. Without it, each child Pi session must obtain trust interactively. The extension rejects this bypass unless the parent context reports the project trusted and the user separately confirms it for that run. The parent decision is never represented as automatically applying to children. Extension starts are rejected outside the interactive TUI when confirmation is unavailable.

## Destructive operations

The CLI:

- refuses to replace an existing tmux session
- reserves one exact controller tmux name, verifies private controller markers/state before attach or stop, refuses duplicate or unrelated-name collisions, and blocks in-TUI session switching/forking away from the fixed controller ID
- uses exact tmux targets for every operation on an existing session/window, including attach, status, stop, and failure cleanup
- requires `--yes` to restart a role
- requires `--yes` to stop a worker grid and `--confirm` to stop the controller
- retains coordination records and the dedicated controller Pi conversation after session termination
- kills a partially created tmux session after startup failure and leaves a private `FAILED` startup state when safe

It does not push, merge, publish, deploy, or clean target repositories.
