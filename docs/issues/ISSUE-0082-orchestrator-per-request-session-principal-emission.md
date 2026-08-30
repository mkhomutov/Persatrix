---
id: ISSUE-0082
summary: "The per-request session/principal rail built by ISSUE-0081 (PR 2 `persatrix-session`, PR 3 `persatrix-principal`) is armed but never fed: the Go orchestrator resolves ONE session id per process at boot (`cmd/orchestrator/startup.go::resolveSessionID`) and emits no per-request gRPC headers, so every inbound request falls back to the persona-runtime construction snapshot. The cross-conversation memory bleed and cross-tenant leak that ISSUE-0081's Python vertical fixes therefore stay DORMANT until the orchestrator derives a per-request session id (unit = `(agent, channel, user)`, orchestrator-authoritative + persisted) and emits it — and, once auth lands, a per-request principal. Storage + transport + binding are all ready Python-side; this issue is the activation half."
status: resolved
severity: high
area: cmd/orchestrator
created: 2026-05-29
closed: 2026-08-18
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
> 2026-08-03 → 2026-08-06 — **the Part 2 build log lives in its own
> companion**: [ISSUE-0082 Part 2 — the v0.3.14 build log](ISSUE-0082-part2-v0314-build-log.md)
> (scoping, the dormant rail, the propagation-lock review finding, the
> producer PR). Split out 2026-08-23 at the 3 000-word prose cap; no
> decision moved with it.
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
> 2026-08-06 — **both residuals are now designed in their own files**:
> [ISSUE-0123](ISSUE-0123-per-speaker-interaction-scope.md) (R-1) and
> [ISSUE-0124](ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md)
> (R-2). Two conclusions carry back. R-1 binds the record's own frozen
> principal at close, **retiring** the asymmetry above rather than
> resolving it. And the two **must ship together**: R-1 alone leaves a
> systematic `'local'` record holding every agent turn in every room;
> R-2 alone leaves the aggregate. The group-channel MT asked for above
> is
> [MT-MEMORY-GROUP-TENANT-001](../manual-tests/MT-MEMORY-GROUP-TENANT-001.md),
> runnable both ways. Nothing is implemented — both fixes are v0.4.0.
>
> 2026-08-18 — **R-1 and R-2 re-slotted v0.4.0 → v0.3.15**, so interaction
> functionality is complete before v0.4.0 organizations build on it. Designs
> ([ISSUE-0123](ISSUE-0123-per-speaker-interaction-scope.md) / [ISSUE-0124](ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md)) sit in draft
> [#822](https://github.com/mkhomutov/Persatrix/pull/822); its Phase 0 gate
> measures against the v0.3.14 tag, so it starts after this release.
>
> 2026-08-18 — **RESOLVED.** The recorded two-principal run landed at PR 1
> ([execution report](../manual-tests/v0.3.14-execution-report.md)) and the
> [ISSUE-0130](ISSUE-0130-catchup-replay-rederives-memory-under-default-principal.md)
> leak-stopper at PR 1a, so no unattributable memory reaches the shared
> tenant. The Part 2 promise is met at the scope it claims: the per-turn
> boundary on the live dispatch. Open by design beyond it — R-1, R-2, and
> ISSUE-0130's **(b)** — all v0.3.15.
