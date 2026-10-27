# Provider-call admission: provider-free capability evidence

Issue [#182](https://github.com/revazi/pi-tmux-orchestrator/issues/182) needs
broker-coordinated authorization before ordinary worker requests. The absence
of that authorization in the existing worker is an implementation gap, not
proof that Pi cannot support it.

## Reproduce the boundary check

```bash
node tests/pi-provider-admission-contract.mjs
# Or supply the root of an isolated installed Pi package:
node tests/pi-provider-admission-contract.mjs /absolute/path/to/pi-coding-agent
```

This standalone probe explicitly requires the reviewed Pi **1.4.0** SDK
contract. It checks the package version before constructing sessions: older
SDK factories may ignore the supplied model runtime and fall back to
file-backed authentication. It does not silently accept another SDK version,
change the project's compatibility floor, or establish a compatibility matrix.

The probe uses real `createAgentSession`, extension dispatch, agent abort, and
OpenAI completions/responses adapters. Registry/auth resolution and resource
discovery are replaced by explicit in-memory fixtures. Every request has an
injected fetch sentinel that counts attempted dispatch and throws without
network I/O. A separate global-fetch sentinel catches accidental fallback.
There is no provider response emulation, credential-file read, or inference.

Sixteen checks cover both API adapters and TUI/RPC extension bindings:

| Hook action | Injected fetch dispatches | Final assistant stop reason |
|---|---:|---|
| Allow | 1 | `error` from the synthetic fetch sentinel |
| Throw only | 1 | `error` from the synthetic fetch sentinel |
| Abort, then throw after asynchronous denial | 0 | `aborted` |
| Abort, then throw after authorization failure | 0 | `aborted` |

The TUI binding invokes the installed interactive mode's real queued-message
abort adapter with synthetic rendering/queue inputs; RPC uses the SDK's default
session-abort binding. Neither test starts a terminal or tmux worker.

**Throwing from `before_provider_request` alone is fail-open.** Pi catches the
extension error and continues with the payload. In the tested SDK and adapters,
`ctx.abort()` synchronously aborts the current request signal before dispatch.
Do not await `session.abort()` from this hook: it waits for the active run to
settle, while the run is waiting for the hook.

## What this enables, and what remains

The initial static-start implementation uses this boundary; see the
[enforcing call-ceiling operator guide](usage.md#opt-in-enforcing-provider-call-ceilings-static-starts).
This probe establishes the dispatch boundary, while separate broker and framed
worker tests exercise reservation/pause/continuation behavior. A gate must await authenticated single-writer broker
admission before proceeding and abort on denial, disconnection, timeout, or
invalid/stale authorization. It must not rely on an exception alone.

This probe does **not** implement ceilings, atomic reservations, continuation,
restart/recovery accounting, budget-pause routing, or mandatory-review
preservation. It does not cover other transports, provider-internal retries,
cache-warming/compaction requests, or other Pi versions. TUI/RPC hook bindings
are not a substitute for staged-package tmux lifecycle acceptance.

Cost enforcement is a separate capability. The installed Pi AI
`calculateCost(model, usage)` calculates `usage.cost` from model catalog rates.
The worker currently sums `usage.cost.total` without a provider-billing source
marker. These values must not be relabeled as actual provider-reported spend.
A requested actual-cost ceiling needs trustworthy provenance or explicit
fail-closed cost-unavailable behavior; this call-admission probe does not
supply billing data.

No production wire acceptance, complete #182 implementation, or spending
savings are claimed by these checks.
