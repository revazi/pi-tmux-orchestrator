# Custom specialist registry

The version-1 registry defines bounded, user-owned **read-only specialists**.
The registry command is validation-only. Explicit custom selections can now be
previewed through CLI `start --dry-run`, but cannot yet be launched or activated.
Built-in start, model, profile, report, and worker policies are unchanged.
Broker/worker integration is tracked in #60 and profile/activation integration in #61.

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

## Preview an explicit selection (partial #60)

After reviewing and registering the exact Markdown resources:

```sh
pi-tmux-agents --json start --project /absolute/target/project \
  --task 'Inspect the requested change without editing.' \
  --custom-role custom-security EXACT_PROVIDER EXACT_MODEL off \
  --role-registry /absolute/user-owned/roles.json \
  --dry-run --skip-model-check
```

Replace `EXACT_PROVIDER` and `EXACT_MODEL` with available exact identifiers. Each
repeatable `--custom-role` consumes **four** values: ID, provider, model, thinking.
All are required, with at most eight unique registered IDs. Provider/model strings
are bounded to 256 printable, non-whitespace characters and cannot start with `-`;
thinking uses the existing supported levels. No model/profile/project default is
inherited for custom roles, including defaults for their specialist contract.
`--role-registry` requires a selection and uses the same precedence and filesystem
policy as registry validation. Omission of `--custom-role` never reads the registry.

TUI and `--rpc-workers` previews use the same bindings and fixed read-only policy,
and always retain the built-in implementer and reviewer. Custom skills come only
from the verified registry definition; `--worker-skill`, `--worker-context`, and
`--force-specialist` do not gain custom mappings. Profile mappings and deterministic
custom activation remain #61. This is a CLI preview, not a new Pi start-tool field.

Every preview rereads the registry/resources and verifies their digests. Successful
JSON includes bounded identities, explicit models, report contracts, fixed tool
policy, and registry-bound skill counts, not resource paths, digests, or bodies.
`custom_role_selection.launch_supported` is **false** and its
`resource_verification: checked_at_selection` is only a point-in-time observation;
ordinary public role metadata remains `resource_verification: not_checked`, never
claiming retained launch authorization. Nothing is persisted or launched. The
normal CLI dependency/session checks still run; without `--skip-model-check`, Pi
model-catalog discovery also runs. Skipping that check does not prove availability,
and discovered custom-provider extensions may be unavailable to discovery-disabled
custom workers. The orchestrator does not submit an inference prompt during a preview.

Without `--dry-run`, any custom selection fails with `custom_start_not_enabled`
before dependency checks, resource reads, state creation, or tmux mutation. Both
lower broker gates also remain. These planning regressions do not themselves
establish the connected lifecycle evidence documented below; #60 stays open
until the gates are reviewed separately.

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
tool set without prompts/provider calls. Connected actual-Pi startup and recovery
coverage is documented below; complete report generation remains outside the
provider-free boundary.

## Resource-bound bootstrap and retained metadata (partial #60)

Manifest v6 can retain a pinned `custom_role_registry` path and at most eight
custom worker records. Each custom record has a canonical `custom_role` definition
(`id`, `contract`, `prompt`, `skills`) instead of independently overridable skills,
and the exact `read,grep,find,ls` tool policy. It still requires the built-in
implementer and reviewer. Manifest v1–v5 remains supported, and ordinary starts
still write v5. Public selection is currently preview-only and writes no manifest.
Broker initialization and recovery explicitly reject custom worker sets until
connected lifecycle acceptance and live-start integration land.

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
extension discovery. The separate staged-package lifecycle smoke below connects
actual Pi to the broker without submitting an inference prompt.

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
synthetic streams, not custom tmux/TUI/RPC runs. Live public starts and custom
partial-start/disconnect/restart TUI/RPC and actual-Pi acceptance must land before
removing the gates. No provider
savings or full lifecycle acceptance is established by these tests.

## Connected broker regression boundary (partial #60)

`tests/test_custom_broker_lifecycle.py` runs the real broker server on its private
Unix socket with real SQLite and explicit synthetic worker peers. Setup first
asserts that both public initialization and broker construction reject custom
roles, then bypasses only the constructor catalog gate within the test. No
production gate or authentication, framing, routing, control, or recovery method
is disabled. The tests do not invoke the public start/launcher path.

Connected regressions cover:

- All three specialist contracts through implementation, custom evidence, and
  mandatory independent review; specialist findings cannot approve the run.
- Partial worker connection, wrong-role tokens, duplicate connections, invalid
  generations, and authenticated reviewer impersonation rejection.
- Accepted-assignment reconnect with stable assignment/delivery identity;
  unacknowledged delivery reconnect becomes uncertain rather than replaying work.
- Authenticated restart with fresh resource verification, generation advancement,
  old-generation rejection, new delivery identity, and idempotent control retry.
- Disconnect during restart delivery preserves uncertainty; revoked resources
  reject restart without disconnecting or mutating the active worker.
- Report replay after reconnect and in-memory evidence loss neither accounts nor
  routes twice; task, report, and resource canaries are absent from SQLite.

