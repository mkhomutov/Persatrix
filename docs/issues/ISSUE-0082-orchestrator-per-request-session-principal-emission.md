---
id: ISSUE-0082
summary: "The per-request session/principal rail built by ISSUE-0081 (PR 2 `persatrix-session`, PR 3 `persatrix-principal`) is armed but never fed: the Go orchestrator resolves ONE session id per process at boot (`cmd/orchestrator/startup.go::resolveSessionID`) and emits no per-request gRPC headers, so every inbound request falls back to the persona-runtime construction snapshot. The cross-conversation memory bleed and cross-tenant leak that ISSUE-0081's Python vertical fixes therefore stay DORMANT until the orchestrator derives a per-request session id (unit = `(agent, channel, user)`, orchestrator-authoritative + persisted) and emits it — and, once auth lands, a per-request principal. Storage + transport + binding are all ready Python-side; this issue is the activation half."
status: open
severity: high
area: cmd/orchestrator
created: 2026-05-29
refs:
  - docs/issues/ISSUE-0081-session-id-process-global-not-task-local.md
  - docs/issues/ISSUE-0082-part1-session-emission-pr-plan.md
  - docs/rfcs/0031-per-session-namespacing-channels.md
  - docs/rfcs/0039-user-accounts-authentication.md
  - cmd/orchestrator/startup.go
  - agents/session_metadata.py
  - agents/session_id.py
  - agents/principal_id.py
---

## Summary

ISSUE-0081's Python vertical (PRs [#453](https://github.com/mkhomutov/Persatrix/pull/453) / [#454](https://github.com/mkhomutov/Persatrix/pull/454) / [#455](https://github.com/mkhomutov/Persatrix/pull/455) / [#456](https://github.com/mkhomutov/Persatrix/pull/456)) moved the session id from process-global-cached to **task-local, resolved at call time**, added a `principal_id` tenant dimension with strict isolation, and laid the gRPC + event-envelope rail that binds both per request inside `on_event`. All of that is **armed but not fed**:

* The Go orchestrator still calls `resolveSessionID` **once at boot**
  (`cmd/orchestrator/startup.go:39`) and exports a single
  `PERSATRIX_SESSION_ID` into the persona-runtime process.
* It emits **no** per-request `persatrix-session`
  (`agents.session_id.SESSION_METADATA_GRPC_KEY`) header, and **no**
  `persatrix-principal` (`agents.principal_id.PRINCIPAL_METADATA_GRPC_KEY`).
* So `agents.session_metadata._session_from_context` /
  `_principal_from_context` always return `None`, and every handler runs
  under its construction snapshot — exactly the pre-ISSUE-0081 behaviour.

Net: a single-session-per-process deployment is unchanged and correct,
but the cross-conversation and cross-tenant isolation the Python work
provides only activates when the orchestrator emits per-request ids.

## Context

