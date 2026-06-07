# Interaction Summary Surface — PR Implementation Plan (v0.3.8 scope: a readable synthesized outcome)

**RFC**: [0020-interaction-lifecycle.md](0020-interaction-lifecycle.md) (the one-per-interaction summary this surface reads; §C lifecycle states, §D storage model)
**Created**: 2026-06-07
**Branch prefix**: `feature/v038-interaction-summary-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.8-plan.md](../v0.3.8-plan.md) (Workstream 1c — the readable synthesized outcome)

---

## Overview

A closed brainstorm must hand back a *result*, not just stop. This plan surfaces the [RFC 0020](0020-interaction-lifecycle.md) one-per-interaction summary at end-of-interaction — in the **web console** and via the **CLI** — so "converge and terminate" produces something a human can read. This is the "produce a real result" half of the v0.3.8 headline; the [governance layers](0030-governance-layers-pr-plan.md) are the "converge and terminate" half.

### The generation-vs-surface question — resolved

The [master plan §Open-question status](../v0.3.8-plan.md#open-question-status) flagged this as the one decision to resolve **before** implementation: *is the interaction summary already generated at close, or does v0.3.8 add the synthesis step?*

**Resolution: the summary is already generated at close — v0.3.8 surfaces it, with one gap to close.** Per [RFC 0020 §C](0020-interaction-lifecycle.md#c-interaction-lifecycle-states) and [§D](0020-interaction-lifecycle.md#d-storage-model), a closed multi-turn interaction (`turn_count > 1`) already persists an LLM-generated summary (`context_management.summarization.model`) to the `episodes` table on close, transitioning the row to `closed`/`summarized`; single-turn interactions degenerate to the per-event summary text; a summary timeout writes the `"[interaction summary unavailable]"` sentinel via the janitor. The close path lives in `_persist_closed_interaction` ([agents/memory/interactions.py](../../agents/memory/interactions.py)). So **generation is not in scope** — surfacing is.

**The one gap (this plan owns it):** v0.3.8 introduces new close *triggers* — the Layer 4 end-vote and the Layer 1 cost-ceiling exhaustion ([governance-layers PR plan](0030-governance-layers-pr-plan.md)). Acceptance requires that **every** close `trigger` (votes / cost / idle) routes through the summarising close path so a converged brainstorm **always** yields a readable summary. The end-vote close routes through RFC 0020's structural close (already summarises); the **cost-ceiling termination must be verified to actually close + summarise the interaction**, not merely stop fanout. PR 1 owns that verification + fix.

### What this plan delivers

1. **Every close trigger summarises** (PR 1) — the back-end guarantee that votes/cost/idle each leave a `closed`/`summarized` episode row.
2. **Web console interaction-summary surface** (PR 2) — the converged interaction's summary rendered in the conversation view when an interaction closes.
3. **CLI interaction-summary surface** (PR 3) — read a closed interaction's summary from the terminal.
4. **Manual test + docs + status** (PR 4) — the readable-outcome acceptance record and operator docs.

**Explicitly deferred**: changing *how* summaries are generated (the RFC 0020 summariser is unchanged); the 2000-char summary cap and the failure sentinel are inherited as-is. Per-interaction *streaming* of a live summary is out of scope — the surface renders the persisted summary at close.

**Prerequisites (satisfied)**: [RFC 0020](0020-interaction-lifecycle.md) Interaction lifecycle + summarisation (shipped — Phase 2 onward generates the summary at close). This plan composes with the [governance-layers PR plan](0030-governance-layers-pr-plan.md) (its Layer 4 / Layer 1 closes are the new triggers) but only **soft-depends** on it: PR 1 can verify the idle-close path independently; the votes/cost triggers are exercised once the governance layers land (combined MT, master-plan Phase 3).

### Decisions locked at plan-authoring time

- **SS1 — surface, don't regenerate.** v0.3.8 reads the persisted RFC 0020 summary; it does not add or alter the summariser. **All PRs.**
- **SS2 — every close trigger carries a summary.** votes / cost / idle each route through `_persist_closed_interaction` so the `episodes` row is `closed`/`summarized` (or carries the `"[interaction summary unavailable]"` sentinel on summary failure — the surface renders that sentinel rather than nothing). **PR 1.**
- **SS3 — render the failure sentinel honestly.** If the summary is the `"[interaction summary unavailable]"` marker, the surface shows that the interaction closed but the summary failed — never a blank or a fabricated synthesis. **PR 2 + PR 3.**

---

## Sequencing

**Merge order: PR 1 → PR 2 → PR 3 → PR 4.** (PR 2 and PR 3 both depend on PR 1's read path and are independent of each other; sequence PR 2 before PR 3 so the web surface anchors the shared read API.)

- **PR 1** is the back-end guarantee + the read path: confirm/wire every close trigger to summarise, and expose the closed-interaction summary through a read API (REST + any existing episode-query path) the surfaces consume.
- **PR 2** renders the summary in the web console conversation view.
- **PR 3** adds the CLI surface.
- **PR 4** is the manual test + docs + status closeout.

Every PR is **TDD-first**: the close-trigger→summarised-row assertion for PR 1, the render test for PR 2, the CLI-output test for PR 3.

---

## Dependency Graph

```
PR 1 (every close trigger → _persist_closed_interaction summarises; read API exposes the closed-interaction summary)
  ↓                          ↓
