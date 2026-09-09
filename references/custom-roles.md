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

## Worker contract boundary (partial #60 implementation)

The protocol validators now accept custom identities only when explicitly given
a bounded trusted identity-to-contract map. A worker frame cannot register a role
or choose its own contract. Without that map, custom messages and reports remain
rejected. Public starts and live broker routing are not wired to custom roles
yet; this is not end-to-end launch support.

The shared worker bridge supports an internal, launch-bound
`PI_TMUX_ORCHESTRATOR_SPECIALIST_CONTRACT` (`probe`, `playwright`, or `django`).
A valid `custom-*` identity requires that binding; built-in identities reject it.
Both launchers strip ambient values and set the contract only after fresh
resource verification. It is not a user-facing launch workaround.

Custom workers retain their custom identity in reports. Only the bound specialist
report kind is permitted, including after assignment restoration. They cannot
submit implementation/plan/reviewer reports, changed paths, or final approval.
Their fixed tool set is `read,grep,find,ls,orchestrator_report`: shell, edit/write,
browser execution, and unapproved extension tools are filtered at activation and
blocked again at tool-call time. This is a tool-policy boundary, not an OS sandbox;
no shell-based verification is available to custom workers in this slice.

Model-free bridge tests cover assignments, reports, restoration rejection, and
write-tool denial. The isolated actual-Pi smoke checks RPC startup and the active
tool set without prompts/provider calls. Full custom-role tmux/TUI/RPC orchestration,
routing, usage, and recovery acceptance remain #60.

## Resource-bound bootstrap and retained metadata (partial #60)

Manifest v6 can retain a pinned `custom_role_registry` path and at most eight
custom worker records. Each custom record has a canonical `custom_role` definition
(`id`, `contract`, `prompt`, `skills`) instead of independently overridable skills,
and the exact `read,grep,find,ls` tool policy. It still requires the built-in
implementer and reviewer. Manifest v1–v5 remains supported, and ordinary starts
still write v5. No new public start selection is exposed in this slice. Broker
initialization and recovery explicitly reject custom worker sets until the remaining
public selection and lifecycle acceptance land.

Retained reads validate bounded metadata without opening the registry/resources;
a deleted or changed source does not make retained metadata unreadable. This is
not evidence that resources are still valid. Launch and restart preflight reopen
the pinned registry, compare every selected definition exactly, and verify current
resource bytes and digests. A changed contract, identity, path, skill list, missing
file, unsafe permission, link, or changed digest fails closed. New environment or
global registry defaults cannot redirect an existing binding. Restart preflight
runs before manifest mutation, broker handover, or killing the worker; actual
bootstrap verifies again rather than trusting preflight results.

Both presentations consume owner-only, fixed-slot snapshots of freshly verified
Markdown, not mutable external paths reopened later by Pi. There is one generated
system prompt and at most four skill snapshots per custom role in the private
coordination directory; repeated launches replace those slots atomically. These
are private worker bootstrap inputs, not broker task/report handoffs. Resource
bodies never enter manifests, SQLite, public status, or Supervisor projections.
Only the selected Markdown is copied: neighboring files, executable attachments,
and relative-path dependencies are not copied or implicitly authorized.

Custom bootstrap disables automatic extension, skill, and prompt-template discovery.
Only the trusted worker extension and verified snapshot skills are explicitly loaded;
project instructions and the separate child trust boundary remain enabled. This
prevents discovered extensions from replacing a supposedly read-only built-in tool.
The actual-Pi RPC smoke covers snapshot skill discovery after source mutation,
fixed tools despite skill `allowed-tools` claims, and disabled global/project
extension discovery. It does not exercise full custom broker lifecycle acceptance.

## Gated broker workflow (partial #60)

The internal broker workflow now derives an identity-to-contract map from a fully
validated retained manifest, never from a worker frame or current environment.
Retained contract reads do not imply current resource validity: launch/restart
verification above is still mandatory. Inbound hello/report validation and outgoing
and restored assignment kinds use that binding. Custom identities cannot select a
contract, impersonate another authenticated identity, authorize operator control with
a worker token, or submit writer/reviewer reports.

