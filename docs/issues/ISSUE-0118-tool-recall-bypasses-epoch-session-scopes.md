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
  - agents/tools/recall.py
  - agents/memory/_epoch_filter.py
  - agents/persona_runtime/__init__.py
  - agents/confidentiality_tripwire.py
  - agents/dispatch_context.py
  - internal/channels/recall.go
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

> 2026-08-02 — **v0.3.12 is released** with this as a documented Known Gap
> ([v0.3.12 — Memory that travels](https://github.com/mkhomutov/Persatrix/releases/tag/v0.3.12)).
> The caveat landed in **both** required places: the curated `[0.3.12]`
> Upgrade Notes (the `--epoch` / `--session` row) and the Known Gaps section
> of the published release body. Nothing about the shipped behaviour changed
> at the tag — this note only records that the maintainer's "does not gate"
> call was exercised and the disclosure obligation met. Fix remains slated
> **v0.3.13**.

> 2026-08-03 — **v0.3.13 plan opened**
> ([v0.3.13-plan.md](../v0.3.13-plan.md)): this fix is the release's first
> implementation PR (`feature/v0313-issue0118-tool-recall-scopes`) and gates
> the live MT arc. The fix shape above — thread the scopes the classification
> way, `event.metadata` → `DispatchContext`, re-entered around tool
> execution — is confirmed as the plan-opening scope lock. One live arc at
> release-prep verifies both this fix (Leg 4 with a tool round) and
> [ISSUE-0121](ISSUE-0121-crossroom-person-identity-legs-never-run-live.md)'s
> legs 1b/2b; the v0.3.12 `--epoch`/`--session` caveat retires in the
> v0.3.13 release notes.

> 2026-08-03 — **Fix PR opened** (`feature/v0313-issue0118-tool-recall-scopes`,
> v0.3.13 PR 1) with a **diagnosis refinement** from deterministic
> reproduction. Probing all three dispatch shapes (direct `on_event`,
> `dispatch()` → queued `EventLoop` with handle, `enqueue_inbound` →
> `on_inbound`) shows the **in-loop tool round** — `_execute_tools` inside
> `_on_event_inner` — already inherits the handler's ContextVar binding on
> every path: the multi-turn memory-tool round was *not* the ContextVar
> gap. Two real holes remain, and the PR closes both:
>
> 1. **The executor hop** (the summary's mechanism, relocated): everything
>    the `ActionExecutor` runs AFTER `on_event` returns — the end-vote
>    close discharge persisting the voter's interaction, the legacy
>    cascade's child dispatches — executes on the dispatching task, where
>    the handler's scopes never reach; memory seams there resolved
>    construction snapshots. Fixed the classification way (the fix
>    direction above): epoch/session lift structurally in
>    `DispatchContext.for_event` (via the same leaf readers the `on_event`
>    binders consume — the drift guard), re-entered around action
>    processing by `DispatchContext.request_scopes`; the chat surface's
>    post-reply execute threads them explicitly, and the legacy cascade's
>    child events carry the keys on the metadata rail.
> 2. **The reachable live-leak mechanism — the verbatim channel recall.**
>    `recall_channel_messages` hits the channel store, which is
>    single-epoch by the [ISSUE-0106](ISSUE-0106-recall-epoch-filter-decoupled-from-unpersisted-publish-epoch.md)
>    direction-(b) decision (physical isolation; the endpoint 400s epoch
>    overrides since #778) — so a fresh-epoch turn's recall read the live
>    world's verbatim history *regardless* of ContextVars. This fits the
>    live evidence exactly (injection admitted zero; the two-call tool
>    round surfaced the fact verbatim). The tool now declines with an
>    ordinary empty result when the bound per-request epoch differs from
>    the process's world epoch — a fresh epoch sees nothing, and does not
>    learn that withheld history exists. Session deliberately does not
>    gate the tool (room-continuity has a carve-out by design; membership
>    is the recall access rule).
>
> Deterministic CI pins all of it: the executor-hop re-entry (red without
> the threading), the tool-recall-on-the-executor-leg shape, the legacy
> cascade seeding, the chat-surface threading, the foreign-epoch wall, and
> — new — the in-loop injection/tool parity that held all along, so a
> future refactor moving tool execution off the handler task flips a test
> instead of shipping the leak silently. Closure (status → resolved) waits
> on the Phase 3 live proof: the MT fresh-epoch leg green **with a tool
> round evidenced in provenance** (the plan's acceptance criterion).

> 2026-08-03 — **PR #809 review follow-up: the other channel-store read
> paths, audited.** The foreign-epoch wall covers the *tool* door
> (`recall_channel_messages`); the two non-tool readers of the same
> single-epoch store were checked for the same
> reachable-under-a-foreign-epoch shape:
>
> 1. **Boot-time catch-up** (`agents/channel_catchup.py`) — **not a
>    door.** It replays last-N channel history through `on_event` at
>    persona-runtime boot only (the module's sole trigger), where no
>    per-request scope exists and the replay events carry no epoch
>    metadata: ingestion lands under the process's world epoch, and a
>    later foreign-epoch turn's recalls of what it ingested are already
>    walled by the memory tiers' strict epoch equality.
> 2. **The per-turn conversation window** (RFC 0034 —
>    `persona_runtime/conversation_seed.py` →
>    `agents/channel_history_fetcher.py`) — **reachable, and
>    deliberately not walled.** Every persona turn reconstructs the LLM
>    `messages` seed from the current channel's recent transcript with
>    no epoch check, so a foreign-epoch turn's prompt does include the
>    live window of the channel it was delivered into. The asymmetry
>    against the wall (which declines even same-channel recall under a
>    foreign epoch) is accepted because the two reads differ in reach:
>    the seed is bounded to the recent, delivery-visible transcript of
>    the one room the probe was sent into — content that room already
>    displays to its members — while the tool reaches arbitrary
>    historical content on demand (`query` / `limit` / any member
>    channel), which is what leaked live on 2026-07-30. The live
>    evidence is consistent with the boundary: the taught fact lived
>    outside the probe channel, injection admitted zero, and only the
>    tool round crossed. Recorded here so the boundary is explicit
>    rather than silent: if the epoch posture ever tightens to "a
>    fresh-epoch turn's prompt carries no live transcript at all", the
>    window seed (`_build_seed_messages`) is the seam to gate — a scope
>    decision for a future release, not v0.3.13 fix work, and it does
>    not gate the Phase 3 live proof (the MT's taught fact sits outside
>    the probe channel's window by construction).
