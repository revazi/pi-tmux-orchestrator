# Broker dashboard design

This document is the review contract for the `BROKER + STATUS` pane. The
broker remains the authoritative state writer and event source; tmux only hosts
and displays this projection. A dashboard refresh is requested by broker state
transitions or a supported terminal resize signal, never by a timer, polling
loop, or model turn.

## Operator hierarchy

The pane answers these questions in order:

1. **Where am I?** Product, exact session, project (when space permits).
2. **What needs attention?** Workflow state and round are the strongest line.
3. **How is it coordinated?** Worker transport, broker protocol, and actual
   provider-reported run tokens.
4. **Who is doing what?** One row per role: connection/generation, lifecycle,
   active assignment, configured provider/model/thinking, tokens, context, and
   a body-free assignment-guardrail marker when present.
5. **What just changed?** Up to eight newest metadata events, without IDs or
   bodies.
6. **What can I do?** Exact attach, status, confirmed stop, return, and zoom
   guidance.

Full-layout wireframe (values are illustrative metadata):

```text
PI TMUX ORCHESTRATOR  /  SESSION pi-example-agents
BROKER + STATUS  /  PROJECT /work/example
* ACTIVE   ROUND 2
TRANSPORT TUI   PROTOCOL BROKER-V1 / V1   ACTUAL USAGE 42.8k TOKENS

ROLES
ROLE         LINK       STATE       ASSIGNMENT          MODEL                    THINK   TOKENS   CTX
------------------------------------------------------------------------------------------------------
implementer  + up / g1  active      r2 implementation   anthropic/model-name     high     31.2k  62.4%
reviewer     + up / g1  idle        -                   google/model-name        medium   11.6k  28.1%

RECENT METADATA EVENTS
#00124  14:03:18  implementer  worker_lifecycle  active  r2
#00125  14:03:21  broker       context_delivered delivered

ACTIONS  attach: pi-tmux-agents attach pi-example-agents   status: pi-tmux-agents status pi-example-agents
         stop: pi-tmux-agents stop pi-example-agents --yes   tmux: prefix + L return / prefix + z zoom
```

## Semantic tokens

Color is redundant with labels, markers, grouping, and capitalization; it is
never the only state signal.

| Token | ANSI intent | States and uses |
|---|---|---|
| `success` | green | `ready`, connected/idle health, accepted/completed/delivered |
| `active` | cyan | `active`, `connecting`, `initializing`, delivering/streaming |
| `warning` | yellow | `needs_attention`, `waiting`, soft-budget warnings, assignment guardrails, context at 80%+ |
| `error` | red | `uncertain`, disconnected, error/failed/rejected/conflict |
| `muted` | dim neutral | secondary labels, timestamps, unknown/starting metadata |

The product heading and round use bold emphasis. No role receives a decorative
identity color, so the same state always has the same meaning across rows.

## Responsive contract

| Layout | Breakpoint | Preserved information |
|---|---|---|
| Full | width >= 100 and height >= 22 | Project, assignment, full role columns, bounded event rail, two-line actions |
| Compact | width >= 64 and height >= 12 | Identity/state, transport/protocol, one role row with generation, lifecycle, usage/context, assignment-guardrail marker, thinking/model; events when height remains |
| Narrow | otherwise | Identity/state first, one role health row each with assignment-guardrail marker; model/thinking/assignment details and events only when height remains; hidden roles are counted |

Every rendered line is limited to one column less than the pane width to avoid
a terminal auto-wrap, and frames never exceed pane height. Dynamic cells use a
Unicode ellipsis (or `.` in ASCII mode). Lower-priority event rows and details
are removed before identity, workflow state, role health, or command guidance.

## Plain and terminal modes

- An ANSI-capable TTY is repainted in place in one write; unchanged same-size
  frames are suppressed. Supported POSIX event loops register `SIGWINCH`, so a
  tmux resize or zoom recomputes the layout without polling. The renderer clears
  stale lines, hides the cursor only while the broker owns the pane, and
  restores cursor/style state on normal exit or errors.
- `NO_COLOR` disables semantic SGR color while retaining in-place TTY refresh.
- `TERM=dumb` and non-TTY streams emit no control sequences. They print one
  plain dashboard followed by bounded one-line metadata updates rather than
  repeating whole frames.
- Non-UTF-8 streams use ASCII markers, separators, and truncation.
- All dynamic text is control-code sanitized before width calculation and
  output.

## Intentional omissions

The dashboard is a control-plane summary, not another worker log:

- Task, prompt, operator-message, report, diff, log, credential, token-secret,
  endpoint, raw error, and provider request/response bodies are never selected.
- Assignment, delivery, report, command, and authentication IDs are omitted;
  they add diagnostic noise and are available through bounded metadata APIs
  where appropriate.
- Raw assistant/tool progress stays in worker panes. The event rail shows only
  sequence, time, role, event name, status, and round metadata.
- PIDs and inferred process liveness are omitted. Connection state comes from
  the live broker; retained reads continue to report host runtime as not
  observed.
- Cost, detailed token categories, full timestamps, and long model identifiers
  yield to state and role legibility at constrained sizes; exact retained data
  remains available through status/Supervisor APIs.
- Spinners, animations, progress estimates, and hard-budget gauges are omitted
  because they would imply polling or precision the broker does not have. A
  compact `G~`/`G!` prefix denotes a retained assignment warning/hard fact
  without presenting it as a live gauge.

These omissions keep the pane bounded, metadata-only, and useful for deciding
whether to attach, inspect status, intervene, or stop the exact session.

## Pi overlay projection

The parent extension also exposes `/or-dashboard`, a separate cross-session
overlay inspired by Pi Fallow and Pi Tasklight. It reads the bounded `list` JSON
projection plus current-project doctor metadata once on open and again only on
explicit `r`. It never tails pane output or starts a refresh timer. Each running
session row keeps exact session/project identity, workflow state/round, profile,
linked-role count, calls, operational tokens, complete provider-reported cost,
and maximum available role context pressure. Doctor remains a separate bounded
section, and missing provider values render as unavailable.

Arrows or `j`/`k` select; Enter closes the overlay and reuses the existing
watch/attach path; `d` toggles doctor; `q`/Escape closes. The invoking Pi must be
inside tmux for attach. Pi's public overlay API currently has no row-click
callback, so mouse activation is not emulated with terminal links. The profile
row is deliberately read-only until a later strict, confirmed configuration
editor is designed.