This is connected **broker-only** evidence. Synthetic peers explicitly
acknowledge/report; no worker extension, tmux pane, supervisor, provider, or
bootstrap is exercised. The separate model-free tmux boundary below adds launcher
coverage without turning these peers into actual-Pi evidence.

## Internal launch and model-free tmux boundary (partial #60)

The shared start manifest constructor now has a strict body-free manifest-v6 path
for explicit custom selections. It requires exact role/config membership and a
registry if and only if custom identities are selected. Ordinary starts retain
manifest v5. The public CLI gate still precedes selection, so this internal path
cannot make a live custom start available by itself.

`tests/custom_worker_tmux_smoke.py` constructs that internal v6 projection and
runs real tmux panes through the production `_run-agent` entry point in both TUI
and RPC modes. A bounded model-free Pi host loads the real JavaScript worker
extension and supplies only its documented registration/event surface; the
extension owns broker-v1 framing, delivery acknowledgement, assignment state,
and report submission. The broker also runs in its real monitor-pane subprocess;
a test-only entry shim bypasses only its temporary custom-role catalog gate after
validating the selected `custom-*` identity. SQLite, the TUI launcher, RPC
supervisor, tmux respawn, resource verification, snapshot creation, and mandatory
workflow routing remain active. It verifies:

- Exact launch-bound identity/contract, extension-enforced active no-shell tools,
  disabled automatic extension/prompt/skill discovery, and one explicit trusted
  extension/skill.
- Private system-prompt and skill snapshots for both presentations without task,
  report, prompt, or skill bodies entering SQLite.
- Implementation-to-custom-to-independent-review routing through terminal approval.
- Authenticated custom restart with generation advancement and a second verified
  TUI/RPC process; resource revocation rejects restart without replacing or
  disconnecting the healthy worker.
- Accepted active assignments retain exact assignment/delivery identity across a
  broker-process replacement, remain active after the extension's reconnect
  lifecycle/duplicate acknowledgement, and report exactly once before mandatory
  review.
- A live stale generation repeatedly fails authentication after restart admission.
  Terminating generation 2 before its replacement delivery acknowledgement marks
  the handover uncertain; reconnecting that exact generation does not replay it,
  route review, account usage, or accept a report. Uncertainty and the absence of
  replay survive a further broker-process replacement in both TUI and RPC.
- Exact monitor-pane broker termination and respawn after readiness; every worker
  reconnects to the replacement process while retained custom generation,
  authority, and terminal workflow state remain unchanged. Exact session cleanup
  waits for broker socket removal and durable worker disconnects.
- Manifest-v6 startup does not become `RUNNING` until the exact broker and every
  selected worker pane remain live and authenticated through a bounded stability
  window. Selection-to-launch resource revocation fails admission in TUI and RPC,
  marks the retained run `FAILED`, removes the broker socket and exact new tmux
  session, and leaves a prefix-colliding existing session unchanged. Unit coverage
  also bounds one- and eight-custom-role fan-out and failures before grid creation.

The real-extension sequence exposed two lifecycle ordering requirements now
covered at the broker boundary for every role: an ordinary startup lifecycle
cannot erase `recovering` or durable `uncertain`, and an accepted assignment
acknowledgement restores an otherwise idle/disconnected reconnect to `active`.
Only the assignment acknowledgement completes a prepared handover.

This boundary is **not actual Pi or provider evidence**: the synthetic host loads
the real extension but does not implement Pi's full runtime or issue model
requests. The distinct actual-Pi boundary below now covers connected startup and
recovery, but intentionally does not claim a generated report or provider behavior.
#60 remains open for separately reviewed gate removal; #61 stays separate.

## Connected actual-Pi boundary (provider-free #136)

`tests/actual_pi_custom_lifecycle.py` runs the actual installed Pi executable for
the custom worker in real tmux TUI and RPC panes while the built-in workers remain
bounded model-free peers. It is invoked against the exact locally staged npm
package from `scripts/test.sh`, not an installed global package. The process uses
disposable HOME, XDG, npm, Pi, state, session, and tmux directories under an
`env -i` allowlist. A literal non-secret local model entry permits idle startup;
a loopback request sentinel and Pi offline mode require zero provider requests.
No operator auth or package settings are read or changed.

For both presentations the smoke requires the staged worker extension to
authenticate the exact custom identity/contract, consume the freshly verified
prompt and skill snapshots, retain the fixed
`read,grep,find,ls,orchestrator_report` tool argument, and disable automatic
extension, prompt-template, and skill discovery. A deliberately discovered
extension must not execute. It then confirms a generation-advancing worker
restart, old-process exit, unchanged live worker on resource-revoked restart,
broker-process disconnect/reconnect without replacing Pi, metadata-only SQLite,
zero provider usage, and exact tmux/process/socket cleanup. The RPC supervisor
must additionally complete an actual Pi `get_state` exchange.

This is actual-Pi **startup, bridge, and recovery** evidence, not a complete report
round: assigning work with `triggerTurn` would require model inference. No model
prompt is sent and `orchestrator_report` is not synthesized through a fake
provider. Any provider-backed report/quality acceptance remains a separately
authorized step. The existing connected model-free boundaries continue to cover
report normalization, routing, mandatory independent review, stale generations,
and uncertain handovers. Public and lower custom live-start gates remain intact
until #137 reviews their removal.

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
