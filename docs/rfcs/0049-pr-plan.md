# RFC 0049 — PR Implementation Plan (Phases 0–1 — v0.3.12 scope: the gated widenings + the capture path)

**RFC**: [0049-memory-consolidation-gradient.md](0049-memory-consolidation-gradient.md)
**Created**: 2026-07-25
**Branch prefix**: `feature/v0312-rfc0049-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.12-plan.md Phase 1](../v0.3.12-plan.md#phase-1--implement-rfc-0037-rfc-0049-p01-rfc-0039-p12)

---

## Overview

RFC 0049's v0.3.12 slice is **Phases 0–1**: Phase 0 is the RFC 0037 keystone (owned entirely by the [RFC 0037 PR plan](0037-pr-plan.md) — this plan does not restate it), and Phase 1 is the set of scope widenings + the capture path that turn the ratified gradient into behavior:

- **Capture** — the [RFC 0026 topic-predicate amendment](0026-amendment-topic-subject-predicates.md): the `topic.*` predicate namespace + extractor-prompt + recall-seeding, behind the allowlist blast-radius re-review. Without it, the L2 widening reads an empty tier.
- **L2 widening** — the [RFC 0031 fact-scope amendment](0031-amendment-fact-scope-by-consolidation-level.md): facts (topic knowledge included) recall **cross-room**, visibility = the RFC 0037 protection level inherited from source. Re-roots and closes [ISSUE-0084](../issues/ISSUE-0084-fact-scope-by-subject-not-uniform-session.md); supersedes memory-scope-axes.md decision 4 (already annotated).
- **L1 widening** — the [RFC 0049 L1 amendment](0049-amendment-l1-cross-room-availability.md): raw episodic recall becomes cross-room *available* behind the gate, with the RFC 0031 §D session filter converted from hard wall to **room-first ranking** (the dementia-test continuity bar is a ranking property, preserved by the boost).
- **Shadow → live promotion** — both widenings ship shadow-first and promote on the RFC 0044 golden-trace measurement gate.

**══ MERGE GATE (absolute):** no PR in this plan merges before [RFC 0037 PR 5](0037-pr-plan.md#pr-5--featurev0312-rfc0037-recall-filter-phase-1-step-5--issue-0106b--the-merge-gate-opens-here) (the §D gate + §F filter) is on `main`. Widening before the gate is a confidentiality regression by definition ([RFC 0049 §E](0049-memory-consolidation-gradient.md#e-confidentiality-is-the-keystone-not-an-add-on); extended to L1 by the [L1 amendment](0049-amendment-l1-cross-room-availability.md#sequencing--dependencies)). Branch names carry `post-gate` as a reviewer tripwire.

**What stays absolute:** `epoch` (run/test isolation) and `principal` (tenant) remain hard walls — this plan widens the *room* axis only, and each persona recalls only its **own** memory of other rooms.

This plan covers Phase 1 across **5 PRs**:

## Progress Overview

| PR | Step | Branch | Status | GitHub PR | Merged |
|----|------|--------|--------|-----------|--------|
| 1 | capture: `topic.*` predicates + extractor + recall seeding + blast-radius review | `feature/v0312-rfc0049-post-gate-topic-capture` | ✅ Merged | [#781](https://github.com/mkhomutov/Persatrix/pull/781) | 2026-07-27 |
| 2 | L2 fact widening, SHADOW (0031 fact-scope amendment) | `feature/v0312-rfc0049-post-gate-l2-widening` | ✅ Merged | [#782](https://github.com/mkhomutov/Persatrix/pull/782) | 2026-07-27 |
| 3 | L1 room-first ranking, SHADOW (L1 amendment) | `feature/v0312-rfc0049-post-gate-l1-ranking` | ✅ Merged | [#783](https://github.com/mkhomutov/Persatrix/pull/783) | 2026-07-27 |
| 4 | measurement gate → live flip (verdict GREEN — both widenings LIVE) | `feature/v0312-rfc0049-post-gate-promotion` | ✅ Merged | [#784](https://github.com/mkhomutov/Persatrix/pull/784) | 2026-07-28 |
| 5 | closeout: MT + docs + ISSUE-0084 close + RFC flip | `feature/v0312-rfc0049-closeout` | ✅ Merged | [#785](https://github.com/mkhomutov/Persatrix/pull/785) | 2026-07-28 |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged

## Dependency Graph

```
RFC 0037 PR 5 on main (§D gate + §F filter)                       ══ MERGE GATE
   │
   ├── PR 1 (capture: topic.* predicates + extractor + recall seeding
   │     + blast-radius review)
   │       │
   │       └── PR 2 (L2 fact widening, SHADOW: cross-room fact recall
   │             behind the gate + shadow-mode evaluation plumbing)
   │
   └── PR 3 (L1 widening, SHADOW: session filter wall → room-first ranking
         + gated cross-room episodic recall mode)
         (independent of PRs 1–2; either order)

        PR 2 ─┐
        PR 3 ─┴─→ PR 4 (measurement + live flip: golden-trace shadow verdict
                    → promote L1+L2 to the live prompt, or ship shadow-only
                      with the flip criterion documented)
                        │
                        └── PR 5 (closeout: docs + ISSUE-0084 close
                              + gradient docs + RFC flip)
