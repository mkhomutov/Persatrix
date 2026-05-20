# Agent Runtime Vocabulary — Discussion Notes

**Status**: 🔨 Draft
**Author**: Maksim Khomutov
**Date**: 2026-05-20
**Target**: scope-shaping for draft RFCs 0041 / 0042 / 0043 / 0044
**Companion to**: [v0.3.x sequencing](v0.3.x-sequencing.md), [ROADMAP.md §RFC Master Index](../ROADMAP.md#rfc-master-index)

---

## Table of Contents

- [Why this doc exists](#why-this-doc-exists)
- [Scope and non-goals](#scope-and-non-goals)
- [The four seams](#the-four-seams)
  - [Seam 1 — typed events as the agent-turn primitive](#seam-1--typed-events-as-the-agent-turn-primitive)
  - [Seam 2 — lifecycle callbacks](#seam-2--lifecycle-callbacks)
  - [Seam 3 — scoped state namespaces](#seam-3--scoped-state-namespaces)
  - [Seam 4 — inbound agent-interop endpoint](#seam-4--inbound-agent-interop-endpoint)
- [Eval-set shape as the regression gate](#eval-set-shape-as-the-regression-gate)
- [Recommended sequencing](#recommended-sequencing)
- [What this doc does not propose](#what-this-doc-does-not-propose)
- [Related documentation](#related-documentation)

---

## Why this doc exists

Four observations about Persatrix's agent runtime have converged from independent threads of recent work:

1. **Error replies are being threaded into the channel-message path ad-hoc.** PRs [#395](https://github.com/mkhomutov/Persatrix/pull/395) (ISSUE-0065 — wallet denial) and [#396](https://github.com/mkhomutov/Persatrix/pull/396) / [#398](https://github.com/mkhomutov/Persatrix/pull/398) (ISSUE-0066 — wallet lease-cap / rate-limit and `AioRpcError(RESOURCE_EXHAUSTED)`) all solve the same shape of problem: a turn fails partway through, and the system has nowhere to put the failure event except as a synthesized chat reply. The fix worked but the seam — *what is the typed thing that just happened?* — is missing.

2. **The persona-memory pipeline has several "the right place to inject this" points that the code currently expresses as method overrides or call-site edits.** The F-3 recall filter ([RFC 0031](rfcs/0031-per-session-namespacing-channels.md) Phase 2), the [persona quality bar](memory-quality-roadmap.md#quality-bar--the-dementia-test) gate, the wallet lease pre-charge ([RFC 0023](rfcs/0023-llm-call-leasing.md)), and the prompt-redaction work ([RFC 0009](rfcs/0009-security-sandboxing.md) §`InputSanitizer`) all want the same seam: "fire before the model is called; mutate the context; veto if necessary." Today each of these threads its own hook in its own place.

3. **State is multi-scoped but flat.** Persona state, channel state, per-interaction working memory ([RFC 0034](rfcs/0034-persona-conversational-working-memory.md)), per-session namespaces ([RFC 0031](rfcs/0031-per-session-namespacing-channels.md)), and ephemeral tool state all coexist in code without a uniform convention for *which scope a piece of state belongs to and how long it lives*. New work has to re-decide each time.

4. **The agent contract is internal-only.** Every agent that wants to participate in a channel today has to speak the orchestrator's internal gRPC contract. There is no edge surface for an external participant — human-facing CLI/web UI ([RFC 0016](rfcs/0016-human-participant-chat-interface.md)) is the only inbound shape, and it does not generalize to "another agent system wants to join a channel."

These look like four unrelated complaints. They are, in fact, the same complaint at four layers of the stack: **the runtime lacks a vocabulary**. There is no typed name for "a thing that happened in a turn," no named hook for "where work plugs in," no named scope for "where state lives," and no named edge for "how non-Persatrix participants join."

This doc proposes that vocabulary, mapped to four RFCs. It is not itself an RFC — it is the planning step that sits between the observations and the RFCs that act on them.

## Scope and non-goals

**In scope.** Naming the four seams, stating their shared motivation, fixing their dependency order, and pointing to the RFC that owns each.

**Out of scope.** Editing accepted RFCs. Adopting any external framework's runtime, services, or wire format. Changing the orchestrator's internal gRPC contract ([RFC 0040](rfcs/0040-agent-orchestrator-transport-unification.md) already owns that direction). Multi-node mesh or cross-org federation ([RFC 0012](rfcs/0012-protocols-organizations.md) territory).

---

## The four seams

### Seam 1 — typed events as the agent-turn primitive

**Owned by**: [RFC 0041 — Typed Event Taxonomy and Lifecycle Callbacks](rfcs/0041-typed-event-taxonomy-lifecycle-callbacks.md).

A turn today produces a heterogeneous mix of side effects — model output, tool calls, tool results, state mutations, log lines, error replies, telemetry spans — and the consumers (channel publish, structured logger, tracer, dead-letter queue, eval harness) each pick what they need from the agent loop by reaching into different fields. The proposal is one typed event union (`ModelOutput`, `ToolCall`, `ToolResult`, `StateDelta`, `Error`, `Control`) emitted as a single ordered stream per turn, and one set of consumers that subscribe to it. The error-reply work for ISSUE-0065 / ISSUE-0066 becomes typed `Error` events with `kind=wallet_denied | lease_cap | rate_limit | resource_exhausted` rather than synthesized chat replies.

### Seam 2 — lifecycle callbacks

**Owned by**: [RFC 0041 — Typed Event Taxonomy and Lifecycle Callbacks](rfcs/0041-typed-event-taxonomy-lifecycle-callbacks.md) — co-designed with Seam 1 because callbacks take and emit events.

Four named hooks — `before_model`, `after_model`, `before_tool`, `after_tool` — give the F-3 recall filter, the persona quality-bar gate, the wallet lease pre-charge, and the prompt redactor a uniform place to live. Each hook is a pure function from `(context, event_stream_so_far)` to a list of new events plus an optional veto. Today the same work is woven into the agent loop at different points; the seam makes the pipeline auditable and individually testable.

### Seam 3 — scoped state namespaces

**Owned by**: [RFC 0042 — State Namespacing by Scope Prefix](rfcs/0042-state-namespacing-by-scope.md).

A prefix convention — `app:` / `persona:` / `channel:` / `session:` / `interaction:` / `temp:` — declares where a piece of state lives and how long it persists. The same key in two scopes is two different facts (`channel:#planning:topic` vs `interaction:abc:topic`). The convention is shallow on purpose: it removes a class of "where does this belong" decisions without forcing a storage migration. Couples to Seam 1 only via `StateDelta` event payloads carrying namespace metadata.

### Seam 4 — inbound agent-interop endpoint

**Owned by**: [RFC 0043 — Inbound Agent-Interop Endpoint](rfcs/0043-inbound-agent-interop-endpoint.md).

A bounded inbound surface that lets a non-Persatrix agent join a channel — speak, be addressed, hear other participants — without speaking the internal orchestrator gRPC contract. The internal contract stays as-is; this is an adapter at the edge, scoped narrowly enough that it cannot be a back-door into any orchestrator API the internal contract does not already expose. Orthogonal to Seams 1–3.

## Eval-set shape as the regression gate

**Owned by**: [RFC 0044 — Eval-Set Shape with Golden Traces](rfcs/0044-eval-set-golden-traces.md).

Seams 1 and 2 change what the runtime *emits*. Seam 3 changes where state *lives*. Seam 4 changes who can *join*. None of those changes are testable today against a regression bar — [`evaluators/`](../evaluators) has scenario runners but no codified golden-trace format that asserts "this sequence of typed events in this order, this state at this scope, this final transcript." RFC 0044 specifies that format and seeds it with the [dementia test](memory-quality-roadmap.md#quality-bar--the-dementia-test) and the F-3 recall scenario. Landing this *first* means the other three RFCs ship against an existing bar instead of inventing one each.

---

## Recommended sequencing

```
RFC 0044 — Eval-set shape with golden traces           (gates the others)
    ↓
RFC 0041 — Typed events + lifecycle callbacks          (core agent-loop change)
    ↓
RFC 0042 — Scoped state namespaces                     (uses StateDelta events)
    ↓
RFC 0043 — Inbound agent-interop endpoint              (edge surface, independent)
```

`0043` is genuinely independent of `0041`/`0042` and could land in parallel. It is sequenced last because its review surface (security, auth, scope of external exposure) is the heaviest and benefits from the other three already framing what an external participant can and cannot do.

No effort estimates, no calendar gates. Each RFC carries its own phased plan; this doc only fixes the *order*, not the cadence.

---

## What this doc does not propose

- **Replacing the orchestrator runtime.** The Go orchestrator, the Python agent workers, and the gRPC contract between them stay as they are. These four seams are vocabulary additions inside the existing architecture, not a re-platform.
- **A new framework dependency.** No third-party agent runtime, service interface, or wire format is adopted. Each seam is a Persatrix-native shape motivated by Persatrix-native incidents.
- **Changing the YAML workflow language.** [`workflows/`](../workflows) and [`blueprints/`](../blueprints) are unaffected.
- **Memory architecture changes.** [`memory-quality-roadmap.md`](memory-quality-roadmap.md) and [`storage-architecture-roadmap.md`](storage-architecture-roadmap.md) own the memory layer; these RFCs only touch memory at the hook seam (where the F-3 recall filter and quality-bar gate plug in).

---

## Related Documentation

- [RFC 0041 — Typed Event Taxonomy and Lifecycle Callbacks](rfcs/0041-typed-event-taxonomy-lifecycle-callbacks.md)
- [RFC 0042 — State Namespacing by Scope Prefix](rfcs/0042-state-namespacing-by-scope.md)
- [RFC 0043 — Inbound Agent-Interop Endpoint](rfcs/0043-inbound-agent-interop-endpoint.md)
- [RFC 0044 — Eval-Set Shape with Golden Traces](rfcs/0044-eval-set-golden-traces.md)
- [RFC 0040 — Agent–Orchestrator Transport Unification](rfcs/0040-agent-orchestrator-transport-unification.md) — the internal gRPC contract these RFCs leave untouched
- [Memory Quality Roadmap](memory-quality-roadmap.md) — companion discussion doc for the memory layer
- [Storage Architecture Roadmap](storage-architecture-roadmap.md) — companion discussion doc for storage
- [ISSUE-0065](issues/ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md) / [ISSUE-0066](issues/ISSUE-0066-chat-rest-resource-exhausted-no-channel-reply.md) — the error-reply incidents that motivated Seam 1
