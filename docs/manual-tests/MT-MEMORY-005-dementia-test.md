# Manual Test MT-MEMORY-005: Persona Memory — Dementia Test

**Test ID**: `MT-MEMORY-005`
**Feature Area**: Memory (qualitative acceptance gate)
**Version**: 1.0
**Created**: 2026-05-01
**Last Updated**: 2026-05-01
**Status**: Draft (scaffold — populated after [RFC 0026](../rfcs/0026-declarative-facts-tier.md) Phase 1 lands)

---

## Overview

**Purpose**: Qualitatively verify that the persona-memory subsystem passes the **dementia test** — recall@k metrics alone are insufficient because high recall scores can co-exist with a persona that fails to *act* on retrieved facts. The acceptance bar is qualitative: across a five-interaction scenario over 30 minutes, the persona must reference earlier-established facts when natural triggers appear, *without* keyword overlap to seed retrieval.

**Scope**: End-to-end persona behaviour after the [Memory Quality Roadmap](../memory-quality-roadmap.md) deliverables ship — specifically [§A facts tier](../memory-quality-roadmap.md#a-promote-key_facts-to-a-declarative-fact-tier) (RFC 0026), [§B continuity bridge](../memory-quality-roadmap.md#b-continuity-bridge-across-interaction-close), [§D outcome-tagged importance](../memory-quality-roadmap.md#d-outcome-tagged-importance-not-turn-count-importance), [§E reflection-driven consolidation](../memory-quality-roadmap.md#e-reflection-driven-consolidation-not-llm-clustering) (RFC 0027), and [§F since-we-last-spoke header](../memory-quality-roadmap.md#f-structured-since-we-last-spoke-prompt-header).

**Out of Scope**: `recall@k` metric measurement (covered by automated tests at the `agents/memory/` layer); LLM response quality unrelated to memory; multi-agent shared-memory scenarios.

**Quality bar source**: [Memory Quality Roadmap §Quality bar — the dementia test](../memory-quality-roadmap.md#quality-bar--the-dementia-test).

---

## Related Documentation

**Feature Documentation**:
- [docs/memory-quality-roadmap.md](../memory-quality-roadmap.md) — quality bar, root causes, alternatives.
- [RFC 0026 — Declarative Facts Tier](../rfcs/0026-declarative-facts-tier.md) — §A landing surface.
- [RFC 0027 — Reflection-Driven Consolidation](../rfcs/0027-reflection-driven-consolidation.md) — §E landing surface (v0.4.0 leg).
- [RFC 0020 — Interaction Lifecycle](../rfcs/0020-interaction-lifecycle.md) — interaction-bounded episodes; §D outcome tags resolve OQ #6.
- [RFC 0021 — Persona Temporal Awareness](../rfcs/0021-persona-temporal-awareness.md) — temporal data feeding §F.

**Related Automated Tests** (to be added as deliverables ship):
- Unit tests: `agents/tests/test_fact_store.py` (RFC 0026 Phase 1)
- Integration tests: `tests/integration/test_facts_recall.py` (RFC 0026 Phase 2)
- Integration tests: `tests/integration/test_continuity_bridge.py` (§B)
- Integration tests: `tests/integration/test_consolidation_recall.py` (RFC 0027 Phase 2)

---

## Preconditions

### System Requirements

**Operating Systems**:
- ☐ Windows 10/11 (x64)
- ☐ macOS 12.0+ (Intel/Apple Silicon)
- ☐ Linux (Ubuntu 22.04+)

**Dependencies Installed**:
- Python 3.11+ and `make build-agents` complete.
- Rust toolchain and `make build-cli` complete.
- A running orchestrator (`make run-orchestrator`).
- `ANTHROPIC_API_KEY` exported (the persona is LLM-backed).

### Application State

- ☐ Fresh persona DB (or a designated test persona slug not used elsewhere).
- ☐ At least one of the [Memory Quality Roadmap](../memory-quality-roadmap.md) deliverables shipped — see Test Procedure variants.
- ☐ Persona configured with `memory.facts.enabled: true` (after RFC 0026 Phase 1).

---

## Test Procedure

The test runs **three legs** over **five interactions**. Each leg covers one of the three signal classes the dementia test asserts: **named entity**, **stated preference**, **explicit commitment**. The five-interaction shape spreads them over enough wall-clock time (30 minutes minimum) to exercise [RFC 0020](../rfcs/0020-interaction-lifecycle.md) idle-gap closure between turns 2/3 and 4/5.

### Setup

1. Start the orchestrator and a persona named `dementia-test-bob`.
2. From the CLI, open a chat session: `persatrix chat dementia-test-bob`.
3. Note the session start time. Plan to leave ≥ 11 minutes of idle time between Interaction 2 and Interaction 3 (forces RFC 0020 idle-gap closure).
4. Plan to leave ≥ 11 minutes between Interaction 4 and Interaction 5.

### Leg 1 — Named Entity (Interactions 1 → 4)

**Interaction 1 (establish)**: Tell the persona one named entity, embedded in conversation. Example:

> "I'm picking up my daughter Mira from school later — she's seven."

**Expected**: persona acknowledges naturally; no special memory call-out required.

**Interaction 4 (trigger, no keyword overlap)**: After ≥ 11 minutes idle (closing Interaction 1 + 2 + 3 in one window), open Interaction 4 with a trigger that does *not* contain "Mira" or "daughter":

> "What's a good weekend activity for a kid that age?"

**Pass criterion**: persona references Mira by name, or refers to "your daughter," within Interaction 4. The named entity must surface from memory, not from the immediate prompt.

**Fail criterion**: persona asks "How old is the kid?" or "Whose kid?" or generates generic advice with no reference to Mira.

### Leg 2 — Stated Preference (Interactions 2 → 5)

**Interaction 2 (establish)**: State a clear preference, again embedded in normal conversation:

> "I really hate phone calls — text or async always works better for me."

**Expected**: persona acknowledges naturally.

**Interaction 5 (trigger, no keyword overlap)**: After ≥ 11 minutes idle, ask:

> "What's the best way to coordinate with the contractor next week?"

**Pass criterion**: persona's recommendation respects the stated preference (e.g., suggests text/email, *not* a phone call). Bonus: persona references the preference explicitly ("since you mentioned you prefer not to call…").

**Fail criterion**: persona recommends a phone call; persona contradicts the preference; persona re-asks the preference ("Do you prefer phone or text?").

### Leg 3 — Explicit Commitment (Interaction 3 → 5)

**Interaction 3 (establish)**: Make an explicit commitment:

> "I'll send you the budget spreadsheet tomorrow."

**Expected**: persona acknowledges; ideally registers the commitment (after [RFC 0021 P2 commitments](../rfcs/0021-persona-temporal-awareness.md) ships in v0.4.0, this becomes a structured commitment row).

**Interaction 5 (trigger, no keyword overlap)**: Same Interaction 5 used by Leg 2. The trigger turn or the persona's response should naturally surface a reference to the outstanding commitment:

> Persona ideally references: "By the way, did you send the spreadsheet?" or similar.

**Pass criterion**: persona references the open commitment without prompting, OR responds appropriately if the user mentions it without re-introducing it.

**Fail criterion**: persona has no awareness of the commitment when it would naturally come up; persona re-asks what was committed.

---

## Expected Results Summary

| Leg | Established at | Triggered at | Pass criterion | Pass/Fail |
|-----|----------------|--------------|----------------|-----------|
| 1 — Named Entity | Interaction 1 | Interaction 4 | "Mira" or "your daughter" referenced without keyword overlap | ☐ |
| 2 — Stated Preference | Interaction 2 | Interaction 5 | Recommendation honors preference (no phone-call suggestion) | ☐ |
| 3 — Explicit Commitment | Interaction 3 | Interaction 5 | Open commitment referenced or correctly handled | ☐ |

**Overall pass**: all three legs pass. **Two of three** = partial; investigate which deliverable hasn't landed yet. **One or zero** = fail.

---

## Variants

### V1 — Pre-RFC 0026 baseline

Run the full procedure against `main` *before* RFC 0026 Phase 1 lands. Record results as the "before" snapshot. Most legs are expected to fail — this is the baseline that justifies the roadmap.

### V2 — Post-RFC 0026 Phase 1 (facts tier shipped)

Re-run after [RFC 0026](../rfcs/0026-declarative-facts-tier.md) Phase 1 + Phase 2. Legs 1 and 2 should now pass cleanly because both the named entity and the preference are extractable as facts (`(subject="user", predicate="has_daughter_named", object="Mira")` and `(subject="user", predicate="prefers", object="text or async")`). Leg 3 may still fail — commitments are an [RFC 0021 P2 surface](../rfcs/0021-persona-temporal-awareness.md) (v0.4.0).

### V3 — Post-§B continuity bridge

Re-run after [§B](../memory-quality-roadmap.md#b-continuity-bridge-across-interaction-close) ships. Eliminates a separate failure mode where a fast follow-up message after an idle close starts with empty working memory.

### V4 — Post-RFC 0027 (consolidation)

Re-run after [RFC 0027](../rfcs/0027-reflection-driven-consolidation.md) lands in v0.4.0. Legs that depend on multi-interaction relationship-arc reasoning ("our relationship has shifted") become testable; extend the procedure with a fourth leg after that ships.

---

## Edge Cases & Error Scenarios

### Edge Case 1: LLM provider transient error during a trigger turn

**Scenario**: The LLM call for Interaction 4 or 5 fails or returns an unrelated response.

**Expected Behavior**: Re-run the trigger turn. If the failure is reproducible, capture the trace and treat as inconclusive — not a memory failure.

### Edge Case 2: Idle window shorter than the configured `interaction_idle_timeout_sec`

**Scenario**: The tester rushes through interactions without leaving ≥ 11 minutes between them.

**Expected Behavior**: Test is invalid — RFC 0020 close path is not exercised. Re-run with the proper idle windows.

### Edge Case 3: Persona timezone misconfigured

**Scenario**: After [RFC 0021 P1](../rfcs/0021-persona-temporal-awareness.md), the recency rendering looks wrong ("3 weeks ago" when only minutes have passed).

**Expected Behavior**: Not a memory test failure — file as an RFC 0021 bug. Re-run after fix.

---

## Test Results

| Date | Tester | OS | Variant | Result | Notes |
|------|--------|----|---------|--------|-------|
| YYYY-MM-DD | [Name] | [OS] | V1 / V2 / V3 / V4 | Pass/Fail | [Notes — which legs passed/failed; LLM transcript link] |

---

## Notes

- The dementia test is the **qualitative acceptance gate** for memory-quality work. A green `recall@k` automated test does not substitute for a green leg here — the failure modes are different (recall ranks fragments; this test asks the persona to *act* on what it retrieved).
- Each leg's "no keyword overlap" rule is the test's load-bearing constraint. If the trigger turn contains the established entity or preference, the test exercises retrieval-by-keyword, not memory-by-relevance, and the result is meaningless.
- This test should re-run before any v0.3.x or v0.4.0 release that touches memory. Add a row to the Test Results table with the variant identifier.
- After [§D outcome tags](../memory-quality-roadmap.md#d-outcome-tagged-importance-not-turn-count-importance) lands, instrument the test to capture the `outcome` tag emitted by the summarizer for each interaction — Leg 3's commitment leg should produce `outcome: commitment`.
