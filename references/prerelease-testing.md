# Pre-release extension testing

This guide tests the exact local `pi-tmux-orchestrator` checkout before any
version bump, tag, release, or publication. Pi packages execute with full system
access. Review the branch, commit, package allowlist, extension, skill, and Python
CLI before running it.

## Acceptance layers

Keep these results distinct:

1. **Package provenance** proves which Git checkout produced the tarball and
   staged package.
2. **Provider-free actual-Pi acceptance** proves Pi loads the staged extension,
   commands, model tool, and skill, and connects staged-package custom TUI/RPC
   workers from a fixed accepted planning record through startup, broker
   reconnection, restart, revocation, retained provenance, and cleanup without
   sending a prompt. It does not exercise planner inference or claim generated
   reports.
3. **Local tmux acceptance** exercises fixed accepted v10/v11 manifests across TUI/RPC
   broker panes, routing, rollback, restart, cleanup, and retained metadata;
   model-free peers cover report paths.
4. **Provider-backed acceptance** measures real model behavior and usage. It can
   incur cost and must be explicitly chosen.

Model-free fixtures and serialized-byte/operation counts are proxies. They do
not prove provider cost, cache behavior, reviewer quality, or production-wire
acceptance. The worker-prompt `--check` gates the lean `--system-prompt`
contract and orchestrator-owned reviewer prompt size, not Pi's default coding
prompt length. The suite also validates the strict user-global planner-policy
parser/projection and selection against synthetic available/scoped catalogs;
these checks make no provider call and do not identify Jev.

