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
>
> 2026-08-05 — **v0.3.14 PR 1 (the dormant rail) is open**:
> `grpcmeta.MDPrincipal` + `InjectPrincipal` (empty → no-op),
> `channels.WithPrincipal`/`PrincipalFromContext` (pinned to survive the
> fanout's `context.WithoutCancel` hop — the propagation property), the
> `Dispatch` injection with the `principal.id` span attribute, and the
> Go-side lockstep guard asserting the bare literal
> `"persatrix-principal"` (completing the cross-language pair with
> `test_principal_id_leaf_module.py`). Dormancy is pinned, not assumed:
> absence tests assert no header and no span attribute ride a dispatch
> whose ctx carries no principal. The producer is PR 2.
>
> 2026-08-06 — **PR 1 review: the propagation lock covers descent, not
> every orchestrator-authored dispatch.** The wording was corrected in
> the carrier's doc comment, the plan's propagation-posture bullet and
> the changelog entry. A path that builds a *fresh* context reaches
> `Dispatch` with no principal on it, and the origin audit's
> `handleConveneChannel` is not the only one: the synthesis-close
> timeout (`ChannelRouter.onSynthesisTimeout`,
> `internal/channels/synthesis_close.go`) hands `context.Background()`
> to `boundedClose`, so the close-notification fan it drives descends
> from no request. Principal is the **only** axis a ctx reset exposes —
> session re-resolves through the SessionResolver, epoch falls back to
> the boot value, neither reads a ctx value — so under PR 2 a
> close-notification turn descending from an authenticated publish would
> be ingested persona-side under the shared `'local'` principal: one
> person's turn written into the shared tenant. Both origins are PR 2's
> to close (the cleanest shape for the timeout is stamping the detached
> ctx onto the pending-synthesis record at arm time rather than
> constructing `Background()` at fire time).
>
> 2026-08-06 — **v0.3.14 PR 2 (the producer) is open — the rail is fed.**
> The RFC 0039 §F verified `participant_id` is threaded onto the request
> context and emitted at the dispatch chokepoint, so under
> `auth.mode: enabled` persona memory partitions by authenticated person.
> Three notes on how it landed, each a deviation or a finding rather than
> a restatement of the plan:
>
> 1. **Threaded in `authMiddleware`, not per handler.** The plan's lock
>    said "the REST handlers thread the resolved identity"; the top-ranked
>    risk was a *missed* origin, which fails open into the shared `'local'`
>    tenant with no error and no red test. Threading at the one place
>    identity is resolved — the middleware wrapping the root mux — makes
>    exhaustiveness a property of the composition instead of a
>    hand-maintained list. The predicate is `authIdentity.Authenticated`,
>    not `authEnforced()`: under `enabled` an unauthenticated caller on a
>    public route resolves `anonymousIdentity`, whose participant is the
>    literal `"local"`, and stamping that would put a header on the wire
>    where there is none today for no change in resolved value. The
>    enumeration survives as `dispatchOriginClassification` +
>    `principal_route_table_test.go`, which parses the package's own
>    registrations and fails on an unclassified route — documentation kept
>    honest by a test, not a gate.
> 2. **The origin set is three routes**, all confirmed rather than
>    assumed: channel publish, chat, and `handleConveneChannel`. The two
>    the plan left open are both **non-dispatching**: `workflows/run`
>    schedules through the executor's `ExecuteTask` RPC, which has no
>    persona-memory principal consumer at all (the Python servicer lifts
>    `persatrix-principal` in `SendChatMessage` and
>    `ReceiveChannelMessage` only), and `handleRecallMessages` reads the
>    **channel** store — membership-scoped verbatim messages, a table with
>    no `principal_id` column — never principal-partitioned persona memory.
> 3. **The synthesis-close reset is fixed as directed** (the principal
>    string is stashed at arm time, not the whole detached ctx — that
>    string is the entire delta a fresh context loses, and retaining a
>    context on a record that lives for the ~2-minute timeout window would
>    pin its values and span for no benefit). It was also the **only**
>    fresh-context dispatch path in `internal/channels`: every other
>    detachment is `context.WithoutCancel`, which preserves values.
>
> Live proof is `MT-MEMORY-MULTIUSER-001`, authored here and executed at
> release-prep. Authoring it surfaced a scaffolding constraint worth
> recording: **RFC 0039 Phase 3 (account administration) is v0.4.0, so
> `account bootstrap` — which refuses once any account exists — is the
> only shipped account-creation verb.** The MT therefore rotates the
> second principal by deleting `accounts.db` and re-bootstrapping (the
> persona's `memory.db` is a separate store, so the corpus survives),
> which makes the two accounts sequential rather than concurrent. The
> concurrent case is pinned deterministically instead
> (`tests/integration/test_principal_emission_isolation.py`: one process,
> two scopes, one shared room). This is a live-testing ergonomics gap, not
> a product gap, and it closes with Phase 3.
>
> 2026-08-06 — **two residuals surfaced at PR 2 review; accepted and
> stated, not fixed.** Both are cases where the per-turn boundary this PR
> lands holds, but a *derived* or *relayed* write does not inherit it.
> Neither is closed by v0.3.14; both are recorded here so the release
> note's claim can be read at the right scope.
>
> **R-1 — the interaction close writes a single-tenant aggregate of
> multi-speaker content.** The persona's RFC 0020 interaction scope is the
> ROOM, not the speaker: `scope_for_channel_event`
> (`agents/memory/scopes.py`) keys on `channel_id` / `thread_id` /
> `sender_id`, never on the principal, so in a group channel every
> speaker's turns accumulate into ONE `InteractionTracker` record. At
> close, that whole record is summarised **and facts are extracted from
> it** (`agents/persona_runtime/summarize_close.py`), inside the closing
> turn's principal binding — `_on_event_inner` runs under
> `request_scope_from_metadata` (`agents/persona_runtime/__init__.py`),
> and the close-notification handler runs on the gate-suppress path within
> it. So the summary and its extracted facts land under whichever
> principal the CLOSING turn carried, while their content spans everyone
> who spoke in the interaction. Facts are cross-room by default (RFC 0049
> Phase 1), so in a shared room a fact derived from B's disclosure can be
> written under A's principal and recalled by A in a different room.
>
> This is not the timeout stash's doing — the ordinary bounded close
> already descends from the publish that crossed the bound, so it is on
> the main path. It is also why the close-path asymmetry the review
> flagged is left as-is rather than papered over: the synthesis-REPLY and
> end-vote closes descend from the chair persona's own unauthenticated
> publish and so carry no principal, while the bound-crossing and timeout
> closes carry the triggering/arming person's. Making those consistent by
> extending the stash would make the single-tenant aggregate write
> *systematic* on every close path; making them consistent the other way
> (aggregates always `'local'`) would put a person's own interaction
> summaries and close-extracted facts out of reach of their own
> authenticated turns, which is a regression for the single-user
> deployment. Neither is right while the aggregate is multi-speaker. The
> real fix is **per-speaker interaction scope** — keying the tracker scope
> by principal so each speaker gets their own record, summary and
> extraction in a shared room — which is an RFC 0020 §G scope-routing
> change and belongs to the v0.4.0 line, not to this release. Until then
> the asymmetry stands, documented as a symptom.
>
> **R-2 — the orchestrator hop drops the tenant on agent cascades.** A
> persona's reply re-enters through `HTTPChannelPublisher`
> (`agents/channel_publisher.py`) as a fresh, unauthenticated REST
> publish, so every fanout descending from it dispatches with no
> principal and the receiving personas write under `'local'` — even
> inside an interaction an authenticated person started. In a multi-agent
> room, agent B's restatement of A's disclosure is therefore written to
> the shared tenant, which every agent-origin and autonomous turn can
> recall and speak into any room it is a member of. Note the two cascade
> routes now differ: the IN-PROCESS `EventDispatcher` cascade *does*
> forward the axis (`origin_principal_id`, `agents/action_executor.py`);
> only the orchestrator-mediated hop loses it.
>
> The obvious fix is not available. Having the persona send the principal
> back on its outbound publish would require the orchestrator to trust an
> agent-supplied identity claim — and because the persona binds
> `principal_scope` from that header and recall is strict equality on it,
> that hands an unauthenticated caller a cross-tenant **read** primitive,
> which is strictly worse than the leak it closes. The only safe shape is
> server-side causal tracking (the orchestrator holding, per channel and
> agent, the principal of the human publish that dispatched to it, and
> re-stamping the reply from that server-held state) — new machinery with
> real edge cases (several people dispatching to one agent, interleaving,
> TTL, a reply landing after someone else spoke). Deferred, not designed
> here.
>
> **Neither residual is covered by `MT-MEMORY-MULTIUSER-001`**, which
> drives `persatrix chat` against a single persona: a DM has no second
> speaker to aggregate and no agent-to-agent cascade. Both would need a
> multi-agent group-channel MT to be observed live.
>
> 2026-08-18 — **R-1 and R-2 re-slotted v0.4.0 → v0.3.15**, so interaction
> functionality is complete before v0.4.0 organizations build on it. Their
> designs (`ISSUE-0123` / `ISSUE-0124`) sit in draft
> [#822](https://github.com/mkhomutov/Persatrix/pull/822), whose Phase 0 gate
> measures against the **v0.3.14 tag** — so it starts after this release.
>
> **R-3 — the catch-up replay re-derives memory under the default
> principal**, filed as
> [ISSUE-0130](ISSUE-0130-catchup-replay-rederives-memory-under-default-principal.md)
> from the release-prep PR 1 live arc, which carries the evidence and the fix
> shapes. Unlike R-1/R-2 it is not blocked by the agent-supplied-claim trust
> problem, and is fixed **inside v0.3.14**.
>
> **This issue stays `open` through release-prep PR 1** — a recorded
> deviation from the [plan](../v0.3.14-release-prep-plan.md). It closes when
> the ISSUE-0130 leak-stopper lands.
