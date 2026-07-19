# RFC 0049 Amendment — L1 Cross-Room Availability (Gated Raw Recall)

**Type**: amendment to [RFC 0049](0049-memory-consolidation-gradient.md) Non-Goal #1, §B (gradient table, L1 row), §C (corollary "L1 → room"), §D (reconciliation table, row 1); recall-semantics touchpoint on [RFC 0031](0031-per-session-namespacing-channels.md) §D
**Status**: 📋 Proposed — reverses ratified Non-Goal #1 per the maintainer's 2026-07-15 v0.3.12 scope lock ("everything crosses channels, gated by classification"), applied 2026-07-19
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

## Consequences

- **RFC 0049 text**: Non-Goal #1, the §B L1 row, the §C corollary, the §D
  reconciliation row 1, and the Summary law are annotated with dated
  pointers to this amendment (applied alongside it).
- **[memory-scope-axes.md](../memory-scope-axes.md)**: *no decision
  superseded* — its grounding principle ("continuity is the default,
  isolation the named exception") is what this amendment extends to L1;
  room-continuity survives as the ranking default.
- **EVAL-RECALL-001** ([RFC 0044](0044-eval-set-golden-traces.md)): must be
  re-specified before recording — as drafted it asserts the room wall this
  amendment removes. Its real invariant (epoch/principal no-leak +
  classification gating) is a follow-up re-spec, tracked in the v0.3.12
  decision list.
- **Open intersection**: multi-source protection stamping (a pump-produced
  entry consolidated from many rooms) remains an open design item of the
  v0.3.12 review — this amendment inherits, and does not resolve, it.

## Sequencing & dependencies

RFC 0037 Phase 1 (§D gate + §F filter) **must land first**; the L1
widening lands behind it in the same release, with the room-first ranking
change and the gated cross-room recall mode as separate, testable PR
slices in the v0.3.12 plan. Shadow evaluation against RFC 0044 golden
traces (the 0049 Phase-1→2 measurement-gate pattern) applies to the L1
widening the same way it applies to L2.
