---
id: ISSUE-0082
summary: "The per-request session/principal rail built by ISSUE-0081 (PR 2 `persatrix-session`, PR 3 `persatrix-principal`) is armed but never fed: the Go orchestrator resolves ONE session id per process at boot (`cmd/orchestrator/startup.go::resolveSessionID`) and emits no per-request gRPC headers, so every inbound request falls back to the persona-runtime construction snapshot. The cross-conversation memory bleed and cross-tenant leak that ISSUE-0081's Python vertical fixes therefore stay DORMANT until the orchestrator derives a per-request session id (unit = `(agent, channel, user)`, orchestrator-authoritative + persisted) and emits it — and, once auth lands, a per-request principal. Storage + transport + binding are all ready Python-side; this issue is the activation half."
status: open
severity: high
area: cmd/orchestrator
created: 2026-05-29
refs:
  - docs/issues/ISSUE-0081-session-id-process-global-not-task-local.md
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
