---
id: ISSUE-0085
summary: "Under the scope-axes reframing, `session` becomes the room-continuity unit `(agent, channel)` — it accumulates and never auto-resets — so it can no longer be the home of F-3 run/test isolation. F-3's bleed spans both room-scoped memory (episodes) and person-scoped memory (relationship, person-facts), so a fresh channel name cannot isolate a rerun (relationship/person-facts are keyed on the participant and survive a room rename). Add a dedicated orthogonal `epoch_id` axis (default `live`), modeled on the `principal_id` axis: strict equality, **no `legacy` carve-out** (a fresh epoch sees nothing), in the `relationships` primary key, on the same task-local contextvar + gRPC-header rail. Prod never changes it; CI bumps it per run. Keep `make reset` as the nuke; reject overloading `principal_id` for test isolation."
status: resolved
severity: medium
area: agents/memory
created: 2026-05-30
closed: 2026-05-31
refs:
  - docs/memory-scope-axes.md
  - docs/rfcs/0031-per-session-namespacing-channels.md
  - docs/rfcs/0039-user-accounts-authentication.md
  - agents/principal_id.py
  - agents/memory/_principal_filter.py
  - internal/channels/session_binding.go
---

## Summary

The [scope-axes reframing](../memory-scope-axes.md) redefines `session` as room-continuity (`(agent, channel)`, accumulating). Once session is continuity, it cannot also be the per-run isolation namespace RFC 0031 originally built it to be — so **F-3 ("a rerun must not inherit the prior run's state") needs its own axis**. This issue tracks adding it: a dedicated `epoch_id` dimension.

## Context

