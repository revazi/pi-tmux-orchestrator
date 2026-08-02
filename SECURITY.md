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
- does not provide sandboxing between agents running as the same user

Only the implementer is given normal Pi write tools. Reviewer and probe omit `edit` and `write`, but retain `bash` to run tests. Their read-only policy is therefore a workflow boundary, not an operating-system sandbox.

## Private data

Never put these values in task, handoff, review, probe, status, test, or issue content:

- API keys, OAuth tokens, Keychain values, or authentication files
- private résumé, customer, production, or business data
- raw provider requests or responses
- system prompts or proprietary prompt payloads
- private endpoints
- unbounded raw errors

Coordination directories are created with mode `0700` and files with mode `0600`. Operators remain responsible for local backups, screen recording, shell history, and tmux server access.

## Project trust

`--approve-project` passes Pi's trust bypass to child sessions. Use it only after inspecting and trusting the target project. Without it, each child Pi session must obtain trust interactively.

## Destructive operations

The CLI:

- refuses to replace an existing tmux session
- requires `--yes` to restart a role
- requires `--yes` to stop a session
- retains coordination records after session termination

It does not push, merge, publish, deploy, or clean target repositories.