```

## PR 1 — `feature/v0312-rfc0049-post-gate-topic-capture` (the 0026 amendment, expanded stub → implementation)

- Expand [0026-amendment-topic-subject-predicates.md](0026-amendment-topic-subject-predicates.md) from stub to full implementation amendment (its own instruction).
- `agents/memory/fact_predicates.py`: the **closed, guarded** `topic.*` namespace (`topic.has_status`, `topic.has_deadline`, `topic.decided`, `topic.owned_by` — the stub's seed set; new verbs still require an amendment + PR).
- `agents/persona_runtime/fact_extractor.py`: extractor prompt proposes salient canonicalized topic subjects alongside self/counterparty; recall seeding extracts candidate topic subjects from the inbound stimulus.
- **Named in-PR security gate**: the allowlist blast-radius re-review — subject canonicalization limits, object length bounds, the RFC 0009 delimiter escape — recorded in the amendment before the prompt ships.
- Protection stamping is inherited (RFC 0037 §C, already live) — a topic fact from a `restricted` DM is stamped `restricted`; tests assert it.

## PR 2 — `feature/v0312-rfc0049-post-gate-l2-widening` (the 0031 fact-scope amendment, shadow)

- Fact-tier recall drops the room wall: L2 facts (person *and* topic) become cross-room candidates, every candidate passing the RFC 0037 §D gate; provenance (`source_channel_id`) rides for ranking/telemetry.
- **Shadow mode**: cross-room candidates are computed and logged (what *would* have been injected) without entering the live prompt; the RFC 0044 harness records shadow traces for the PR 4 measurement.
- Tests: a topic fact taught in room A is a candidate in room B; a `restricted`-stamped fact is withheld acting-`public`; epoch/principal walls asserted intact.
- Closes the *scope* half of the scenario; capture (PR 1) supplies the tier.

## PR 3 — `feature/v0312-rfc0049-post-gate-l1-ranking` (the L1 amendment, shadow)

- `agents/memory/_session_filter.py` + the episodic recall path: the RFC 0031 §D hard exclusion becomes **room-first ranking** (same-room boost; other-room episodes admissible, demoted); the gated cross-room episodic recall mode this names.
- Every cross-room candidate passes the §D gate (identical rule to L2); the CLI/debug `sessions="*"` path is unchanged (it was never the mechanism).
- **Shadow mode**, same plumbing as PR 2.
- Tests: ranking order (same-room first at equal relevance); gate enforcement on cross-room episodic candidates; `EVAL-MEMORY-001` replays green (the continuity tripwire).

## PR 4 — `feature/v0312-rfc0049-post-gate-promotion` (the measurement gate → live flip)

**Depends on PRs 2 *and* 3** (not on the merge gate alone): it promotes L1+L2 together, so both shadow slices must be on `main` with shadow traces recorded before the verdict can be run.

- Run the shadow verdict: golden-trace evaluation (RFC 0044) of prompt quality under the RFC 0017 injection budget + dementia-test continuity on the room-first goldens.
- **Green** → flip L1+L2 shadow → live (small, config-flip-shaped diff) + the room-axis integration eval (the L1 amendment's EVAL follow-up).
- **Red** → v0.3.12 ships shadow-only; the flip criterion and the failing measurement are documented in the release notes and a tracked issue — the release does not block ([master plan §Risk](../v0.3.12-plan.md#risk-and-mitigations)).

**As-implemented (verdict GREEN — both widenings promoted).** The measurement consumer is `evaluators/shadow_measurement.py` (three criteria over the `tier`-keyed `shadow_traces`, both withhold fields read: `label_integrity` / `bounded_volume` / `continuity`); the measurement seed is `EVAL-MEMORY-002` (the scenario-2 DM→standup arc, shadow-pinned via the new per-recipe `setup.memory` override + per-interaction `room:` driver extension), and the recorded verdict — 2 gate-admitted cross-room candidates, 0 withheld, 0 unknown-label, goldens 4/4 with `EVAL-MEMORY-001` byte-identical un-re-recorded — is re-executed on every CI run by `tests/integration/test_cross_room_seed_replay.py`. The flip: `cross_room: live` default on both knobs (one widened read per turn; live episodic reinforces; wall/boost guard at the query helpers), and the room-axis integration eval is `EVAL-MEMORY-003` (load-bearing via the shadow-pinned-replay cassette-miss strip test). Promotion details live in the two amendments' Promotion sections.

## PR 5 — `feature/v0312-rfc0049-closeout`

- `MT-MEMORY-CROSSROOM-001` (the DM→standup scenario, live) authored + run.
- Docs: `docs/guides/persona-agents.md` + `sessions.md` gradient/scope sections; [memory-scope-axes.md](../memory-scope-axes.md) cross-links verified (decision 4 already annotated).
- Close [ISSUE-0084](../issues/ISSUE-0084-fact-scope-by-subject-not-uniform-session.md) (re-rooted, not implemented-as-filed); RFC 0049 front-matter → ⚠️ Partially Implemented (P0–1 v0.3.12 ✅; P2–4 v0.4.0); ROADMAP row flip.

**As-implemented.** [MT-MEMORY-CROSSROOM-001](../manual-tests/MT-MEMORY-CROSSROOM-001.md) is authored with **live execution slated for v0.3.12 release-prep** — the MT-AUTONOMOUS precedent, and [v0.3.12-plan §Release-prep](../v0.3.12-plan.md) already lists it as a live gate-sweep deliverable (the deterministic backbone runs in CI: `test_cross_room_seed_replay.py` + `EVAL-MEMORY-003`). The docs sweep also **re-anchored the two MTs whose absence bars the promotion redefined**: [MT-MEMORY-005 §V6](../manual-tests/MT-MEMORY-005-dementia-test.md) and [MT-SESSION-003](../manual-tests/MT-SESSION-003.md) now carry the cross-run bar on the **epoch** axis (a fresh session surfacing a person-fact is the v0.3.12 feature, not the F-3 reproduction), and [sessions guide §7](../guides/sessions.md) documents the live per-tier posture + rollback levers. memory-scope-axes decision 4's supersession annotation verified in place — no edit needed. ISSUE-0084 closed re-rooted per §D (subject-classification machinery deliberately never built).

---

## Risks

| Risk | Mitigation |
|------|------------|
| Widening merges before the gate. | The merge-gate is stated here, in the master plan, and in the L1 amendment; `post-gate` branch names; reviewers reject early bases. |
| Cross-room candidates flood the injection budget. | Room-first ranking + shadow-first + the PR 4 measurement gate; red gate ⇒ shadow-ship, not a blocked release. |
| The `topic.*` extractor widening re-opens prompt-injection surface. | PR 1's named blast-radius review; the namespace stays a closed allowlist. |
| Continuity (dementia-test) regresses under ranking. | `EVAL-MEMORY-001` replay is a per-PR tripwire on PR 3 and a promotion criterion on PR 4. |
