# Custom specialist registry

The version-1 registry defines bounded, user-owned **read-only specialists**.
This registry implementation is validation-only: custom roles cannot yet be
selected, launched, or activated. Built-in start, model, profile, report, and
worker policies are unchanged. Broker/worker integration is tracked in #60 and
profile/activation integration in #61.

## Validate without starting workers

```sh
pi-tmux-agents role-registry --project /absolute/target/project
pi-tmux-agents --json role-registry --project /absolute/target/project \
  --registry /absolute/user-owned/roles.json
```

Precedence is explicit `--registry`, then
`PI_TMUX_ORCHESTRATOR_ROLE_REGISTRY`, then
`$PI_CODING_AGENT_DIR/tmux-orchestrator-roles.json` (normally
`~/.pi/agent/tmux-orchestrator-roles.json`). Paths must be absolute and canonical;
there is no project-local lookup, merge with model configuration, or resource
discovery. A missing default file means zero custom roles. An explicitly selected
missing file is an error. Existing registry files must use version 1; unknown or
legacy versions are rejected rather than guessed or rewritten.

Validation is local and read-only: no tmux, Pi, provider, model resolution, hooks,
or role startup is invoked. JSON returns bounded definition metadata and
`launch_supported: false`, never prompt/skill bodies. Failed validation emits no
partial definitions. The command does not install or approve anything.

## Definition format

Example shape (replace the placeholder paths and digests after reviewing the
exact resource content):

```json
{
  "version": 1,
  "roles": [
    {
      "id": "custom-security",
      "contract": "probe",
      "prompt": {
        "path": "/absolute/user-owned/security.md",
        "sha256": "REPLACE_WITH_REVIEWED_FILE_SHA256"
      },
      "skills": [
        {
          "path": "/absolute/user-owned/security-skill.md",
          "sha256": "REPLACE_WITH_REVIEWED_FILE_SHA256"
        }
      ]
    }
  ]
}
```

After reviewing a resource, `shasum -a 256 /absolute/path.md` prints its digest.
Put that exact lowercase 64-character digest in the registry; the validator does
not generate or silently update approved digests. Every validation rereads and
checks resources. Changed or missing content fails closed. Validation is an
observation at that time, not permission to use subsequently modified resources;
future launch/recovery integration must revalidate them.

Limits and fields:

- At most **8** definitions in a **64 KiB** registry; duplicate JSON keys and IDs
  are rejected. No unknown fields are accepted at any level.
- IDs are at most **32 characters**, begin with `custom-`, and use lowercase
  letters/digits separated by single hyphens. The suffix starts with a letter.
  Built-in roles and authority/target aliases are reserved even with that prefix
  (for example `custom-reviewer`, `custom-parent`, and `custom-broker`). No tmux
  target punctuation, whitespace, or alias substitution is accepted.
- `contract` is exactly `probe`, `playwright`, or `django`, identifying an existing
  built-in read-only specialist report contract, never writer/reviewer authority.
- One required UTF-8 Markdown prompt, at most **16 KiB**; optional `skills` contains
  at most **4** UTF-8 Markdown resources, each at most **32 KiB**. Paths are unique
  within a definition, including the prompt. Each resource has exactly `path`
  and `sha256` fields. Paths are at most **1024 characters**.
- Provider/model/thinking settings remain separate. Credentials, tools, arbitrary
  report schemas, extensions, activation rights, and authority fields are rejected.
  Prompt/skill text cannot grant tools or replace required independent review.

## Filesystem trust boundary

The registry and every resource must be outside the canonical target project,
owned by the current user, nonempty regular files with one hard link, and not
writable by group/others. Symlinks are rejected at **every path component**.
Ancestor directories must be owned by the current user or root and not writable
by group/others, except root-owned sticky directories such as `/tmp`.

The reader walks directories through file descriptors with no-follow flags,
checks file metadata before and after bounded reads, rejects FIFOs/devices,
invalid UTF-8, excess size, and detected concurrent changes, and closes all
handles. Use private directories and mode-`0600` files for private guidance.
No registry or resource body is written to coordination state or status output.

The built-in implementer remains the only writer and the built-in reviewer
remains mandatory. Registering a specialist does not enable it or satisfy review.
There is no installation, global configuration mutation, release, or deployment
step in this feature.