The decisive constraint (full analysis in [Memory Scope Axes §Epoch](../memory-scope-axes.md#epoch--the-testrun-isolation-axis)): **F-3's bleed spans two kinds of memory** — room-scoped (episodes) *and* person-scoped (relationship, person-facts). Any isolation mechanism must reset both.

- "Fresh channel name per test run" resets only the room-scoped episodes; relationship and person-facts are keyed on the *participant* ([ISSUE-0084](ISSUE-0084-fact-scope-by-subject-not-uniform-session.md)) and survive a room rename, so a rerun reusing `--user alice` still surfaces old trust and opinions — the F-3 symptom.
- Overloading `principal_id` (test = synthetic tenant) was **rejected**: it re-commits the "one identifier, many meanings" mistake the reframing exists to fix, and in real multi-tenant prod the principal is already meaningful.
- `make reset` is **kept** as the documented nuclear option, but it cannot express isolated-but-coexisting worlds (it wipes the volume), so it loses the CI-continuity and cross-room-recall test scenarios RFC 0031 §Motivation wanted.

So isolation is a logical-key axis, structurally identical to the `principal_id` axis already shipped by [ISSUE-0081](ISSUE-0081-session-id-process-global-not-task-local.md) PR 3.

## Impact

- Today, F-3 isolation works via per-run `PERSATRIX_SESSION_ID` rotation, so this is **not broken right now** — it becomes load-bearing once [ISSUE-0083](ISSUE-0083-session-binding-sender-axis-fragments-multiparty-rooms.md) lands and makes the session unit stable + accumulating. Sequenced after 0083.
- Without epoch, the reframed model has no clean test-isolation primitive other than `make reset`.

## Proposed fix / investigation path

1. **Migration** — add `epoch_id TEXT NOT NULL DEFAULT 'live'` across the persona-memory tiers (`episodes`, `relationships`, `facts`, `notes`, `interactions`) and the Go channel store, mirroring the `principal_id` migration (v11). Put `epoch_id` in the `relationships` **primary key** (not just a column) — same reasoning as `principal_id` there: an `ON CONFLICT DO UPDATE` would otherwise bleed trust across epochs while a residual filter masks it.
2. **Recall + write filter** — unconditional `AND epoch_id = ?` on every recall and per-request write path. **No carve-out** and **no `'*'` sentinel** — a fresh epoch must see nothing (contrast the session `legacy` carve-out, which exists *for* continuity). Mirror [`agents/memory/_principal_filter.py`](../../agents/memory/_principal_filter.py) as `_epoch_filter.py`.
3. **Rail** — a sibling `contextvars.ContextVar` (cf. [`agents/principal_id.py`](../../agents/principal_id.py)) + a `persatrix-epoch` gRPC header; the orchestrator resolves it at boot from `PERSATRIX_EPOCH` (default `live`) and emits it per request. Prod never changes it; CI bumps it per job.
4. **Operator surface (open)** — whether `persatrix epoch new` / `--epoch` is part of RFC 0031 Phase 3 or a later phase is an open sequencing decision (flagged in the [Phase 3 plan amendment](../rfcs/0031-phase3-pr-plan.md#amendment--scope-axes-reframing)). `make reset` deprecation breadcrumb (Phase 4) should point at epoch, not `session new`.
5. **`make reset`** — keep as the nuke; update its operator-guide framing to "epoch is the everyday logical-branch tool; `make reset` wipes all epochs across all sessions."
6. **Maintenance sweeps caveat** — the agent-global eviction/retention/janitor sweeps already skip the `principal_id` filter; `epoch_id` inherits the same gap and the same deferral (capacity-policy decision, not a per-request read path).

> Maintainer owns sequencing (later patch / new RFC 0031 phase / successor RFC). This is the structural half of the reframing; [ISSUE-0083](ISSUE-0083-session-binding-sender-axis-fragments-multiparty-rooms.md) is the prerequisite that makes it necessary.

## Resolution

**Resolved in v0.3.5** as RFC 0031 [Phase 3b](../v0.3.5-plan.md#phase-3b--rfc-0031-epoch-axis-issue-0085), executing the [epoch PR plan](../rfcs/0031-epoch-pr-plan.md) (PRs 1–6):

1. **Migration** ✅ — migration v12 added `epoch_id TEXT NOT NULL DEFAULT 'live'` across the five persona-memory tiers + channel-store v6, with `epoch_id` in the `relationships` primary key ([#474](https://github.com/mkhomutov/Persatrix/pull/474)).
2. **Recall + write filter** ✅ — `agents/memory/_epoch_filter.py`: unconditional `AND epoch_id = ?`, no carve-out, no `'*'` sentinel; wired across every recall + per-request write path ([#475](https://github.com/mkhomutov/Persatrix/pull/475)).
3. **Rail** ✅ — `agents/epoch_id.py` ContextVar + `persatrix-epoch` gRPC header; orchestrator boot-resolves `PERSATRIX_EPOCH` (default `live`) and emits it per request; persona-runtime ingress lifts it into an `epoch_scope` ([#472](https://github.com/mkhomutov/Persatrix/pull/472), [#476](https://github.com/mkhomutov/Persatrix/pull/476)).
4. **Operator surface** ✅ — resolved to **bare flag + env, no registry verbs** (epoch has no continuity-room lifecycle to manage): `--epoch <id>` on the dispatch-bearing verbs (precedence above `PERSATRIX_EPOCH`), documented in [`docs/guides/epochs.md`](../guides/epochs.md) ([#477](https://github.com/mkhomutov/Persatrix/pull/477)).
5. **`make reset` framing** ✅ — reframed at epoch as the everyday run-isolation tool across the channels / persona-agents / sessions guides; `make reset` is the whole-stack nuke (all epochs across all sessions).
6. **Maintenance sweeps caveat** — `epoch_id` inherits the `principal_id` deferral (the agent-global eviction/retention/janitor sweeps skip the filter): recorded, not closed.

The structural F-3 fix is gated end-to-end by `tests/integration/test_epoch_run_isolation.py`: a rerun reusing `--user alice` under a fresh `PERSATRIX_EPOCH` — same room, same user — inherits none of the prior run's episodes, relationship trust, or person-facts.