PR 2 (web console renders     PR 3 (CLI reads the closed-interaction
      the summary at close)         summary)
  ↓                          ↓
PR 4 (MT readable-outcome + docs + status + CHANGELOG)
```

---

## PR Sequence

### PR 1: `feature/v038-interaction-summary-close-and-read` — Every close trigger summarises + the read path

**Depends on**: RFC 0020 summarisation (shipped); soft-composes with the governance-layers PR plan (its close triggers).
**Purpose**: Guarantee that votes/cost/idle each leave a `closed`/`summarized` episode row, and expose that summary through a read API the surfaces consume.

| File | Change |
|------|--------|
| [`agents/memory/interactions.py`](../../agents/memory/interactions.py) | Verify the close path: the Layer 4 end-vote structural close already routes through `_persist_closed_interaction` (summarises). **Fix the gap**: a Layer 1 cost-ceiling termination must *close + summarise* the interaction (route it through the same close path with a `cost` close reason), not merely stop fanout — otherwise a cost-bounded brainstorm stops with no readable result (SS2). Add a `cost` close reason + the matching `agent.interactions.closed.by_cost` counter alongside the existing idle/structural reasons. |
| read API (orchestrator REST + `agents/…`) | Expose a closed-interaction summary read: given an `interaction_id` (or the latest closed interaction for a scope), return `{interaction_id, scope, closed_at, close_reason/trigger, turn_count, participants, summary}` from the `episodes` row (filter `summary IS NOT NULL AND summary != ''` per [§D](0020-interaction-lifecycle.md#d-storage-model), but **include** the `"[interaction summary unavailable]"` sentinel rows so a failed summary is still surfaced honestly — SS3). Reuse the existing episode/recall query plumbing where possible rather than a parallel store reader. |
| tests | **(TDD — write first.)** An idle close, a structural (end-vote) close, and a cost-ceiling close each leave a `closed`/`summarized` row reachable by the read API; the read API returns the summary + the close `trigger`; a summary-failure row returns the sentinel (not an empty body); `turn_count=1` returns the degenerate per-event summary. |

**Acceptance**: every close trigger (idle / end-vote / cost) yields a readable `closed`/`summarized` episode row; the read API returns the summary + trigger for an interaction; the failure sentinel is surfaced, never a blank; the `by_cost` close counter increments on cost termination.

---

### PR 2: `feature/v038-interaction-summary-web` — Web console interaction-summary surface

**Depends on**: PR 1.
**Purpose**: When an interaction closes, render its summary in the web console conversation view so a human sees the synthesised outcome.

| File | Change |
|------|--------|
| `web/src/panels/ConversationFeed.svelte` / [`ChannelTimeline.svelte`](../../web/src/panels/ChannelTimeline.svelte) | Render a distinct "interaction closed" affordance in the conversation view carrying the summary + the close `trigger` (votes / cost / idle) when the read API reports a newly closed interaction for the active scope. Honestly render the `"[interaction summary unavailable]"` sentinel as a "summary unavailable" state (SS3), not a blank. |
| web data layer (`web/src/…`) | Fetch the closed-interaction summary from PR 1's read API; subscribe/refresh on interaction close. Keep it additive — an open interaction's live turns render exactly as today; the summary affordance appears only at close. |
| `web/src/panels/*.test.js` (new/extended) | **(TDD — write first.)** The summary affordance renders the summary + trigger when the read API reports a close; the sentinel renders as "unavailable"; an open interaction shows no summary affordance (no regression to the live feed). |

**Acceptance**: a closed interaction shows its summary + close trigger in the web console conversation view; the failure sentinel renders honestly; the open-interaction live feed is unchanged; the web test lane is green.

---

### PR 3: `feature/v038-interaction-summary-cli` — CLI interaction-summary surface

**Depends on**: PR 1.
**Purpose**: Read a closed interaction's summary from the terminal, so the convergence outcome is visible without the web console.

| File | Change |
|------|--------|
| [`cmd/orchestrator/…`](../../cmd/orchestrator) (or the existing CLI surface) | Add a command/subcommand to print a scope's latest closed-interaction summary (or by `interaction_id`): the summary, close `trigger`, `turn_count`, participants. Consume PR 1's read API. Render the sentinel as an explicit "summary unavailable" line (SS3). |
| tests | **(TDD — write first.)** The command prints the summary + trigger for a closed interaction; prints the "unavailable" line for a failure-sentinel row; exits non-zero / prints a clear "no closed interaction" message when the scope has none. |

**Acceptance**: the CLI prints a closed interaction's summary + trigger; the sentinel is shown honestly; an absent summary is a clear message, not a crash.

---

### PR 4: `feature/v038-interaction-summary-closeout` — Manual test + docs + status

**Depends on**: PR 2, PR 3.
**Purpose**: The readable-outcome acceptance record and operator docs.

| File | Change |
|------|--------|
| `docs/manual-tests/MT-INTERACTION-SUMMARY-001.md` (new) | **New.** Drive a multi-turn interaction to close on each trigger (idle, end-vote, cost); assert the web console and the CLI each show a readable summary + the correct close trigger; assert a forced summary failure renders the sentinel honestly. (The combined convergence story is `MT-CONVERSATION-CONVERGENCE-001`, owned by the release-prep plan; this MT isolates the summary surface.) |
| [`docs/guides/channels.md`](../../docs/guides/channels.md) (+ web console guide if present) | Document the interaction-summary surface: where it appears (web conversation view + CLI), that it renders the RFC 0020 per-interaction summary at close, and the close triggers (votes / cost / idle). |
| [`0020-interaction-lifecycle.md`](0020-interaction-lifecycle.md) + ROADMAP | Status hygiene: record the summary surface landing in v0.3.8 (the summary was generated since RFC 0020; v0.3.8 surfaces it). RFC Master Index note refresh; `make rfcs` regenerates INDEX. |
| CHANGELOG | `[0.3.8]` Upgrade Note: a closed interaction now surfaces its summary in the web console and via the CLI — additive; the summariser is unchanged. |

**Acceptance**: `MT-INTERACTION-SUMMARY-001` recorded; the web console + CLI surfaces shown live; docs name the surface and the triggers; RFC 0020 status note refreshed; CHANGELOG Upgrade Note present.

---

## Test Strategy (summary)

- **Unit (PR 1)**: every close trigger (idle / end-vote / cost) leaves a `closed`/`summarized` row; read API returns summary + trigger; failure sentinel surfaced; `turn_count=1` degenerate summary; `by_cost` counter.
- **Unit (PR 2)**: web summary affordance renders summary + trigger; sentinel → "unavailable"; open interaction → no affordance.
- **Unit (PR 3)**: CLI prints summary + trigger; sentinel line; clear "none" message.
- **Manual (PR 4)**: `MT-INTERACTION-SUMMARY-001` — readable summary on each close trigger, web + CLI, honest failure rendering.
- **Combined (master-plan Phase 3)**: `MT-CONVERSATION-CONVERGENCE-001` — the converged brainstorm ends and hands back the summary.
- **Regression**: every PR keeps the RFC 0020 interaction + web conversation suites green; the summariser is untouched.

---

## Status & ROADMAP hygiene

Per [master-plan §ROADMAP hygiene](../v0.3.8-plan.md#roadmap-hygiene):

- **PR 1 open** → no RFC status change (read path + close-trigger wiring; companion PR plans excluded from `INDEX.md`).
- **PR 2 / PR 3 merge** → the surface lands; `Last updated` refresh.
- **PR 4 merges** → CHANGELOG `[0.3.8]` Upgrade Note seeded; RFC 0020 note records the summary surface landing in v0.3.8.
- **v0.3.8 tag** → `MT-INTERACTION-SUMMARY-001` + the combined `MT-CONVERSATION-CONVERGENCE-001` re-run live on HEAD as release gates (master-plan Phase 3).

---

## Related documentation

- [RFC 0020 — Interaction Lifecycle](0020-interaction-lifecycle.md) — §C lifecycle states, §D storage model (the `(closed_at, summary)` encoding the read path filters on), §Security (the 2000-char cap + the failure sentinel this surface renders). The summary this plan surfaces.
- [Governance-layers PR plan](0030-governance-layers-pr-plan.md) — supplies the new close triggers (Layer 4 end-vote, Layer 1 cost ceiling) every one of which must carry a summary.
- [Tier B PR plan](0030-amendment-relevance-gated-response-tierb-pr-plan.md) — the no-pile-on sibling; the converged set of turns is what the summary synthesises.
- [v0.3.8 plan](../v0.3.8-plan.md) — the release this lands in; Workstream 1c; the §Open-question status entry this plan resolves.
- [`agents/memory/interactions.py`](../../agents/memory/interactions.py), `web/src/panels/ConversationFeed.svelte`, [`cmd/orchestrator`](../../cmd/orchestrator) — the code this plan touches.
