---
id: ISSUE-0085
summary: "Under the scope-axes reframing, `session` becomes the room-continuity unit `(agent, channel)` — it accumulates and never auto-resets — so it can no longer be the home of F-3 run/test isolation. F-3's bleed spans both room-scoped memory (episodes) and person-scoped memory (relationship, person-facts), so a fresh channel name cannot isolate a rerun (relationship/person-facts are keyed on the participant and survive a room rename). Add a dedicated orthogonal `epoch_id` axis (default `live`), modeled on the `principal_id` axis: strict equality, **no `legacy` carve-out** (a fresh epoch sees nothing), in the `relationships` primary key, on the same task-local contextvar + gRPC-header rail. Prod never changes it; CI bumps it per run. Keep `make reset` as the nuke; reject overloading `principal_id` for test isolation."
status: open
severity: medium
area: agents/memory
created: 2026-05-30
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
