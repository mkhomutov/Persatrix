# RFC 0049 Amendment — L1 Cross-Room Availability (Gated Raw Recall)

**Type**: amendment to [RFC 0049](0049-memory-consolidation-gradient.md) Non-Goal #1, §B (gradient table, L1 row), §C (corollary "L1 → room"), §D (reconciliation table, row 1); recall-semantics touchpoint on [RFC 0031](0031-per-session-namespacing-channels.md) §D
**Status**: ⚠️ Implemented in SHADOW — v0.3.12, [RFC 0049](0049-memory-consolidation-gradient.md) Phase 1 PR 3 ([0049-pr-plan.md](0049-pr-plan.md)); the live-prompt promotion is PR 4's measurement-gated flip. Reverses ratified Non-Goal #1 per the maintainer's 2026-07-15 v0.3.12 scope lock ("everything crosses channels, gated by classification"), applied 2026-07-19; implemented 2026-07-27
**Author**: Maksim Khomutov
**Date**: 2026-07-19
**Target**: v0.3.12 — **behind the RFC 0037 keystone** (nothing widens before the §D gate + §F filter land; the [RFC 0049 §E](0049-memory-consolidation-gradient.md#e-confidentiality-is-the-keystone-not-an-add-on) hard sequencing rule extends to L1)
**Authoritative model**: [RFC 0049 — Memory Consolidation Gradient & Scope Reconciliation](0049-memory-consolidation-gradient.md)
**Supersedes**: RFC 0049 Non-Goal #1 ("Unifying episodic recall", ratified 2026-06-06) and the §C corollary "L1 → room. (Unchanged.)"

---

## Context

RFC 0049 as ratified kept raw episodic memory (L1) **room-walled**: only
consolidated tiers (L2+) cross rooms, and cross-room *episodic* lookup
remained a CLI/debug-only path (`sessions="*"`,
[`_session_filter.py`](../../agents/memory/_session_filter.py)). Two reasons
carried that wall: **leakage** (solved by the RFC 0037 egress gate) and
**continuity/ranking quality** — same-room episodes are what the
[dementia-test](../memory-quality-roadmap.md#quality-bar--the-dementia-test)
continuity bar ranks on, and flooding recall with other rooms' episodes
would degrade it.

The v0.3.12 scope lock chooses the fuller model: a persona's raw
experience is *available* to it everywhere, exactly as its consolidated
knowledge is — made safe by classification, not by walls.

## The change

**L1 episodic recall becomes cross-room *available*, classification-gated
— while staying room-*ranked* by default.**

1. **The wall becomes a ranking cue.** The RFC 0031 §D session filter, as
   the L1 *default*, changes from hard exclusion to **room-first ranking**:
   same-room episodes are boosted (the [RFC 0049 Risks](0049-memory-consolidation-gradient.md#risks)
   mitigation, now the mechanism); other-room episodes are admissible
   candidates, demoted by the ranking cue. The dementia-test continuity
   bar is a *ranking* property and is preserved by the boost, not by the
   wall.
2. **Every cross-room candidate passes the RFC 0037 gate.** An episode
   whose protection level outranks the acting channel's classification is
   withheld (or projected, per 0037 §E) — identical to the L2 rule. No
   cross-room episode is admissible before the 0037 §D gate ships.
3. **The raw-*verbatim* half is already half-shipped.** [RFC 0036](0036-persona-message-recall.md)
   channel-store recall is *already* cross-channel (membership-scoped,
   server-side SQL); it gains only the 0037 §F acting-channel
   classification clause. **Only the episodic memory tier needs new
   mechanism** — the gated cross-room recall mode this amendment names.
4. **What stays absolute.** The `epoch` (run/test isolation) and
   `principal` (tenant) axes remain **hard walls**, untouched. This
   amendment widens the *room* axis only. Per-persona ownership is
   unchanged: each persona recalls only its **own** memory of other rooms.

## Implementation (v0.3.12 PR 3 — the SHADOW slice)

The widening ships **shadow-first** (the [measurement gate](#sequencing--dependencies)): each channel-anchored turn computes what the room-first-RANKED recall *would* have injected and records it, while the live prompt keeps the room-walled recall byte-for-byte.

- **The ranking mechanism** — new [`agents/memory/episodic_room_ranked.py`](../../agents/memory/episodic_room_ranked.py) (`recall_room_ranked`): the RFC 0031 §D session filter, applied as a **score multiplier** instead of a WHERE wall. The boost set resolves exactly like the live wall (`_resolve_session_list(None, …)` — active room + `legacy` carve-out, call-time `session_scope` wins), so wall and boost can never drift on what "same room" means; `session_boost_expr` (`ROOM_BOOST_FACTOR = 2.0`, [`_session_filter.py`](../../agents/memory/_session_filter.py)) multiplies the composite score on all three query branches (FTS5 / LIKE / recency). Same-room first at equal relevance; a cross-room row must more than double a same-room row's composite score to outrank it. The read is **side-effect-free** — no `access_count` bump — because the live composite score reads `access_count`: a reinforcing shadow would perturb live ranking on later turns and shift the landed RFC 0044 goldens off their cassettes. Whether the PR 4 live flip reinforces is decided where the promotion lands. Boost-factor calibration against the RFC 0017 budget is a PR 4 measurement concern.
- **The shadow pass** — new [`agents/persona_runtime/episodes_shadow.py`](../../agents/persona_runtime/episodes_shadow.py), invoked from `_inject_memory_context` beside the live episodic recall with the same query / `EPISODIC_RECALL_LIMIT` / `min_score`, so the shadow-vs-live comparison is like-for-like by construction. The **cross-room delta** — ranked widened rows whose `id` the live recall did not return — passes through a dedicated RFC 0037 §D `TurnInjectionGate` at the turn's acting classification. Only channel-anchored turns (`CHANNEL_ACTING_EVENT_TYPES`) run the pass: the tick-shaped class floors to rule-(b) `public` and is the RFC 0017 §F cheap-idle path, so idle ticks keep costing zero DB round-trips.
- **The trace** — one structured INFO record per turn with a non-empty delta, on the `agents.persona_runtime.episodes_shadow` logger: `tier: "episodic"` (the merged-stream discriminator — the facts trace now carries `tier: "facts"` symmetrically), `agent_id`, `acting`, per-candidate `{episode_id, rank, protection_level, session_id, source_channel_id}` — `rank` is the row's position in the WIDENED result (live rows included): "this row would have been the prompt's #N episodic line", the displacement signal the PR 4 measurement compares against the live top-N — and the split withhold counts (`withheld` above-rank vs `unknown_label` rule-(c)), the same two fields the PR 4 consumer reads off L2 traces. Quiet turns — empty delta, tick-shaped events, `off` mode — emit nothing. The pass shares `_inject_memory_context`'s never-fail contract.
- **The knob** — `memory.episodic.cross_room: off | shadow` (schema-gated enum, default `shadow`; resolved at agent construction; `"live"` rejected at both the schema and the resolver until PR 4), the exact twin of `memory.facts.cross_room`.
- **Harness recording** — `capture_shadow_traces` now listens on **both** shadow loggers into one chronologically-merged, `tier`-keyed `EvalRun.shadow_traces` stream → the `shadow_traces` report-artifact key. Landed goldens replay byte-identically (the pass shifts no request hash and reinforces nothing).

## Security considerations

- **Gate before trace.** Every cross-room candidate passes the §D gate *before* it is recorded — a `restricted`-stamped episode on a turn acting below `restricted` appears only as a withheld count (change item 2, pinned by test).
- **Log-egress bound.** The trace never carries the episode **summary** (or context/outcome/tags) — the process log is its own egress surface. The PR 4 measurement joins `episode_id` back against the store when it needs content.
- **The widened-read F-3 pin.** RFC 0031 §Security pins the persona-runtime *prompt-context* path never widening past the room. `episodic_room_ranked.py` + `episodes_shadow.py` are the documented L1 carve-out (the `facts_shadow` precedent): the widened read feeds nothing to WorkingMemory, the RFC 0017 budget, the §G manifest, or any reinforcement — its sole output is the log record. Pinned end-to-end (`test_episodes_shadow.py::TestShadowNeverEntersPrompt`); the live prompt path still calls `recall(sessions=None)` until the PR 4 flip. The CLI/debug `sessions="*"` path is untouched (change item 3 — it was never the mechanism).
- **Absolute walls unchanged.** `epoch` and `principal` remain strict-equality SQL clauses on every branch of the widened read (change item 4, pinned by test).

## Consequences

- **RFC 0049 text**: Non-Goal #1, the §B L1 row, the §C corollary, the §D
  reconciliation row 1, and the Summary law are annotated with dated
  pointers to this amendment (applied alongside it).
- **[memory-scope-axes.md](../memory-scope-axes.md)**: *no decision
  superseded* — its grounding principle ("continuity is the default,
  isolation the named exception") is what this amendment extends to L1;
  room-continuity survives as the ranking default.
- **EVAL-RECALL-001** ([RFC 0044](0044-eval-set-golden-traces.md)):
  ✅ re-specified 2026-07-19 (decision item 7) — re-anchored to the
  absolute epoch/principal walls; the room axis (room-first ranking +
  classification gating) gets its own RFC 0037 integration eval.
- **Multi-source stamping intersection**: ✅ resolved 2026-07-19
  (decision item 3) — [RFC 0037 §C "Synthesized (multi-source)
  entries"](0037-memory-confidentiality-channel-classification.md#c-memory-provenance-and-protection-level)
  owns the rule; this amendment inherits it.

## Sequencing & dependencies

RFC 0037 Phase 1 (§D gate + §F filter) **must land first — SATISFIED**
([RFC 0037 PR 5](0037-pr-plan.md), #778, 2026-07-26); the L1 widening
lands behind it in the same release ([0049 PR plan](0049-pr-plan.md)
PR 3, this implementation), with the room-first ranking change and the
gated cross-room recall mode as one shadow slice. Shadow evaluation
against RFC 0044 golden traces (the 0049 Phase-1→2 measurement-gate
pattern) applies to the L1 widening the same way it applies to L2 —
the PR 4 flip promotes both or documents a shadow-ship.