Spawned from the ISSUE-0081 closeout (PR 4, [#456](https://github.com/mkhomutov/Persatrix/pull/456)). ISSUE-0081 stays open until this lands; it documented the deferral inline (see its §Notes and the RFC 0031 §B/§C/§E amendments). RFC 0031's §B amendment already records the chosen **session unit = `(agent, channel, user)`**, **orchestrator-authoritative + persisted** (so a multi-day dementia-test arc survives a persona-process restart).

## Impact

* **Cross-conversation memory bleed (latent).** Any deployment where one
  persona process fields more than one conversation concurrently still
  shares one `(agent_id, session_id)` namespace until per-request
  `persatrix-session` is emitted. The fix is built; it just is not wired
  to a source.
* **Cross-tenant leak (blocks multi-user).** The `principal_id` dimension
  defaults every request to `'local'` until a verified principal is
  emitted; multi-user isolation cannot be exercised end-to-end without it.
* **No regression risk.** Until activated, behaviour matches today's
  single-session-per-process model exactly.

## Proposed fix / investigation path

Two independently shippable parts:

1. **Session emission (self-contained Go follow-up).** Derive the
   per-request session id from the `(agent, channel, user)` unit; own +
   persist it orchestrator-side (so it survives a persona-process
   restart); emit it as the `persatrix-session` gRPC header on every
   outbound dispatch / channel-message call. Pin end-to-end with a test
   that two concurrent conversations for one agent recall in isolation
   through the live gRPC path (the Python side is already covered by
   `tests/unit/python/test_principal_scope.py` + the PR 2 binding tests).

2. **Principal emission (gated on [RFC 0039](../rfcs/0039-user-accounts-authentication.md)).**
   Once authenticated accounts exist, emit the verified principal as the
   `persatrix-principal` header. Until then the storage layer correctly
   collapses to the single-tenant `'local'` principal.

Storage (migrations), transport keys, and the `on_event` binding are all
in place — this issue is wiring the **source** of the two ids, not the
mechanism.

## Notes

> 2026-05-29 — filed at the ISSUE-0081 PR 4 closeout so the remaining
> activation work is discoverable on the issues index rather than living
> only in ISSUE-0081's notes. Severity mirrors ISSUE-0081 (high — it gates
> the multi-conversation / multi-user story) but is **latent** today: no
> shipped deployment serves multiple conversations or tenants from one
> process, so nothing leaks until that ships *and* this is wired.
>
> 2026-05-29 — **Part 1 (session emission) landed.** PR 1 added the
> persisted `(agent, channel, user) → session_id` binding store
> (`internal/channels/session_binding.go`, migration v4) — the
> orchestrator-authoritative source RFC 0031 §B specifies. PR 2 wired its
> emission: `GRPCMessageDispatcher.Dispatch` resolves the binding and
> injects `persatrix-session` (`internal/observability/grpcmeta`) on every
> outbound `ReceiveChannelMessage`, feeding the ISSUE-0081 rail on the
> session axis. PR 3 lands the end-to-end gate
> (`tests/integration/test_session_emission_isolation.py`): a real gRPC
> `persatrix-session` header freezes the interaction's session, two
> concurrent conversations for one agent recall in isolation, and a
> pre-activation `legacy` row stays visible to both. RFC 0031 §B/§E
> amendments updated to record the session axis is now active. **This
> issue stays open for Part 2 (principal emission)**, gated on
> [RFC 0039](../rfcs/0039-user-accounts-authentication.md): until a
> verified principal source exists the orchestrator emits nothing on the
> `persatrix-principal` rail and the storage layer correctly collapses to
> the single-tenant `'local'` principal.
>
> 2026-08-03 — **Part 2 inherits the executor-hop principal threading
> (ISSUE-0118 closeout).** v0.3.13 PR 1
> ([#809](https://github.com/mkhomutov/Persatrix/pull/809)) threaded the
> per-request epoch/session axes across the executor hop
> (`DispatchContext.for_event` lift + `request_scopes()` re-entry) and
> initially left the **principal** axis out — its rail has no live
> producer, and without the threading, the moment Part 2's producer
> emitted, everything the executor runs AFTER `on_event` returns (the
> end-vote close discharge, legacy in-process cascade children,
> `SendChatMessage`'s post-reply execute) would have resolved the
> *construction* principal instead of the request's: the exact leak
> class ISSUE-0118 closed for epoch, resurfacing on the strict-equality
> tenant axis. **The same PR closed that gap** (review finding 4) rather
> than deferring it here: `agents/principal_id.py` gained the
> `principal_id_from_metadata` leaf reader (the handler binder now reads
> through it — one validation seam), `DispatchContext` carries
> `origin_principal_id` (lifted in `for_event`, re-entered by
> `request_scopes()` in the binder order session → principal → epoch),
> the legacy cascade's child events seed
> `EVENT_PRINCIPAL_METADATA_KEY` beside the epoch/session keys, and
> `SendChatMessage` threads `request_principal` onto its post-reply
> context. The rail is DORMANT — nothing emits principals yet, so
> behaviour is unchanged everywhere — and the threading is pinned by
> `tests/unit/python/test_dispatch_context_scope_threading.py`. What
> remains for Part 2 is the **emission itself**: the orchestrator's
> per-request principal producer feeding the `persatrix-principal` rail
> (gated on [RFC 0039](../rfcs/0039-user-accounts-authentication.md)'s
> verified claim), plus live verification that an emitted principal
> reaches both the handler binding and the executor re-entry. No
> v0.3.14 plan doc exists yet; when it opens, carry the emission +
> live-verification scope (not the threading — it already shipped) into
> the ISSUE-0082 Part 2 scope section.
>
> 2026-08-05 — **Part 2 scoped: the [v0.3.14 plan](../v0.3.14-plan.md) is
> open** (the hand-off the note above asks for). Locks taken at the plan
> opening: **derivation source** = the RFC 0039 §F verified
> `participant_id` off `authIdentity` (not the account id — RFC 0039 §A
> binds them 1:1, and §F already stamps this same value as the chat
> surface's verified claim, so memory and conversation name one
> identity); **surface** = a new `grpcmeta.MDPrincipal`
> (`persatrix-principal`, byte-matched to
> `agents.principal_id.PRINCIPAL_METADATA_GRPC_KEY` by a lockstep guard)
> injected at the same `GRPCMessageDispatcher.Dispatch` chokepoint that
> emits session + epoch, fed by a request-ctx carrier the REST handlers
> set from `identityFrom(r.Context())` (the `WithSessionOverride`
> precedent); **`auth.mode: enabled`-only** — under `disabled`, or for
> any unauthenticated caller, nothing is emitted and the persona keeps
> resolving `'local'` byte-identically; **propagation** = the principal
> rides orchestrator-authored hops descending from a principal-bearing
> publish (parity with the Python legacy-cascade seeding v0.3.13 PR 1
> landed), while agent/autonomous-origin turns emit nothing. Split
> across plan PR 1 (the dormant rail) and PR 2 (the producer + the
> end-to-end gate `tests/integration/test_principal_emission_isolation.py`
> + `MT-MEMORY-MULTIUSER-001`, run live at release-prep). This issue
> closes with PR 2's live verification.
>
> Two further locks came out of the planning review, both folded into the
> plan. **Origin set = enumerated, not sampled**: a missed dispatch
> origin fails *open* — no error, no red test, just a silent collapse to
> `'local'` where two people's rows co-mingle — and the audit already
> found a third origin beyond `handlePublishMessage` and the chat
> handler, `handleConveneChannel`
> (`internal/server/channel_convene_handlers.go` calls
> `channelRouter.ConveneChannel(r.Context(), …)` directly, so it descends
> from no publish and the propagation lock does not cover it).
> `workflows/run` and the `handleRecallMessages` read surface are
> classified in PR 2 rather than assumed, and a route-table test pins the
> classification so a later route cannot leak by omission.
> **Activation day**: migration v11 backfilled every pre-existing row to
> `'local'` and the principal predicate is strict equality with
> deliberately no `legacy`-style carve-out, so a deployment that has run
> `auth.mode: enabled` since v0.3.12 finds each persona's accumulated
> memory unreachable the day emission lands. The session axis absorbed
> its equivalent via the §D carve-out; the principal axis cannot, since
> an always-visible principal *is* the cross-tenant bridge the boundary
> forbids. The reset is therefore accepted and made visible — an MT leg
> observes it, and the release notes + Known Gaps state it with the
> operator remedy.