For the next stable candidate, run this guide as part of the explicit
[release gate #139](https://github.com/revazi/pi-tmux-orchestrator/issues/139)
only after engineering roadmaps
[#101](https://github.com/revazi/pi-tmux-orchestrator/issues/101) and
[#58](https://github.com/revazi/pi-tmux-orchestrator/issues/58) close. Staging a
package or completing this guide does not select a version or authorize tagging
or publication; `RELEASE.md` remains the maintainer checklist.

## Prerequisites

- macOS or Linux
- Python 3.11+
- Node.js 22.19+ and npm
- Pi available as `pi`
- tmux 3.2+; 3.5+ recommended
- a reviewed, clean checkout for the default staging command

Run the normal suite first:

```bash
scripts/test.sh
```

For a reviewed unified configuration outside the target project, the metadata-
only planner projection can be inspected without contacting a provider:

```bash
PI_TMUX_ORCHESTRATOR_CONFIG=/absolute/path/tmux-orchestrator.json \
  pi-tmux-agents --json planner-policy --project /absolute/project
```

The retired `PI_TMUX_ORCHESTRATOR_PLANNER_CONFIG` override and any existing
standalone planner file cause dynamic planning to fail closed with migration
instructions. Do not treat either as active fallback policy; migrate values
explicitly into the unified file and remove the old file/override.

The output must contain only the config path, configured flag, and normalized
canonical identity/thinking policy plus guidance status/digest/body projection—never
credentials, endpoints, task bodies, or provider response bodies. Public
confirmation and retained planning state continue to omit guidance text. The corresponding topology projection can be
inspected without a provider call:

```bash
pi-tmux-agents --json planner-topology --project /absolute/project
```

The private topology v2 projection contains fixed built-in constraints, the
reviewed exact worker-pool policy, and verified custom identity/contract/model
metadata—never registry/resource paths or bodies. This is a private preflight
input, not a public status or handoff: do not copy the pool/configuration body
into reports. Use `--no-project-custom-roles` to confirm the custom candidate list
is empty and that no registry resources are read.

When Pi is available, this invokes
`tests/actual_pi_custom_lifecycle.py` against the exact disposable package staged
by the suite. The test clears its child environment, uses isolated Pi/npm/XDG/tmux
state and a non-secret local model catalog, and fails if its provider-request
sentinel receives traffic. It covers actual-Pi custom TUI/RPC startup and recovery,
not inference or a complete report round. If Pi is unavailable, that layer is
reported as skipped rather than replaced by synthetic evidence.

### Automated provider-free task-intent gate

Stage 1 accepts exactly `change|investigation|review|advisory` on model-tool,
slash, and terminal starts. Verify explicit non-change redirection/cancellation
before planner calls, trust bypass, private launch files, tmux, broker, workers,
and manifests in both TUI/RPC presentations. `--yes` cannot convert non-change
into coding. Check intent-selection and final-confirmation cancellation; ordinary
static change starts must have no classifier call and retain one writer plus
mandatory review. The existing dynamic call's bounded recommendation must be
shown, subordinate to explicit intent, and bound in planning-v4/v10–v11 manifest
metadata. Check every enum, omitted-versus-explicit precedence, malformed values,
stale/tampered preview rejection, retained reads, and body redaction. Legacy
intent remains unavailable, not inferred from report kind. These are synthetic
admission/control-plane checks, not live-provider inference or production-wire
acceptance. No advisory assignment/report workflow is added.

### Accepted decision-evidence gate (Unreleased)

Issue #193 uses **model-free synthetic fixtures only**; do not repeat the stopped
#174 direct-Jev attempt or infer its missing provider response. Run focused
`PYTHONPATH=tests python3 -m unittest test_planning test_planner_evidence` and
`node --test tests/extension.test.mjs`, then the pinned full `scripts/test.sh`
(Ruff 0.11.11), exact package verification/smoke, measured Node coverage and
Fallow 3.22.0 identity-baseline/changed-file gates, and `git diff --check`.

Check planning-v5/evidence-v1 preview, final TUI/RPC confirmation, persisted
manifest v10/v11 reopen, human/JSON status, list/Supervisor summaries versus exact
snapshots, dashboard, and parent final evidence. Confirm exact supplied facts,
binary64 digest agreement (including tiny declared rates), selected confidence
versus probability, independent model/thinking axes, canonical top-three ties,
fixed/singleton authority, Pi probability unavailability, all seven scope
combinations/locks, trusted custom specialists, omitted roles, existing suitability
questions, homogeneous/mixed provider `rationale_unavailable`, and immutable
launch assignments. Malformed/duplicate/non-normalized/oversized/fact-mismatched
and stale/tampered evidence must cause no launch. Static/planning-v1–v4 evidence
is unavailable, not reconstructed. Test sentinels for every private body surface;
no model/provider traffic, credentials, raw errors, or endpoint retention is
necessary. Synthetic wire/schema/package checks are not production acceptance,
quality, cost-savings, or a diagnosis of #174. Keep version 0.11.1, static defaults,
and historical benchmark/release notes unchanged; obtain independent approval
before opening a focused PR.

### Automated provider-free planning gate

Approved-pool migration is a new fail-closed dynamic-start gate: upgrade the
external configuration to version 5 and add `workerCandidates` version 1, or pin
every eligible role exactly. Do not use model-name heuristics or a full-catalog
fallback. Ordinary starts and older retained runs remain supported.

Provider-free fixtures must cover 1/32-identity pool boundaries, the 100-distinct
union bound, built-in role replacement, duplicates/malformed/unavailable/scoped
identities, run/project/global locks over pools, unchanged custom bindings,
unlocked missing-pool no-call/no-launch, and fully locked no-pool planning. Verify
that model-tool, `/or-start`, and terminal failures all preserve fixed actionable
version-5 pool/exact-lock guidance while unrelated raw errors remain redacted. Capture
synthetic direct Jev and Pi inputs to compare canonical candidate identities,
capabilities, and per-role eligibility. Prove an available-but-unapproved model
is never offered or accepted (including cross-role pool violations), and that a
Pi decision model outside the worker pool remains usable without becoming a
worker candidate. Mutate pools/source and approved capability facts after preview
and require no launch; revalidation must use original operator locks, not the
planner's generated assignments. Check bounded source/count and every exact
selected assignment in final confirmation and immediate acknowledgement, including
maximum-length identities. Check redaction, planning-v1 retained readability,
new-launch rejection of legacy records, and static-start compatibility. These
synthetic tests establish no production-wire approval for this policy.

`scripts/test.sh` is the authoritative model-free gate. Its planning coverage
includes strict preferred/fallback and scoped-catalog selection; minimum/maximum
built-in and trusted-custom topologies; malformed and oversized responses;
unsupported thinking, unavailable or ambiguous models, duplicate/excess roles,
stale task/config/topology/catalog/resource bindings, declined confirmations,
and planner/RPC timeout failures. Fake completion fixtures exercise strict parsing
and shared start admission but are not production inference evidence.

The real-tmux layer injects a fixed accepted decision, binds it through dry-run
and launch, and exercises manifest v10/v11 TUI/RPC startup, custom startup rollback,
broker and worker restart, exact cleanup, and retained status/Supervisor reads.
The staged actual-Pi layer repeats custom TUI/RPC lifecycle acceptance with a
local no-inference catalog and a network request sentinel. Security assertions
keep task/context/custom-resource bodies out of argv, manifests, SQLite, status,
dashboards, and Supervisor projections; require owner-only temporary files;
retain one writer plus mandatory built-in review; and revalidate target-project
custom trust before restart. These checks do not access real credentials or
endpoints and do not establish Jev availability, planner quality, worker quality,
provider cost, or latency.

## 1. Stage the exact local package

Choose a new persistent directory outside the checkout:

```bash
cd /absolute/path/to/pi-tmux-orchestrator
COMMIT=$(git rev-parse --short=12 HEAD)
STAGE="$HOME/pi-prerelease/pi-tmux-orchestrator-$COMMIT"
mkdir -p "$(dirname "$STAGE")"
scripts/stage-prerelease.sh --output "$STAGE"
```

The command refuses a dirty checkout by default, refuses existing/noncanonical
or in-repository output paths, runs exact package verification, creates the
actual npm tarball with scripts disabled and offline npm configuration, installs
that tarball under the stage, and runs isolated Pi RPC package discovery. It
does not publish or modify the real Pi home, npm home, settings, or auth.

`--allow-dirty` exists only for local script development/smoke. A dirty artifact
is marked `source_state=dirty` and must not be treated as a commit build.

Inspect the bounded provenance:

```bash
python3 -m json.tool "$STAGE/provenance.json"
```

Expected fields include the Git commit/tree, source state, tarball and installed
package-tree SHA-256 values, package name/version, relative tarball/package-root paths, and
`"published": false`. Validate the retained stage again at any time with:

```bash
scripts/run-prerelease-isolated.sh --stage "$STAGE" --check
```

The package keeps the current unreleased package version; the Git commit and
tarball digest identify this pre-release build.

## 2. Provider-free isolated TUI

Use an explicit project you have inspected:

```bash
scripts/run-prerelease-isolated.sh \
  --stage "$STAGE" \
  --project /absolute/path/to/inspected-project
```

The runner validates provenance and the tarball digest, creates disposable
HOME/XDG/npm/Pi directories with no real authentication, disables discovered
extensions/skills, and loads only the staged package with Pi's explicit `-e`
path. It uses offline/update-disabled and blackhole proxy settings as defense in
depth; these are not an OS network sandbox.

In the TUI, do not send a model prompt. Check:

- `/or-dashboard` opens with no running sessions, concise help, and an About
  footer with version, repository, issues, npm, and contribution details, with
  no automatic doctor invocation;
- pressing `d` in the dashboard runs doctor with paths under the disposable environment;
- `/or-models` returns bounded model metadata without credentials;
- command completion contains only `/or-dashboard`, `/or-models`, `/or-start`,
  `/or-send`, and `/or-stop` from the extension;
- the `tmux_orchestrator` model tool and `tmux-agent-orchestrator` skill appear
  as resources from the staged package;
- `/or-start` reaches its bounded preview/confirmation path; cancel before the
  confirmed start because this environment has no provider authentication.

Exit Pi normally. The runner deletes only its disposable environment; the stage
remains for repeat tests.

## 3. Optional provider-backed manual acceptance

This step uses the operator's normal Pi models/authentication and can incur real
provider cost. Perform it only after reviewing the staged provenance and deciding
to spend provider usage.

Resolve the staged package root:

```bash
PACKAGE_ROOT=$(python3 - "$STAGE" <<'PY'
import json
from pathlib import Path
import sys
stage = Path(sys.argv[1])
value = json.loads((stage / "provenance.json").read_text())
print(stage / value["package_root"])
PY
)
```

Record installed settings before the test:

```bash
pi list > /tmp/pi-packages-before.txt
```

Launch Pi with normal authentication but without discovered extension/skill
packages. The explicit staged package remains temporary for this process and is
not added to settings:

```bash
cd /absolute/path/to/inspected-test-project
pi --no-extensions --no-skills -e "$PACKAGE_ROOT" --no-session
```

Run the smallest useful matrix rather than every expensive combination.
Provider-backed planning evaluation is **not authorized by this guide**. Before
making any call, record separate owner approval and freeze this paired benchmark:

| Case | Fixed task class | Static/manual arm | Dynamic arm |
| --- | --- | --- | --- |
| 1 | simple bounded change with no specialist need | built-in implementer + reviewer | planner may not remove either built-in role |
| 2 | one clearly specialist-relevant change | same explicitly enabled specialist candidate set | planner chooses from that exact set |
| 3 | ambiguous cross-cutting change | all reviewed optional candidates available | planner chooses the smallest accepted roster |

Dynamic planning considers only exact operator-approved worker pools (root config
v5 / workerCandidates v1), or fully authoritative role locks, validated against
Pi's available/scoped catalog. Freeze the same approval policy for both arms;
do not infer recency or quality from model names. Catalog rates are not
observed spend and must not fill a savings or quality cell.

Use the same reviewed project revision, task class, profile, model availability,
budget policy, required checks, and acceptance rubric for both arms. Run one pair
per case first; any repeat or broader matrix needs fresh approval. Record only
the bounded facts listed in section F plus planner latency, corrections, invalid
choices, false specialist omissions, and unnecessary specialists. Report direct
TypeSafe Jev and Pi-fallback evidence separately; never substitute a Pi
display-name match for the direct typed Jev transport.
Stop the benchmark on a trust/authority violation, private-body leak, invalid
model choice, failed mandatory review, or unexpected provider request. Synthetic
and fixed-output runs remain lifecycle evidence only and never fill a quality or
cost cell.

#### Configurable dynamic guidance production-wire acceptance — 2026-09-24

One separately authorized dynamic start used the direct fixed-endpoint TypeSafe
adapter with configured user-global `dynamicGuidance`. TypeSafe accepted the request
and returned decision model `jev-1.13.0`; retained provenance records provider
`typesafe`, source `typesafe-auth`, and the compatibility decision thinking value
`off`. The call reported 11,413 input and 2,401 output tokens (13,814 total);
monetary cost remained unavailable rather than estimated.

The authorization exposed configured status and only guidance digest prefix
`d8f66938f129`, not the guidance body. Jev selected the exact available tuples
`openai-codex/gpt-6-luna` with thinking `off` for the implementer and mandatory
reviewer, with no optional specialist. The launch reread policy and accepted the
same full planner-policy binding
`02d645f8cff0775490675ec537b45afc201036b0b1e6b23467f73ce74cabd0ff`.
Retained manifest/planning provenance, JSON and human status, broker/dashboard
metadata, and structured reports contained the decision identity, exact worker
tuples, usage, and opaque digests without the guidance body. The run reached
`ready` after mandatory review.

This establishes one successful production-wire request, accepted typed
response, deterministic tuple validation, body-free public/retained planning
surfaces, and unchanged-policy launch revalidation. It does not establish that
Jev followed the preference semantically, that the chosen roster or tuples were
better, or that guidance improves cost or quality. The model-free regression
still supplies the separate negative case proving that a changed guidance digest
rejects launch before workers start.

#### Issue #174 direct-Jev benchmark stop evidence — 2026-10-04

The owner separately authorized the frozen three-pair matrix with no repeats and
a $5 maximum total provider-reported spend. The arms used disposable
fixed-revision dependency-free fixtures, the packaged `economy` profile, `single`
flow, a one-repair-round cap, native TUI workers, and exact role locks. A separate
explicit `investigation` intent check returned `direct-parent` before creating a
session or coordination root, so the no-change case made no planning or worker
call.

The simple static arm reached `ready` after mandatory review with no repair or
operator correction, and its required tests passed. Its two workers made 10
provider calls using 18,668 input, 1,051 output, 10,624 cache-read, 0 cache-write,
and 248 reasoning tokens, with $0.0480468 provider-reported cost and 58.684
seconds from manifest creation to ready. The exact launch assignments were
`openai/gpt-6.1-sol` at `low` for the implementer and `xai/grok-4.7` at `low`
for the reviewer.

The matching dynamic arm passed approved-pool admission only after every
planner-eligible role had an exact lock. Its one authorized direct TypeSafe
attempt then returned the bounded `dynamic_planning_failed` terminal result.
The selected-service failure created no tmux session or coordination root and
did not trigger a Pi fallback or worker call. The terminal boundary intentionally
retained no raw provider response and exposed no planner usage for the failed
attempt, so its latency, token count, and monetary cost remain unavailable rather
than estimated. In accordance with the predeclared stop rule, the specialist and
ambiguous pairs were not started and no repeat was attempted.

This is one failed direct-Jev benchmark attempt, not a general failure-rate or
quality estimate. It demonstrates fail-closed launch and fallback behavior but
does not satisfy the direct-Jev rollout gate. A repeat or broader matrix requires
fresh provider-call authorization after the bounded failure is diagnosable; no
default rollout is authorized.

#### Issue #174 fallback benchmark evidence

One explicitly authorized provider-backed run of the frozen three-case matrix was
completed on Pi 0.84.4 against disposable dependency-free fixtures before the
direct TypeSafe adapter existed. The Pi catalog returned no exact `Jev` match, so
this remains **Pi-fallback evidence only** and must not be relabeled as Jev
evidence. The strict configured fallback was `xai/grok-4.6` at `low` for
three planner calls; worker roles were constrained to that exact model with the
packaged `economy` thinking map. No task, prompt, rationale, report, diff, log,
credential, endpoint, or source body is retained here. An excluded readiness
pilot also found two catalog-present identities that were not runtime-eligible in
this environment: `openai-codex/gpt-5.4` was rejected for the active account type,
and `google/gemini-2.5-flash` rejected the configured key. The paired matrix was
therefore frozen on a separately verified xAI identity. Catalog/auth presence is
not production-readiness evidence, which is another reason default rollout
remains blocked.

| Case/arm | Roles | Planner cost | Worker calls/cost | End-to-end wall time | Review outcome |
| --- | ---: | ---: | ---: | ---: | --- |
| simple static | 2 | — | 11 / $0.056918 | 47.209 s | approved; 0 findings; checks passed |
| simple dynamic | 2 | $0.007076 | 16 / $0.116418 | 106.744 s | approved; 2 findings; checks passed |
| specialist static | 3 | — | 34 / $0.369352 | 351.112 s | 1 revision; approved; 5 reviewer findings; checks passed |
| specialist dynamic | 2 | $0.009144 | 16 / $0.133418 | 169.349 s | approved; 3 findings; checks passed |
| ambiguous static | 5 | — | 29 / $0.322824 | 258.259 s | approved; 2 findings; checks passed |
| ambiguous dynamic | 2 | $0.007602 | 14 / $0.225416 | 262.975 s | approved; 3 findings; checks passed |

Dynamic pre-launch planning/preview/launch elapsed times were 8.911 s, 14.674 s,
and 10.857 s. Aggregate static worker usage was 74 calls, 174,801 input,
42,262 output, 291,840 cache-read, 0 cache-write tokens, and $0.749094. Aggregate
dynamic usage was 3 planner calls (5,170 input, 2,119 output, 1,536 cache-read,
0 cache-write tokens, $0.023822) plus 46 worker calls (98,656 input, 29,790
output, 198,400 cache-read, 0 cache-write tokens, $0.475252). In this single
sample, dynamic total cost was 33.4% lower, provider calls 33.8% lower, and wall
time 17.9% lower; the simple case was materially worse on all three measures and
the ambiguous case had slightly higher wall time. These are observations, not a
rollout claim.

All six paired runs reached the retained `ready` state, passed the required test
command, preserved one writer plus mandatory review, made no invalid model
choice, and required no operator correction: planning failure 0/3, workflow
failure 0/6, correction rate 0/6. The dynamic planner chose only implementer and
reviewer in every case. Count the omitted probe in the predeclared
specialist-relevant case as one false specialist omission even though checks and
review passed. The static ambiguous arm also ran Playwright and Django against a
dependency-free CLI fixture; count those two as unnecessary specialists. Direct
TypeSafe `jev-latest` evidence remains unavailable until the new adapter is run
under separate provider-call authorization with `/login typesafe` or the
`TYPESAFE_API_KEY` automation fallback. No default rollout is authorized.

### A. Command and parent supervision

- Run `/or-models` and open `/or-dashboard`; confirm help is concise, the About
  footer contains version/project/package/contribution details, and doctor
  appears only after `d`.
- Configure the credential once with `/login typesafe`, then opt in with
  `/or-start --plan` for a bounded task. With separately authorized TypeSafe
  authentication, confirm direct `jev-latest` is shown before the additional
  provider call; after `/logout` and selecting `TypeSafe Jev Planner`, with no
  environment fallback, confirm
  the exact configured Pi fallback is shown. Confirm Jev wins over an
  operator-supplied Pi decision model while TypeSafe auth is configured, and that
  the exact Pi decision model wins over configured identities after TypeSafe
  auth is removed. Confirm
  selected worker model/thinking tuples are available and use only each model's
  advertised supported levels through `max`. Repeat once with bounded natural-
  language `dynamicGuidance` in the unified `tmux-orchestrator.json` planner member and confirm the
  authorization shows its digest, both direct Jev and Pi-chat receive subordinate
  preferences, the retained plan omits their body, and malformed or oversized guidance fails
  before HTTP.
  Confirm malformed output starts nothing, and launch
  still requires a second confirmation. If exact-project custom specialists are
  configured, confirm only freshly verified allowlisted identities/contracts are
  candidates and an accepted subset remains read-only. This is not evidence of
  cost or quality improvement.
- Start one small `single` workflow with only implementer and mandatory reviewer.
- Reopen `/or-dashboard`, confirm the run and usage metadata appear, use Enter
  to attach when the parent Pi is inside tmux, and verify `x` requires explicit
  confirmation before stopping the selected run.
- Confirm the invoking Pi remains the parent and receives lifecycle/final reports.
- Verify idle workers do not poll or issue provider turns.

### B. Phased implementation and review

- Start one bounded complex task with `phased` flow.
- Confirm inspect/plan is read-only, terminates at the plan report, and the next
  implementation assignment receives the bounded plan without inspection turns.
- Confirm only the implementer writes and the built-in reviewer must approve.

### C. Specialist activation

- Enable only specialists relevant to the test.
- Use a documentation-only change to verify deterministic skips are visible to
  the reviewer without waking irrelevant specialists.
- Use an ambiguous/high-risk change or explicit force selection to verify the
  configured specialist runs and review waits for its real report.
- For one registered custom specialist, preview an explicit thinking level and
  `THINKING=profile`; confirm the explicit level wins and an absent profile mapping
  fails closed. A profile mapping without `--custom-role` or exact-project
  `customRoles` must launch nothing. A version-4 exact-project `customRoles`
  preview must show the identity, contract, and `project-config` source, and
  `--no-project-custom-roles` must omit it. A dynamic preview may select only
  those exact trusted identities; confirm a selected subset is passed as exact
  `--project-custom-role` identities, registry/resource digests are revalidated,
  and unavailable, stale, duplicate, or unallowlisted identities make no
  provider or launch call. Exercise the equivalent terminal path with
  `start --dynamic-plan --authorize-planning --yes` and confirm that omitting
  either authorization fails before planning/launch. After accepting a preview,
  change each mutable class in turn (task/context, planner policy, exact-project
  config or custom resource digest, and available catalog/thinking metadata) and
  confirm pre-launch binding revalidation rejects it with no worker/session.
  Confirm successful `list`, `status`, dashboard, and Supervisor projections
  show only bounded accepted model/role/contract/source/request/timestamp/digest
  metadata and contain no task, prompt, rationale/provider body, endpoint,
  credential, or custom resource path/body.
- Exercise documentation-only skip, ambiguous-path run, and
  `--force-specialist CUSTOM_ID`; confirm every run gates built-in review while a
  skip remains reviewer-visible and issues no specialist provider turn.
- Do not treat synthetic probe/browser evidence as production acceptance.

### D. Workspace capsule experiment

- Use a clean canonical Git root and one or two reviewed relevant paths.
- Enable the workspace capsule and inspect confirmation counts/digest metadata.
- Confirm workers still read governing `AGENTS.md`/`CLAUDE.md` content.
- Exercise one normal source edit and, if needed, a confirmed worker restart;
  normal clean/dirty changes must not stale replay.
- Separately verify changed HEAD/instruction/marker/path trust identity fails
  closed. Keep this experiment opt-in; do not infer savings from local bytes.

### E. TUI/RPC and operator controls

- Run at least one native TUI workflow. Use RPC workers only through an explicit
  request.
- Exercise `status`, `watch`, `attach` (prefix then `L` returns), bounded `send`,
  and one confirmed restart.
- Stop each completed test session explicitly; retained metadata should remain
  readable through Supervisor API v2 without tmux.

### F. Usage and quality evidence

For each provider-backed run, record only bounded non-sensitive facts:

- provider calls and token categories by assignment;
- provider-reported cost when available;
- context occupancy/pressure;
- required checks and their outcomes;
- reviewer findings and revision rounds;
- whether the final result was accepted.

Never post task, prompt, report, provider, diff, log, credential, or private
source bodies in public results.

## 4. Failure capture

If a test fails, capture:

```bash
"$PACKAGE_ROOT/bin/pi-tmux-agents" status SESSION
"$PACKAGE_ROOT/bin/pi-tmux-agents" --json supervisor snapshot SESSION --run RUN_ID
```

Also record the staged commit and tarball digest from `provenance.json`, the test
case, transport, flow/profile, enabled/forced roles, and the exact failed check.
Redact all workflow and provider bodies.

An interrupted assignment or restart is `uncertain`; do not blindly replay it.
Do not replace an existing tmux session.

## 5. Rollback and cleanup

The recommended `-e` procedure does not modify Pi package settings. After all
sessions are explicitly stopped and Pi exits:

```bash
pi list > /tmp/pi-packages-after.txt
diff -u /tmp/pi-packages-before.txt /tmp/pi-packages-after.txt
rm -rf "$STAGE"
rm -f /tmp/pi-packages-before.txt /tmp/pi-packages-after.txt
```

If you deliberately used `pi install "$PACKAGE_ROOT"` instead, remove that exact
local source and verify settings before deleting the stage:

```bash
pi remove "$PACKAGE_ROOT"
pi list
rm -rf "$STAGE"
```

Never remove or overwrite the released/global package merely to test a staged
build. Explicit `--no-extensions --no-skills -e "$PACKAGE_ROOT"` isolates the
pre-release resource selection for one Pi process.

### Independent planning-scope acceptance (#197)

Model-free regressions exercise all seven nonempty combinations on direct Jev
and Pi fallback synthetic transports, asserting identical authorized questions,
fixed-axis locks, exact pool membership, one call, body-free provenance,
cancellation, malformed choices, stale policy/locks/preview, dry-run, and no-launch
failures. Python tests cover effective policy precedence, CLI/terminal forwarding,
current v4 record validation/admission, legacy v3 compatibility, and stale
scope/intent bindings. Assert every Jev/Pi Choice question uses the established
`type`, `instructions`, and `criteria` shape (including `task_intent`), with no
new top-level guidance field. This is synthetic contract evidence, not
production TypeSafe/Pi wire acceptance.

For separately authorized manual acceptance, compare topology-only and
topology-plus-thinking against the deterministic preview for every eligible
role, including optional/custom identities. Verify `/or-start --plan-scopes`
cancellation, `--plan=topology,thinking`, and legacy `--plan` all-axis
compatibility. Confirm scopes, locks, decision source, approved pool counts,
and exact assignments in both confirmations, immediate acknowledgement,
`start --dry-run`, status/dashboard and Supervisor reads. Change policy after
preview and verify no session launches. Do not perform real provider calls
without separate operator authorization. No default rollout or #198–#200 work
is implied.
