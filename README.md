# Pi Tmux Orchestrator

[![npm version](https://img.shields.io/npm/v/pi-tmux-orchestrator.svg)](https://www.npmjs.com/package/pi-tmux-orchestrator)
[![npm downloads](https://img.shields.io/npm/dm/pi-tmux-orchestrator.svg)](https://www.npmjs.com/package/pi-tmux-orchestrator)
[![CI](https://github.com/revazi/pi-tmux-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/revazi/pi-tmux-orchestrator/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE.md)

A [Pi](https://github.com/earendil-works/pi) extension, skill, and
dependency-free Python CLI for coordinating coding agents in monitorable tmux
grids.

## Demo

![Pi Tmux Orchestrator native worker grid and broker dashboard](https://raw.githubusercontent.com/revazi/pi-tmux-orchestrator/main/assets/pi-tmux-orchestrator-demo.png)

Native Pi TUI workers remain directly steerable while the broker dashboard shows
workflow, role, model, usage, context, and recent metadata state.

## What it provides

- One implementer with normal Pi coding tools
- One independent reviewer with read/verification tools
- Optional technical probe, Playwright tester, and Django expert
- One event-driven owner-only Unix-socket broker per run
- An adaptive broker/status dashboard for workflow, role, model, usage, context, and recent metadata events
- The same worker-bridge protocol for interactive TUI and headless RPC workers
- Native worker output in TUI panes and assistant/tool input/tool output visibility in RPC panes
- Parent Pi supervision with event-driven final structured reports and attention alerts
- Assignment-boundary context resets with bounded parent capsules, coalesced latest-per-role run state, and complete Pi history
- An opt-in, ephemeral, evidence-gated workspace-capsule experiment for cold assignments
- Lean worker system prompts, disabled automatic skill discovery, and explicit digest-bound per-role skill opt-in
- Orchestration-only UTF-8 read/grep/bash result caps with actionable continuation and metadata-only tuning facts
- Bounded typed reports through a terminating Pi tool
- No Markdown handoffs, readiness markers, mailbox payload files, relay polling,
  lifecycle sleeps, or tmux key injection in newly started runs
- Metadata-only SQLite state, durable Pi sessions, idempotent command IDs, and
  crash-`uncertain` semantics
- User-configurable provider/model/thinking policy for every role, including Pi
  custom providers, with exact natural-language overrides through the model tool
- Actual provider token/cost accounting when Pi exposes it, plus context pressure
  and strict user-global/per-run budget policy
- A versioned JSON CLI and tmux-independent Supervisor API v2
- A persistent project-neutral controller Pi session
- Explicit project trust, one-writer policy, bounded output, and confirmations
  for restart/stop

## Grid

```text
┌──────────────────────────────┬──────────────────────────────┐
│ Implementer                  │ Reviewer                     │
│ configured provider/model    │ configured provider/model    │
│ configured thinking          │ configured thinking          │
├──────────────────────────────┼──────────────────────────────┤
│ Optional probe               │ Optional Playwright tester   │
│ configured provider/model    │ configured provider/model    │
│ configured thinking          │ configured thinking          │
├──────────────────────────────┼──────────────────────────────┤
│ Optional Django expert       │ Broker + status              │
│ configured provider/model    │ state, roles, models, usage  │
│ configured thinking          │ recent metadata events       │
└──────────────────────────────┴──────────────────────────────┘
```

Tmux hosts and displays the broker and workers. It is not the coordination
transport.

The broker pane is an event-driven terminal dashboard rather than a log tail.
It prioritizes session identity and workflow state/round, then transport and
protocol, per-role connection/lifecycle/assignment/model/thinking/actual usage,
soft-budget pressure, and a bounded recent metadata event rail. Green denotes
healthy/success, cyan active work, yellow attention or budget pressure, red
failure/uncertainty, and dim text secondary metadata. Full, compact, and narrow
layouts adapt to the pane without wrapping. State changes and supported
`SIGWINCH` resize notifications repaint TTYs in place and restore cursor state;
`NO_COLOR`, `TERM=dumb`, non-TTY, and non-UTF-8 outputs
have plain safe fallbacks. The dashboard never renders workflow or provider
bodies and never polls to refresh. See the reviewable
[broker dashboard design](references/dashboard-design.md) for the wireframe,
hierarchy, semantic tokens, breakpoints, and intentional omissions.

## Architecture

`bin/pi-tmux-agents` is a thin executable. The authoritative standard-library
implementation is `pi_tmux_orchestrator/`:

- CLI/controller/tmux host control
- strict manifests and private storage
- framed broker protocol and metadata-only SQLite state machine
- focused dependency-free broker dashboard presentation
- broker clients and TUI/RPC worker supervision
- role system prompts and worker bridge
- retained-run Supervisor API

The extension delegates orchestration control actions as bounded argument
arrays to the Python JSON CLI, reads only bounded model metadata from Pi's
current registry, and owns the parent-session observer/presentation bridge. For
Pi-started runs, the invoking parent Pi keeps an authenticated
read-only broker observer: tmux panes provide live worker visibility, while
structured completion/attention reports return to the parent for decisions.
The broker is the only current-run metadata writer. Pi owns conversation
durability. Reviewer roles inspect the shared worktree directly instead of
receiving copied diffs or logs.

See [coordination protocol v1](references/protocol-v1.md) for schemas, role ACLs,
authentication, lifecycle, report limits, acknowledgements, retry, crash
recovery, and token accounting.

## Requirements

- Pi available as `pi`
- Python 3.11+
- tmux 3.2+; tmux 3.5+ recommended
- Node 22.19+ for package verification
- Ruff 0.11.11 for repository development checks only

Recommended tmux 3.5+ configuration:

```tmux
set -g extended-keys on
set -g extended-keys-format csi-u
```

## Installation

```bash
pi install npm:pi-tmux-orchestrator
```

One run without installation:

```bash
pi -e npm:pi-tmux-orchestrator
```

Reviewed Git commit or local checkout:

```bash
pi install git:github.com/revazi/pi-tmux-orchestrator@<reviewed-full-commit>
pi install /absolute/path/to/pi-tmux-orchestrator
```

Pi packages execute with the current user's permissions. Inspect source before
installation. This package has no runtime dependency tree and is MIT licensed.

If an old installation uses the removed scoped npm identity:

```bash
pi remove npm:@revazi/pi-tmux-orchestrator
pi install npm:pi-tmux-orchestrator
```

## Start from Pi

The package exposes:

- `/orchestrator-help`
- `/orchestrator-about`
- `/orchestrator-doctor`
- `/orchestrator-models [query]`
- `/orchestrator-start [task]`
- `/orchestrator-list`
- `/orchestrator-status [session]`
- `/orchestrator-watch [session]`
- `/orchestrator-attach [session]`
- `/orchestrator-send [session]`
- `/orchestrator-stop [session]`
- Short aliases: `/or-help`, `/or-about`, `/or-doctor`, `/or-models`, `/or-start`, `/or-list`,
  `/or-status`, `/or-watch`, `/or-attach`, `/or-send`, and `/or-stop`
- `/orchestrate` and `/orchestrations` compatibility aliases

The `/or-*` aliases use the exact same handlers, confirmations, selectors, and
safety boundaries as their canonical `/orchestrator-*` commands.

At interactive Pi startup, the extension makes one best-effort, time-bounded
request to the public npm registry. If a newer release exists, it shows a
non-blocking warning with `pi update npm:pi-tmux-orchestrator`. `/or-about`
shows the installed version, latest npm version, update command, and project
links. Set `PI_TMUX_ORCHESTRATOR_DISABLE_UPDATE_NOTICE=1` to disable startup
notices. Update checks are skipped in orchestration worker and controller
sessions.

### Worker model configuration

Pi's own provider authentication and `models.json` remain authoritative. The
orchestrator never reads or copies provider credentials. Configure global
worker defaults outside project repositories in
`~/.pi/agent/tmux-orchestrator.json` (or under `PI_CODING_AGENT_DIR`):

```json
{
  "version": 2,
  "defaultProfile": "balanced",
  "profiles": {
    "review-heavy-economy": {
      "implementer": "medium",
      "reviewer": "high",
      "probe": "low",
      "playwright": "medium",
      "django": "medium"
    }
  },
  "defaults": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6"
  },
  "roles": {
    "reviewer": {
      "provider": "google",
      "model": "gemini-3.1-pro-preview"
    }
  }
}
```

The file is read for every new start. Version-1 model files remain accepted and
are normalized without changing their prior thinking choices. Version 2 adds a
default profile and up to 16 strict custom profiles. A custom name matches
`[a-z][a-z0-9-]{0,31}`, cannot replace a packaged name, and must map all five
known roles to supported thinking levels. Profiles never select provider/model,
change tools or authority, create roles, disable review, or control routing.
Credential and endpoint fields are rejected, and a configuration path inside the
target project fails closed.

Packaged profiles are immutable:

| Profile | Implementer | Reviewer | Probe | Playwright | Django |
|---|---|---|---|---|---|
| `economy` | `medium` | `medium` | `low` | `medium` | `medium` |
| `balanced` | `high` | `high` | `medium` | `medium` | `medium` |
| `thorough` | `xhigh` | `high` | `high` | `high` | `high` |

`thorough` is the compatibility default because it exactly preserves the
pre-profile packaged thinking settings. This is not a quality or cost
recommendation. The checked profile baseline defines simple, medium, and
multi-round cases plus required provider-call/token/cost and acceptance-test/
review metrics, but comparative provider usage and quality are currently marked
unavailable. It therefore makes no savings, equivalence, recommended-default,
or billing claim. Validate that policy record with
`node scripts/execution-profile-baseline.mjs --check`.

Select a profile with `--profile NAME` or
the model tool's `profile`. The effective name, kind, source, and per-role
thinking levels appear in dry-run, confirmation, private manifest v4 metadata,
status, and Supervisor reads. Older manifests report profile metadata as
unavailable.

`defaults` applies to every role and `roles` overrides individual roles. Set
`PI_TMUX_ORCHESTRATOR_CONFIG` to an absolute path to keep the file elsewhere.
Precedence is: explicit per-role/all-role CLI or model-tool override, user-global
role override, user-global defaults, selected custom/packaged profile, then the
compatibility fallback. The selected profile comes from a per-run request,
`defaultProfile`, or the packaged compatibility default, in that order.

### Provider-usage budget configuration

A separate strict user-global file, `~/.pi/agent/tmux-orchestrator-budgets.json`,
defines versioned warning and hard thresholds without entering target
repositories:

```json
{
  "version": 1,
  "enforcement": "warn-only",
  "warning": {
    "run": { "provider_calls": 50, "operational_tokens": 600000 },
    "role": { "operational_tokens": 200000 },
    "assignment": { "context_percent": 80 }
  },
  "hard": {
    "run": { "cost_total": 25 },
    "role": {},
    "assignment": { "context_percent": 95 }
  }
}
```

Scopes are `run`, `role`, and `assignment`. Metrics are provider calls, input,
output, cache read, cache write, optional reasoning, operational tokens,
provider-reported cost, context tokens, and context percentage. Categories stay
separate; `operational_tokens` is not billing. Unknown, credential, endpoint,
non-finite, non-positive, oversized, duplicate, and warning-above-hard values
are rejected. `null` removes an inherited threshold. The packaged migration
default is warn-only with the existing 600,000 run and 200,000 role operational
warnings and no hard thresholds.

Set `PI_TMUX_ORCHESTRATOR_BUDGET_CONFIG` to an absolute external path. A budget
file in the target project is rejected. Explicit `--budget-enforcement` and
repeatable `--budget-override LEVEL.SCOPE.METRIC=VALUE` (or `=off`) values win
for one run; the model tool exposes the same strict native `budgetOverrides`
object. Dry-run JSON and Pi's start confirmation show the effective policy.
The policy is retained as numeric/enum broker metadata, but every threshold is
observational: neither `warn-only` nor the compatibility `hard` mode blocks a
tool, interrupts a provider response, suppresses required review, or changes
downstream assignment routing. Assignment `provider_calls`, `context_tokens`,
and `context_percent` thresholds are projected into each worker bridge. The
bridge counts provider turns from the accepted assignment boundary, emits one
bounded warning in the next tool result, and records a higher-severity hard fact
without enforcing it. Parallel tools and `orchestrator_report` remain available.
Restart restores the assignment-local markers from the Pi session, while
SQLite, status, the dashboard, and Supervisor output retain only bounded numeric
facts. Operators use that visibility to decide whether to steer, request a
report, restart, or stop the run; there is no live budget-resume command because
budgets never pause routing.

### Worker prompts and skill opt-in

New TUI and RPC workers use the same lean custom system prompt instead of Pi's
full general-purpose coding prompt plus repeated role guidance. The lean prompt
keeps active-tool guidance, role authority, one-writer and safety rules, final
report behavior, and context efficiency guidance. Pi still discovers and
appends governing `AGENTS.md`/`CLAUDE.md` files; the orchestrator does not pass
`--no-context-files`.

Automatic worker skill discovery is disabled with `--no-skills`. Load a reviewed
Markdown skill for one role only with repeatable
`--worker-skill ROLE=/absolute/path/SKILL.md`, or the model tool's strict
`workerSkills` role arrays. The dry-run and interactive confirmation show the
exact paths. Each selected file is size-bounded, UTF-8 validated, and bound to a
SHA-256 digest in private manifest metadata; launch or restart fails closed if
it changes. Skills add instructions, not tools: reviewer and specialist Pi
processes retain their enforced read/verification tool allowlist and cannot gain
`edit` or `write` through a skill.

The checked-in model-free Pi 0.84.1 fixture captures the actual built prompt
options with a synthetic context file and skills. Its normalized reviewer prompt
changes from 5,000 to 2,479 serialized characters (50.4%). This is a deterministic
prompt-size proxy, not provider tokens, billing, cache efficiency, or a savings
claim; provider metadata remains authoritative.

### Experimental cold-assignment workspace capsule

`--workspace-capsule` opts one run into a disabled-by-default discovery
experiment. Repeat `--workspace-relevant-path PROJECT_RELATIVE_PATH` for at most
16 existing parent-supplied paths; the Pi model tool uses `workspaceCapsule: true`
and `workspaceRelevantPaths`. Native TUI and headless RPC workers receive
the same broker-rendered baseline.

The deterministic schema is capped at 8 KiB and contains only a SHA-256 identity
for the canonical Git worktree root, initial Git HEAD plus a clean/dirty observation,
project-relative governing instruction paths with SHA-256 hashes,
existing names from a fixed 16-file top-level build/test-marker allowlist, and
the bounded relevant paths. It never enumerates or serializes a complete repository
tree and never includes instruction or source contents; Git may still inspect the
worktree internally to produce the initial clean/dirty observation. The project must be the canonical
Git worktree root with an existing HEAD. Absolute, escaping, missing, duplicate,
non-normalized, oversized, unknown, and symlinked paths or fields fail closed.
Instruction candidates follow Pi's per-directory precedence, but the capsule is
only a hint: Pi context-file discovery remains enabled, and every worker must
still discover and read governing `AGENTS.md`/`CLAUDE.md` plus referenced/scoped
instructions through Pi/project mechanisms.

Construction happens independently for dry-run and confirmed start. The broker
strictly revalidates canonical root, Git HEAD, marker set, relevant paths, and
instruction identities before initial delivery and before a live in-memory
restart replay. Normal task edits may change the clean/dirty observation without
invalidating the initial metadata, so late delivery and restart remain available.
A changed HEAD, instruction, marker set, missing path, or new symlink rejects the
capsule; a stale restart handover becomes `uncertain` rather than trusting old
hints. The capsule exists only in the transient startup
payload, broker memory, and worker baseline. It is deleted with the startup
payload after initial routing and is absent from manifests, SQLite, status,
dashboards, Supervisor API, registries, and project files. There is no cross-run
cache; adding one would first require an explicit privacy/retention policy.
Dry-run/confirmation may expose only bounded schema/count/byte/digest validation
metadata, never path lists or bodies. Retained manifests remain v4-compatible.

The capsule changes serialized startup context and could improve or invalidate a
provider's cache prefix depending on provider serialization and cache policy.
No provider cache behavior is inferred from local bytes. The checked two-fixture
benchmark reports exactly 733 and 886 UTF-8 workspace-hint bytes added to the
synthetic worker baselines; disabled runs add zero. It also reports conceptual
worker discovery-operation proxies of 4→2 while separately reporting 39 and 53
construction-operation proxies. Serialized worker discovery results, provider
calls, tokens, cost, reviewer findings, checks, revisions, and correctness are
all unavailable; the benchmark does not fabricate a full-tree baseline. These
are model-free shape/operation proxies—not savings, billing, cache, provider-call,
or correctness-equivalence evidence—so the experiment remains opt-in and
supports no default change. Validate it with
`python3 scripts/workspace-capsule-baseline.py --check`.

### Implementer inspect/plan contract

The shared TUI/RPC worker bridge accepts a second implementer report kind,
`plan`, only for an active assignment retained as `plan`. During such an
assignment it switches the implementer to active tools
`read,bash,grep,find,ls,orchestrator_report`; `edit`, `write`, and other tools are
removed and blocked. As with specialist read-only policy, retained `bash` is not
an OS sandbox, so the assignment and prompt also prohibit modification. Restart
restores the assignment kind and reapplies the same tool set.

A plan requires a summary plus relevant paths, relevant symbols, intended
changes, required checks, risks, and open questions. Each array has at most 12
strings of at most 300 characters; summary is at most 1,000 characters and
relevant paths must be relative. Plans cannot contain changed paths, executed
checks, findings, limitations, approval, or a verdict. The bridge and broker
both validate the shape, role, assignment-kind match, 32 KiB total report limit,
and one-report-per-assignment rule.

Accepted plan evidence is rendered into the existing latest-per-role rolling
run-state capsule. SQLite retains only its report kind, summary length, zero
change/check/finding counts, numeric provider usage when available, and other
existing metadata—not plan bodies.

Start with `--implementation-flow phased` (or model-tool
`implementationFlow: "phased"`) for complex work. The broker starts a read-only
`plan` assignment, accepts and delivers its bounded plan capsule, then creates a
distinct same-round `implementation` assignment. That boundary prunes inspection
assistant/tool turns before the first implementation provider request while
keeping baseline, plan run state, direct steering, and new-assignment turns. Use
`single`, the compatibility default, for simple work. Repair rounds always start
directly as `implementation` with latest reviewer/specialist evidence rather
than repeating inspection.

The checked synthetic complex-task fixture records a 98.6% serialized-context
proxy reduction at the first implementation request. Provider calls, token
categories, cost, failed checks, and missed findings are unavailable, so this is
not a provider-token, billing, call-savings, or quality-equivalence claim. Run
`node scripts/phased-implementation-baseline.mjs --check` to verify the fixture.

### Deterministic specialist activation

Configured probe, Playwright, and Django roles use fixed versioned predicates;
no classifier model request is made. Empty, malformed, unknown, or potentially
high-risk evidence fails toward running the configured role:

- probe runs for initial tasks containing integration, runtime, security,
  database, migration, protocol, concurrency, transaction, API, auth, or
  credential terms; it skips only clear documentation/typo tasks. On repair
  rounds it skips only documentation-only changed paths.
- Playwright runs for browser/frontend paths and every ambiguous non-documentation
  path; it skips only documentation-only changed paths.
- Django runs for framework markers such as settings, URLs, models, views,
  middleware, migrations, templates, ASGI/WSGI, admin, apps, or `manage.py`; it
  also runs for ambiguous paths and skips only documentation-only or clearly
  frontend-only path sets.

Use repeatable `--force-specialist probe|playwright|django`, or model-tool
`forceSpecialists`, only when an enabled role must run regardless of a skip
predicate. Forced activation always requires that role's real report before
review; a skip cannot satisfy it. SQLite schema v7 stores only role, round,
`run|skipped`, versioned rule ID, and forced boolean. Reviewer run state shows
those decisions and whether activated evidence was reported. Synthetic browser
or probe evidence remains non-production acceptance. The checked four-case
assignment-count proxy selects 8 of 12 formerly unconditional assignments (4
avoided), but provider usage and quality evidence remain unavailable, so it
makes no call, token, cost, or equivalence claim. Validate it with
`python3 scripts/specialist-activation-baseline.py --check`.

### Worker result-volume policy

Only orchestration workers apply an additional result policy before the next
provider request. `read` defaults to and is capped at 400 requested lines with
a 16 KiB/400-line emitted limit. `grep` defaults to and is capped at 40 matches,
context is capped at two lines, and emitted search text is capped at 16 KiB/240
lines. `bash` output is capped at 24 KiB/400 lines while preserving a bounded
beginning, failure-diagnostic excerpt, and ending. When this lower bash cap is
reached, the complete available output is written to a mode-`0600` temporary
file unless Pi already supplied a full-output path.

Every truncated result tells the worker how to request the next targeted read
page, refine grep, or inspect a targeted full-output slice. Session-private
custom entries record only assignment ID, tool enum, direction, booleans, and
numeric source/emitted/input-cap facts. A following read page or refined grep is
recorded as metadata without paths, patterns, commands, or result bodies. This
policy is loaded by the same worker extension in TUI and RPC modes and does not
change ordinary Pi sessions.

The checked synthetic benchmark reports 144,491 before versus 89,733 after
serialized UTF-8 provider-context bytes (37.9% reduction) and two additional
provider calls for read/grep pagination across three scenarios. Run:

```bash
node scripts/result-volume-baseline.mjs --check
```

These are deterministic size/call proxies, not provider tokens, cost, quality,
or production-wire acceptance.

Natural-language requests are supported by the `tmux_orchestrator` tool. For
example, users can ask Pi to “use my current model for every worker,” “use
Anthropic model X with high thinking for the implementer,” or “use configured
models.” Pi can call the metadata-only `models` action to resolve exact IDs; the
same catalogue is available with `/or-models [query]`. The tool must not invent
provider/model IDs. Explicit CLI equivalents remain available:

```bash
pi-tmux-agents start --task-file /tmp/task.md \
  --implementer-provider anthropic \
  --implementer-model claude-sonnet-4-6 \
  --implementer-thinking high \
  --reviewer-provider google \
  --reviewer-model gemini-3.1-pro-preview \
  --reviewer-thinking medium
```

When a session argument is omitted, `status`, `watch`, `attach`, `send`, and
`stop` list valid running orchestrations in a Pi selector showing session and
project. Choosing one passes its exact session name to the authoritative CLI;
providing a session argument still bypasses the picker.

The `tmux_orchestrator` model tool provides bounded `doctor`, `list`, `status`,
`watch`, `attach`, `start`, and `send` actions. Start requires interactive
confirmation. The Pi session that invokes `start` is the parent supervisor; a
run creates only the detached worker grid and does not start another parent Pi,
parent window, or controller. New runs are watched automatically. `watch`
subscribes that invoking Pi to lifecycle/final updates without changing the
terminal. `attach` (or `/orchestrator-attach SESSION`) switches its existing
tmux client into the worker grid after ensuring observation. Use normal tmux
pane keys to select a subagent and type directly into its native Pi editor.
Press the tmux prefix followed by `L` to detach from the grid and return to the
same invoking Pi; the orchestration keeps running and can be reattached.

Native Pi TUI workers are the interactive default, preserving Pi's highlighting,
tool rendering, and input field in every subagent pane. Plain RPC panes remain
an explicit headless automation option only. The parent receives visible
lifecycle/report-received progress and a triggered structured update when the
broker reaches `ready`, `needs_attention`, or `uncertain`. Parent project trust
is never inherited by child Pi sessions; child `--approve` needs separate
confirmation. For natural-language starts, the parent can synthesize an optional
structured `contextCapsule` from its existing conversation without another
model call. The capsule carries only task-relevant current state, settled
decisions, constraints, acceptance criteria, paths, evidence, open questions,
and out-of-scope items—never the complete parent transcript.

## Start from the terminal

```bash
cat > /tmp/pi-agent-task.md <<'TASK'
Implement the requested change, add focused tests, run verification, and stop
after independent review approval.
TASK

cat > /tmp/pi-agent-context.md <<'CONTEXT'
### Current state
A focused branch already contains the reviewed scaffolding.

### Decisions already made
- Preserve broker-v1 as the only workflow transport.

### Acceptance criteria
- Add focused regressions and preserve metadata-only durable state.
CONTEXT

pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md \
  --implementation-flow phased \
  --context-capsule-file /tmp/pi-agent-context.md \
  --worker-skill reviewer=/absolute/path/to/review-skill/SKILL.md \
  --attach
```

The context capsule is limited to 12 KiB, transferred through a private file,
and deleted after baseline delivery. Its body is excluded from SQLite, status,
registries, dashboards, and the Supervisor API. The live broker retains the
rendered per-role baseline only in memory so a confirmed worker handover can
replay it; the worker Pi session retains every delivery in its complete JSONL
history.

Add specialists:

```bash
pi-tmux-agents start \
  --project "$PWD" \
  --task-file /tmp/pi-agent-task.md \
  --with-probe --probe-task-file /tmp/pi-agent-probe.md \
  --with-playwright --playwright-task-file /tmp/pi-agent-playwright.md \
  --force-specialist playwright \
  --with-django-expert --django-task-file /tmp/pi-agent-django.md
```

Use `--rpc-workers` only when plain headless RPC event panes are explicitly
needed for automation. They render assistant progress plus bounded tool inputs
and outputs, but they do not reproduce Pi's native interactive editor or visual
presentation. Native TUI is the default and is the required presentation for
full visual navigation and direct subagent input. Both presentations use the
same broker and bridge; `--rpc-workers` is not a legacy coordination mode.

Use `--approve-project` only after inspecting and trusting the target project.
RPC workers otherwise apply Pi's saved/global trust behavior and cannot display
startup trust dialogs.

## Event-driven workflow

1. Bridges connect and authenticate independently.
2. Broker stores the task plus optional bounded parent context capsule in each Pi session without waking idle roles.
3. The optional initial probe is triggered. In `single` flow the implementer
   receives `implementation`; in `phased` flow it first receives read-only
   `plan`, then the accepted plan capsule and a distinct same-round
   `implementation` assignment.
4. Each implementer report is submitted through `orchestrator_report`; the tool
   terminates without an acknowledgement-only model turn. The phased boundary
   prunes completed inspection assistant/tool turns before implementation.
5. The initial probe and each round's configured specialists are either run or
   skipped by their fixed predicate. Forced roles always run. The broker records
   only bounded decision metadata and requires every activated report before
   review.
6. Broker replaces prior evidence deliveries with one bounded run-state capsule containing only the latest accepted report per role, then wakes reviewer exactly once. Updates for a role already working are coalesced until its next assignment.
7. Each newly accepted assignment emits one metadata-only `context_boundary` event. That boundary changes the projection policy used on every provider request: the worker keeps the baseline, latest run state, assignment, direct messages, and all assistant/tool turns from the new assignment while pruning only prior-assignment assistant/tool turns.
8. A confirmed role restart advances a broker generation, replays the in-memory baseline, and materializes the latest coalesced run state—including an update deferred during the active assignment—before recovering that assignment. A failed local respawn or interrupted replacement recovery is `uncertain`.
9. `approved` marks the workflow ready without an acknowledgement-only worker
   turn.
10. An attached parent observer shows lifecycle and report-received progress,
   then returns the latest structured role reports when the run is ready or
   requires intervention.

Idle workers end their turns. They do not sleep or poll. A parent with an
attached observer also ends its turn and relies on broker updates instead of
sleeping or repeatedly polling status/tmux. Non-terminal updates remain
non-triggering while steering an already-active parent before its next model
step; terminal updates may trigger parent reasoning. A worker settling without
a report becomes `waiting`/needs attention rather than entering an unlimited
reminder loop. Pi invokes the worker context-projection hook for every provider
request, but its pruning policy changes only at a distinct assignment boundary.
Repeated projections retain every assistant/tool turn from the current
assignment, and Pi's durable worker session history remains intact. A deterministic two-round
synthetic regression currently reduces serialized provider-visible message
characters from 99,170 to 8,678 (91.2%) and enforces a minimum 50% reduction in
CI. This is a reproducible character metric, not provider token acceptance; real
token usage remains provider-reported and visible in the dashboard/Supervisor API.

## Manage grids

```bash
pi-tmux-agents list
pi-tmux-agents status SESSION
pi-tmux-agents attach SESSION
pi-tmux-agents send SESSION --role implementer --message-file /tmp/message.txt
pi-tmux-agents send SESSION --role reviewer --delivery follow-up \
  --command-id 0123456789abcdef0123456789abcdef \
  --message-file /tmp/review-message.txt
pi-tmux-agents abort SESSION --role implementer
pi-tmux-agents restart SESSION --role implementer --yes
pi-tmux-agents stop SESSION --yes
```

When the invoking Pi is already inside tmux, `/orchestrator-attach SESSION`
performs the exact client switch without replacing or stopping that Pi. Prefix
then `L` detaches the client from the grid by returning it to the invoking Pi;
it does not stop the workers. Attach and detach can be repeated while the run is
live.

Acknowledgement means acceptance, not completion. Matching role/action/delivery
command IDs deduplicate; conflicting reuse is rejected. Confirmed restart
respawns the worker process and reopens its exact Pi session ID, preserving the
conversation and complete JSONL history. A failed respawn or crash in an
unprovable delivery or replacement-handover window becomes `uncertain`; there is
no exactly-once claim.

## Supervisor API v2

```bash
pi-tmux-agents --json supervisor capabilities
pi-tmux-agents --json supervisor sessions
pi-tmux-agents --json supervisor runs SESSION
pi-tmux-agents --json supervisor snapshot SESSION --run RUN_ID
pi-tmux-agents --json supervisor usage SESSION --run RUN_ID --limit 100
pi-tmux-agents --json supervisor events SESSION --run RUN_ID \
  --cursor implementer=0 --cursor reviewer=0 --limit 50
pi-tmux-agents --json supervisor command SESSION --run RUN_ID \
  --role reviewer --command-id COMMAND_ID
```

Retained-state reads do not query tmux and never infer liveness from retained
PIDs. Host runtime is reported as `not_observed`. Snapshot/status include role
lifecycle, workflow round/state, actual provider usage totals when available,
latest immutable assignment usage, and context pressure without workflow payload
bodies. In Supervisor snapshots, the latest result is
`roles[].runtime.state.latest_assignment_usage`; retained pre-upgrade reports
keep its `usage` value unavailable. `supervisor usage` returns a bounded,
tmux-independent summary grouped by run, role, round, and assignment kind. It
keeps provider calls, input, cache read/write, output, optional reasoning, cost,
and context pressure separate; `operational_tokens` is explicitly not billing.
The live dashboard renders role tokens as cumulative/latest-assignment delta,
and human `status` adds one bounded latest-assignment category line per role.

## Durable state and compatibility

Run state is private and external to target repositories:

```text
~/.pi/agent/orchestrations/<session>/<run>/
```

Files are retained for manifests, authentication, complete Pi JSONL sessions,
metadata-only SQLite, and a transient startup payload deleted after broker
ingestion. Baselines, bounded latest-per-role evidence, and rolling run-state
bodies needed for a confirmed handover remain only in live broker memory and Pi
sessions. Attached-parent report bodies are likewise ephemeral in broker memory
and become durable only in Pi sessions; none enter SQLite, status, journals,
registries, or Supervisor API output. Newly started workers never create or poll
task/handoff/review/specialist payload files or readiness markers.

Retained `0.4.x` runs remain readable and operable through compatibility code.
Retained manifest-v3 broker runs remain readable. Current starts create manifest
v4 with `coordination: "broker-v1"` and bounded execution-profile metadata;
there is no selectable legacy fallback.

## Token policy

The bridge sums actual Pi/provider-reported input, output, cache-read,
cache-write, optional reasoning tokens, provider-call count, and cost across
complete durable history. Each accepted assignment records the delta from its
numeric session boundary, plus current and peak context occupancy when available,
in the same SQLite transaction as its report metadata. Downstream routing starts
only after that commit. Duplicate reports cannot replace the immutable usage
result. Missing data and usage from older retained reports remain unavailable;
the orchestrator does not invent estimates.

Structural savings include no waiting turns, no polling, no copied diffs/logs,
one reviewer wake after all evidence, no approval acknowledgement turn, and
terminating report calls. Current soft role/run operational-token budgets warn
before additional work. Assignment provider-call/context-pressure warnings are
injected once at a tool boundary, and higher-severity threshold facts are
retained as metadata. Budget thresholds are visibility only: they do not block
parallel or sequential tools, pause downstream assignments, skip required
review, or mark workflows ready. The operator decides whether to steer, request
a report, restart, or stop.

### Development token-efficiency baseline

The repository keeps three model-free provider-context fixtures covering a
simple assignment, a larger single assignment, and a multi-round boundary. The
baseline records provider-call count, serialized provider-visible characters
and UTF-8 bytes by call, plus tool-result characters by tool:

```bash
node scripts/token-efficiency-baseline.mjs --check
```

These measurements are deterministic context-size proxies, not provider tokens,
billing, cache acceptance, or production-wire evidence. Intentional changes are
reviewed before regenerating
`tests/fixtures/token-efficiency-baseline.json` with `--write`.

A separate bounded analyzer aggregates only public metadata from retained broker
SQLite state. It does not read Pi session histories or task, report, prompt,
message, diff, log, credential, or provider bodies:

```bash
python -m pi_tmux_orchestrator.token_efficiency \
  --state-root ~/.pi/agent/orchestrations --max-runs 100
```

Its `operational_tokens` field is the sum of input, output, cache-read, and
cache-write categories for comparison; it is not a billing unit. Cost is shown
only when reported by the provider, and unavailable reasoning usage remains
explicitly unavailable.

## Persistent controller

```bash
pi-tmux-agents controller start
pi-tmux-agents controller status
pi-tmux-agents controller attach
pi-tmux-agents controller stop --confirm
```

The optional controller uses stable Pi session ID
`pi-tmux-orchestrator-controller-v1`, a private project-neutral workspace, and
no `edit`/`write` tools. It exists only after an explicit `controller start`; a
normal orchestration never creates it. It can serve as the parent Pi for
cross-project runs; the invoking project Pi is the interactive parent otherwise.
Every target project must be explicit. Duplicate or unmarked reserved tmux names
are refused.

## Safety model

- One writer; independent read-only reviewers retain `bash` and are not an OS sandbox.
- Exact tmux targets; existing sessions are never replaced.
- Private canonical non-symlink state paths and strict schemas.
- Owner-only local socket with per-role and control authentication.
- Role-specific structured report ACLs and bounded frames/fields.
- Metadata-only status, SQLite, journals, registries, and Supervisor API.
- No Pi/provider credential access or copying.
- No synthetic-as-production acceptance claims.
- Explicit trust, restart, abort, and stop boundaries.
- Failure ambiguity is `uncertain`, never blind replay.

See [SECURITY.md](SECURITY.md) and [usage reference](references/usage.md).

## JSON CLI

Place `--json` before or after a public command for one schema-v1 envelope:

```json
{"schema_version":"1","command":"status","success":true,"data":{},"error":null}
```

The outer JSON envelope remains version 1; Supervisor API versioning is
independent. Payload bodies are never returned.

## Author and license

Created and maintained by [Revaz Zakalashvili](https://github.com/revazi).
Contact: [revaz.zakalashvili@gmail.com](mailto:revaz.zakalashvili@gmail.com).
Licensed under the [MIT License](LICENSE.md).

## Development

```bash
python -m pip install ruff==0.11.11
scripts/test.sh
```

Checks are model-free and isolate package/Pi/npm state from real authentication.

To stage and manually test the exact unreleased checkout without publishing or
replacing the installed package, follow
[`references/prerelease-testing.md`](references/prerelease-testing.md). The
recommended flow builds a persistent package root with commit/tarball provenance,
uses a disposable provider-free Pi home first, and keeps any real-auth/provider
acceptance explicit and temporary through `--no-extensions --no-skills -e`.