At the tested workflow boundary, every explicitly selected custom specialist gets
one assignment after each implementation report, not after a phased plan. All
selected custom reports for that round are required before assigning the built-in
reviewer. Specialist verdicts are evidence for that independent reviewer, never final
acceptance. No custom skip predicate, profile mapping, or deterministic activation
policy is introduced; those remain #61. Fan-out is bounded by the eight-role registry
limit. Run-state capsules reserve bounded space for every selected identity without
dropping independent reviewer evidence; reuse hints remain non-authorizing.

Custom report acceptance uses the existing single SQLite transaction for assignment
completion, report metadata, and cumulative/assignment usage. Replayed accepted
reports neither account twice nor reroute, including after in-memory evidence loss.
Routing failures become uncertain rather than ready. Recovery rejects an assignment
kind inconsistent with the retained contract before sending or mutating handover
state. Synthetic regressions cover handler authentication, stale generations,
rollback, bounded projections, all three contracts, eight-role fan-out, and repeated
review rounds. SQLite/public snapshot tests exclude private report/resource bodies.

**This is not public start or full lifecycle support.** Both temporary custom-role
rejection gates remain. The tests use the lower workflow/storage boundary and
synthetic streams, not custom tmux/TUI/RPC runs. Public selection and custom
partial-start/disconnect/restart TUI/RPC and actual-Pi acceptance must land before
removing the gates. No provider
savings or full lifecycle acceptance is established by these tests.

## Retained control and presentation surfaces (partial #60)

Control parsers (`send`, `abort`, `restart`, `events`, and Supervisor event/command
reads) accept canonical custom identities, but syntax does not enable a role:
the exact target manifest must contain it. Restart still requires `--yes`. The
broker independently revalidates custom resources and its bound contract before
changing generation or handing over a worker; CLI preflight and bootstrap checks
remain in place. An accepted command retry acknowledges the existing command,
not permission to launch again with revoked resources. Failure notifications can
still mark a revoked-resource handover uncertain.

`/or-send` obtains its bounded role picker from the exact run's status projection,
not the built-in catalog or current global registry. The model tool accepts the
same bounded identity syntax; the CLI remains authoritative for membership.
No custom model/profile/skill/start override fields are added by these adapters.

Custom public role records expose only `specialist_contract`, the fixed
`custom-read-only-no-shell` tool policy, and `resource_verification: not_checked`
in addition to ordinary role metadata. Status and Supervisor reads never reopen
resources or claim current validity. Supervisor runtime liveness remains
`not-observed`. Resource paths/digests and prompt/skill/report bodies are not added
to these surfaces. The broker pane uses stacked rows for long custom identities,
a read-only badge, and an omission count in short panes rather than colliding
11-column names. At most thirteen worker identities are represented.

The parent observer pins its selected identities/contracts from the CLI status or
start projection before connecting. Snapshots and reports cannot register roles
or replace that binding. Custom reports must match the specialist contract and
cannot contain changed paths or writer/reviewer verdicts. Duplicate snapshot
identities fail closed; `restarting` and `recovering` remain distinct states.
Legacy envelopes without role metadata remain built-in-only. Parent updates stay
bounded (192 × 1024 JavaScript string units for reports, 8 × 1024 for progress); writer and
independent reviewer evidence is ordered before specialist fan-out so custom
reports cannot displace it. Oversized omitted reports are reported honestly.

Tests exercise retained reads with deleted resources, authenticated control with
real SQLite and fake transport, and custom observer events over a local synthetic
Unix-socket peer. These establish adapter behavior, not full custom worker
TUI/RPC lifecycle acceptance. Both start gates remain; #60 stays open.

## Filesystem trust boundary

The registry and every resource must be outside the canonical target project,
owned by the current user, nonempty regular files with one hard link, and not
writable by group/others. Symlinks are rejected at **every path component**.
Ancestor directories must be owned by the current user or root and not writable
by group/others, except root-owned sticky directories such as `/tmp`.

The reader walks directories through file descriptors with no-follow flags,
rejects directory identities matching the target project (including case aliases),
checks file metadata before and after bounded reads, rejects FIFOs/devices,
invalid UTF-8, excess size, and detected concurrent changes, and closes all
handles. Use private directories and mode-`0600` files for private guidance.
Registry validation copies no bodies. Worker bootstrap creates only the bounded
private launch snapshots described above; status and retained control metadata
remain body-free.

The built-in implementer remains the only writer and the built-in reviewer
remains mandatory. Registering a specialist does not enable it or satisfy review.
There is no installation, global configuration mutation, release, or deployment
step in this feature.
