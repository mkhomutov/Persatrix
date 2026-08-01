---
id: ISSUE-0118
summary: "Agent-initiated memory-tool recalls bypass the per-request epoch (and session) scopes: the action executor runs tool calls in a different asyncio task than `on_event`, so the ContextVar scopes `request_scope_from_metadata` binds for the handler never reach the tool execution — `resolve_active_epoch`/`resolve_session_id` fall back to the construction snapshot (boot epoch `live` / legacy session), and a tool recall returns rows the strict-equality run-isolation filter would exclude. Found live at the v0.3.12 MT-MEMORY-CROSSROOM-001 fresh-epoch leg: with the injection path correctly returning zero admissions under `--epoch mt-crossroom-fresh`, the model reached for its recall tool and surfaced the live-epoch fact anyway (the F-3 leak class, via the tool side door). The RFC 0037 classification axis does NOT share the hole — the tripwire/§D work threads acting classification through event.metadata + DispatchContext precisely because of this task hop; epoch and session need the same treatment."
status: open
severity: medium
area: agents
created: 2026-07-30
refs:
  - docs/manual-tests/MT-MEMORY-CROSSROOM-001.md
  - docs/manual-tests/v0.3.12-execution-report.md
  - agents/tools/memory_tools.py
  - agents/memory/_epoch_filter.py
  - agents/persona_runtime/__init__.py
  - agents/confidentiality_tripwire.py
---

## Summary

The per-request epoch override (ISSUE-0085 PR 5 `--epoch`) and per-request
session binding are enforced only on the **injection** path (`on_event` binds
the scopes via `request_scope_from_metadata`, and `_inject_memory_context`'s
queries carry the strict-equality clauses). An **agent-initiated tool recall**
executes in the action executor's task — a different asyncio task than the
`on_event` handler — so the task-local ContextVars never cross, the resolvers
fall back to their construction snapshots (epoch `live`, legacy session), and
the tool reads rows that the run-isolation filter would have excluded.

## Live evidence (v0.3.12 release-prep MT execution, 2026-07-30)

- Fresh-epoch asks (`--epoch` on both the channel-send and chat paths, plus a
  clean probe channel with an empty transcript) still surfaced the live-epoch
  fact ("Atlas ships Friday").
- With `PERSATRIX_MEMORY_PROVENANCE=1`: the leaked turn admitted **zero** items
  on every tier (the injection-path wall held) and ran a two-call tool round;
  the live-epoch control turn admitted 7 facts-tier items normally.
- Direct gRPC probes at the agent with a `persatrix-epoch` header (group and
  DM shapes) both held the wall when the model answered without a tool round —
  and Jaeger shows the orchestrator emitting the override (`epoch.id` on the
  dispatch span), so every wire hop is correct; only the executor-task hop
  drops the scopes.

## Why classification does not share the hole

RFC 0037 PR 7 (#788) hit this exact seam and documented it: "the queued
EventLoop path runs the turn in a DIFFERENT TASK than the executor call, so a
contextvar can't cross" — which is why the acting classification and the
tripwire watch ride `event.metadata` and are lifted structurally by
`DispatchContext.for_event`. The note tools' §C stamp and §D read predicate
were verified live in the same MT run (tool-written notes stamped
`restricted`; the internal-room ask stayed non-disclosing).

## Fix direction

Thread the epoch and session scopes across the executor hop the same way:
stamp them on the event (they already ride `event.metadata` from the
servicer), lift them in `DispatchContext.for_event`, and re-enter the scopes
(or pass resolved ids explicitly) around tool execution. Alternatively, bind
the request scopes around the executor's action-processing task at spawn.

## Impact / scope

- Pre-existing since the tool surface and the epoch/session axes coexist —
  **not** a v0.3.12 regression: nothing in RFC 0037/0049/0039 touched tool
  recall scoping, and the v0.3.12 cross-room widening honors the wall on the
  injection path (verified live, plus the CI strict-equality suites).
- Exposure requires the model electing a tool recall on a turn whose
  injection came back empty — exactly the fresh-epoch shape, so `--epoch`
  run isolation (and `--session` room pinning) cannot be relied on for
  personas with memory tools until fixed.

## Notes

> 2026-07-31 — the maintainer call flagged in the
> [#796 execution report](../manual-tests/v0.3.12-execution-report.md)
> resolved at
> [release-prep PR 2](../v0.3.12-release-prep-plan.md#pr-2--docs--release-checklist):
> **does not gate the v0.3.12 tag.** Pre-existing (not a regression of
> either workstream), the injection-path wall the release ships is proven
> live three independent ways, and the classification axis is unaffected.
> Rides the release as a Known Gap — the release notes must carry the
> caveat that `--epoch`/`--session` isolation cannot be relied on for
> personas with memory tools until fixed. Fix slated **v0.3.13** (thread
> the scopes across the executor hop the classification way:
> `event.metadata` → `DispatchContext`).
