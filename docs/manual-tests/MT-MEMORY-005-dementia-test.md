# Manual Test MT-MEMORY-005: Persona Memory — Dementia Test

**Test ID**: `MT-MEMORY-005`
**Feature Area**: Memory (qualitative acceptance gate)
**Version**: 1.0
**Created**: 2026-05-01
**Last Updated**: 2026-05-17
**Status**: Active (promoted from Draft scaffold after the v0.3.1 release-prep PR 4 re-run — RFC 0026 Phase 1 landed)

---

## Overview

**Purpose**: Qualitatively verify that the persona-memory subsystem passes the **dementia test** — recall@k metrics alone are insufficient because high recall scores can co-exist with a persona that fails to *act* on retrieved facts. The acceptance bar is qualitative: across a five-interaction scenario over 30 minutes, the persona must reference earlier-established facts when natural triggers appear, *without* keyword overlap to seed retrieval.

**Scope**: End-to-end persona behaviour after the [Memory Quality Roadmap](../memory-quality-roadmap.md) deliverables ship — specifically [§A facts tier](../memory-quality-roadmap.md#a-promote-key_facts-to-a-declarative-fact-tier) (RFC 0026), [§B continuity bridge](../memory-quality-roadmap.md#b-continuity-bridge-across-interaction-close), [§D outcome-tagged importance](../memory-quality-roadmap.md#d-outcome-tagged-importance-not-turn-count-importance), [§E reflection-driven consolidation](../memory-quality-roadmap.md#e-reflection-driven-consolidation-not-llm-clustering) (RFC 0027), and [§F since-we-last-spoke header](../memory-quality-roadmap.md#f-structured-since-we-last-spoke-prompt-header). Leg 4 also serves as the **trigger signal** for the deferred draft RFC 0024 (vector recall) per [v0.3.0-plan.md MQ-8](../v0.3.0-plan.md#memory-quality-follow-ups-v03x-and-beyond).

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

The test runs **five legs** over **five interactions**. Legs cover the five signal classes the dementia test asserts: **named entity**, **stated preference**, **explicit commitment**, **paraphrase recall**, and **persona self-consistency**. The five-interaction shape spreads them over enough wall-clock time (30 minutes minimum) to exercise [RFC 0020](../rfcs/0020-interaction-lifecycle.md) idle-gap closure between turns 2/3 and 4/5. Variants gate which legs apply — see §Variants. Allow 45 minutes for a full V4 run including the paraphrase + self-consistency legs.

### Telemetry (required for diagnosis)

For each turn, capture the per-tier provenance of what the [`MemoryBudget` allocator](../rfcs/0017-persona-memory-injection-budget.md#b-memory-budget-allocator) admitted into the prompt: `{working: [...], relationship: [...], facts: [...], notes: [...], episodic: [...]}`. A leg fail with the relevant fact / episode in the admitted slice is a **reasoning miss** (LLM had it and ignored it). A leg fail with the fact / episode absent is a **recall miss**. Without this distinction, every fail becomes an open-ended investigation. Provenance is a debug-mode artifact (gate `PERSATRIX_MEMORY_PROVENANCE=1`); not a production log path. Tracked as MQ-11.

### Setup

1. Start the orchestrator and a persona named `dementia-test-bob`.
2. **Pin the operator session id for the whole arc** (RFC 0031 Phase 2 — v0.3.5): `export PERSATRIX_SESSION_ID=dementia-arc-$(date +%Y%m%d)` (PowerShell: `$env:PERSATRIX_SESSION_ID = "dementia-arc-$(Get-Date -Format yyyyMMdd)"`). Re-export it before *every* interaction window — the orchestrator + persona-runtime both snapshot the value at start; under v0.3.5's §D recall default, single-session recall is the dementia-test recall path ([OQ #1 resolution 1a](../rfcs/0031-per-session-namespacing-channels.md#open-questions)). Forgetting to pin the value causes spurious recall misses on every leg because each interaction would resolve a different session id.
3. From the CLI, open a chat session: `persatrix chat dementia-test-bob`.
4. Note the session start time. Plan to leave ≥ 11 minutes of idle time between Interaction 2 and Interaction 3 (forces RFC 0020 idle-gap closure).
5. Plan to leave ≥ 11 minutes between Interaction 4 and Interaction 5.

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

### Leg 4 — Paraphrase Recall (Interaction 1 → 5)

Leg 4 is the **trigger leg for draft RFC 0024 (vector recall)**. RFC 0026 facts handle structured recall; paraphrase recall sits between facts and prose summaries. Without this leg, MQ-8 has no signal and vectors stay deferred forever.

**Interaction 1 (establish, paraphrase form)**: Embed a topic that admits paraphrase recall — established under one phrasing, retrieved under another:

> "We need to renegotiate the rate card before the contract renewal."

**Expected**: persona acknowledges naturally.

**Interaction 5 (trigger, paraphrase, no keyword overlap with "rate card")**: Ask the topic by paraphrase:

> "Have we ever discussed pricing?"

**Pass criterion**: persona surfaces the rate-card discussion (referenced as pricing, contract terms, or similar). BM25 over multi-turn summaries may pass this; consistent fail across runs is the data signal that triggers RFC 0024.

**Fail criterion**: persona answers "no, this is new" or asks "what kind of pricing?" without referencing the prior rate-card thread. Two or more consecutive **V2 or V3** fails on Leg 4 + provenance showing the relevant episode absent from the `episodic` slice = MQ-8 trigger met. (V4 fails do not count: post-RFC 0027 consolidation notes can carry the relationship-arc abstraction and mask the vector-recall signal — see the §Notes coda.)

### Leg 5 — Persona Self-Consistency (Interaction 1 → 5)

Leg 5 measures the persona's stability *as a subject of its own facts* — orthogonal to user-about-memory. Maps to [RFC 0026 §C.4](../rfcs/0026-declarative-facts-tier.md) (`subject = "self"`).

**Interaction 1 (establish persona self-claim)**: Elicit a stable trait. Phrase the question naturally:

> User: "What kinds of books do you enjoy?"
> Persona: "I really like sci-fi — especially Ted Chiang."

**Expected**: persona makes a coherent self-claim. The fact extractor at interaction close should write `(self, has_preference, "sci-fi / Ted Chiang", source_interaction_id=...)`.

**Interaction 5 (trigger)**: Re-ask, framed differently:

> "Are you a sci-fi fan?" or "Recommend me a book."

**Pass criterion**: persona's response is consistent with Interaction 1 (affirms the preference; or qualifies it consistently — "yes, especially short fiction"). Bonus: persona references Ted Chiang.

**Fail criterion**: persona contradicts Interaction 1 ("not really a sci-fi person") or invents a different stable claim ("I'm more of a literary-fiction reader") without bridging from the original.

---

## Expected Results Summary

| Leg | Established at | Triggered at | Pass criterion | Variant gate | V2 expectation (post-RFC 0026 PR 4) | Pass/Fail |
|-----|----------------|--------------|----------------|--------------|-------------------------------------|-----------|
| 1 — Named Entity | Interaction 1 | Interaction 4 | "Mira" or "your daughter" referenced without keyword overlap | V2+ | ✅ Expected pass — `(sender, has_child_named, "Mira")` admits via facts tier on the trigger turn | ☐ |
| 2 — Stated Preference | Interaction 2 | Interaction 5 | Recommendation honors preference (no phone-call suggestion) | V2+ | ✅ Expected pass — `(sender, dislikes, "phone calls")` / `(sender, prefers, "text or async")` admits | ☐ |
| 3 — Explicit Commitment | Interaction 3 | Interaction 5 | Open commitment referenced or correctly handled | V2 (fact form) / V4 (commitment form) | ⚠️ V2 partial — `(sender, committed_to, ...)` may admit; structured commitment tracking is v0.4.0 (RFC 0021 P2) | ☐ |
| 4 — Paraphrase Recall | Interaction 1 | Interaction 5 | Rate-card / pricing thread surfaced via paraphrase | V2+ (fail = MQ-8 signal) | ↔️ Unchanged from V1 — facts tier does not cover paraphrase recall; consistent V2/V3 fails escalate to RFC 0024 | ☐ |
| 5 — Self-Consistency | Interaction 1 | Interaction 5 | Persona's self-claim stable across the window | V2+ | ✅ Expected pass — `(self, self.has_preference, ...)` admits via the PR 4 `self`-subject seed | ☐ |

**Overall pass per variant**: all variant-gated legs pass. A pass on N-1 of N is partial — investigate which deliverable hasn't landed yet. Two or more fails = fail. Per-leg telemetry (recall miss vs. reasoning miss) determines the next action.

**Expected V2 outcomes (post-RFC 0026 PR 4)**: Legs 1, 2, and 5 flip from V1 baseline-fail to V2 pass. Legs 3, 4 hold unchanged — Leg 3 because structured commitment tracking is v0.4.0 scope (RFC 0021 P2), Leg 4 because paraphrase recall is the MQ-8 trigger for RFC 0024 (deferred to v0.3.x). RFC 0026 PR 4 ships the `last_recalled_at` reinforcement write and the `self` subject seed needed for Leg 5; the per-turn tier-provenance log under `PERSATRIX_MEMORY_PROVENANCE=1` (MQ-11) is the diagnostic that distinguishes a recall miss from a reasoning miss on any leg fail.

---

## Variants

### V1 — Pre-RFC 0026 baseline

Run the full procedure against `main` *before* RFC 0026 Phase 1 lands. Record results as the "before" snapshot. Most legs are expected to fail — this is the baseline that justifies the roadmap.

### V2 — Post-RFC 0026 Phase 1 (facts tier shipped)

Re-run after [RFC 0026](../rfcs/0026-declarative-facts-tier.md) Phase 1 + Phase 2. Legs 1, 2, 4, 5 should pass:
- **Leg 1 / 2**: extractable as facts (`(subject=<sender_id>, predicate="has_child_named", object="Mira")` and `(subject=<sender_id>, predicate="prefers", object="text or async")`). `<sender_id>` is the canonical entity key resolved by [RFC 0026 §C](../rfcs/0026-declarative-facts-tier.md#c-subject-canonicalization) at write time — not a literal `"user"` string. Tests asserting on these tuples must use the resolved `sender_id`, not a hard-coded placeholder. The relationship predicate is gender-neutral by design ([RFC 0026 §B](../rfcs/0026-declarative-facts-tier.md#b-extraction-at-interaction-close)) — a daughter-vs-son distinction, when load-bearing, surfaces in the prose summary that ships in the same close-path round-trip.
- **Leg 4 (paraphrase)**: passes if BM25 over interaction summaries is sufficient. A consistent **V2 or V3** fail on Leg 4 — with provenance showing the relevant episode is absent from the `episodic` slice — is the [MQ-8](../v0.3.0-plan.md#memory-quality-follow-ups-v03x-and-beyond) trigger for RFC 0024. V4 fails do not count: post-RFC 0027 consolidation can mask vector-recall need behind consolidation notes (see §Notes coda).
- **Leg 5 (self-consistency)**: passes if `subject = "self"` predicates are in the Phase-1 vocabulary (RFC 0026 OQ #10). If Phase 1 ships without self-predicates, Leg 5 is a V4 leg, not V2.

**Leg 3 in V2** — *not* a regression if it fails. Structured commitment tracking lands in v0.4.0 with [RFC 0021 P2](../rfcs/0021-persona-temporal-awareness.md). In V2, Leg 3 passes only if the persona references the spreadsheet via fact-tier extraction, e.g. `(<sender_id>, committed_to, "send budget spreadsheet by tomorrow")` where `<sender_id>` is the [§C-canonicalized](../rfcs/0026-declarative-facts-tier.md#c-subject-canonicalization) subject key. A V2 Leg-3 fail with the fact present in the `facts` slice is a *reasoning miss* (LLM ignored the fact). A V2 Leg-3 fail with the fact absent is a *recall miss* — investigate the extractor's commitment-class predicates, not RFC 0021. Either way: **not a regression**, just a known gap until v0.4.0.

### V3 — Post-§B continuity bridge

Re-run after [§B](../memory-quality-roadmap.md#b-continuity-bridge-across-interaction-close) ships. Eliminates a separate failure mode where a fast follow-up message after an idle close starts with empty working memory.

### V4 — Post-RFC 0027 (consolidation)

Re-run after [RFC 0027](../rfcs/0027-reflection-driven-consolidation.md) lands in v0.4.0. Legs that depend on multi-interaction relationship-arc reasoning ("our relationship has shifted") become testable; extend the procedure with a fourth leg after that ships.

### V5 — Post-RFC 0031 Phase 2 (session isolation, v0.3.5)

Re-run after [RFC 0031 Phase 2](../rfcs/0031-per-session-namespacing-channels.md) lands. Two extensions:

1. **Single-session arc**: canonical V2 run with the Setup `PERSATRIX_SESSION_ID` pin — every leg passes with default recall ([OQ #1 1a](../rfcs/0031-per-session-namespacing-channels.md#open-questions)). The Phase 2 PR-plan calls this the **dementia-test bridge**.
2. **Multi-session no-bleed**: close the arc, re-export a fresh `PERSATRIX_SESSION_ID`, and re-run Leg 1's Interaction 4 trigger. The persona must **not** reference Mira — absence is the v0.3.5 promise; a reference is the F-3 reproduction. Cross-session continuity by opt-in is the Phase 3 CLI path (`persatrix memory recall --sessions=…`).

V5 supersedes the v0.3.x re-run cadence — every v0.3.5+ run is a V5 run.

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
| 2026-05-17 | Claude (Opus 4.7) | Windows 11 + Docker | V2 | Pass | v0.3.1 surface run. All 5 legs Pass; release-blocker Legs 1/2/5 Pass. Facts tier extracted 0 facts at run time (root-caused as ISSUE-0054); legs carried by the RFC 0034 conversation window. See [v0.3.1-execution-report.md](v0.3.1-execution-report.md#mt-memory-005-acceptance-passfail-gate). |
| 2026-05-17 | Claude (Opus 4.7) | Windows 11 + Docker | V2 | Pass | Release-prep PR 4 re-run on the RC tip (`main` 47a7797, post-ISSUE-0054-fix). All 5 legs Pass; release-blocker Legs 1/2/5 Pass **via the RFC 0026 facts tier** (6 facts extracted + recalled, clean fence-free episode summaries). F-1 confirmed closed. See [v0.3.1-execution-report.md](v0.3.1-execution-report.md#mt-memory-005-re-run--release-prep-pr-4-release-candidate-tip). |
| 2026-06-01 | Claude (Opus 4.8) | macOS + Docker | V5 | Bridge ✅ / arc carried | v0.3.5 RP PR 1: bridge green live ([`test_session_continuity.py`](../../tests/integration/test_session_continuity.py)) + recall isolation ([MT-SESSION-003](MT-SESSION-003.md)); full arc run live in PR 4 (row below). |
| 2026-06-01 | Claude (Opus 4.8) | macOS + Docker (Anthropic) | V5 | **Pass** | v0.3.5 RP **PR 4** live (§4 hard-block): all 5 legs Pass + no-bleed holds; facts-tier-empty = [ISSUE-0054](../issues/ISSUE-0054-rfc0026-facts-tier-extracts-no-facts.md). Evidence: [exec report § PR 4](v0.3.5-execution-report.md#re-execution--release-prep-pr-4-post-version-bump-rc-tip). |

---

## Notes

- The dementia test is the **qualitative acceptance gate** for memory-quality work. A green `recall@k` automated test does not substitute for a green leg here — the failure modes are different (recall ranks fragments; this test asks the persona to *act* on what it retrieved).
- Each leg's "no keyword overlap" rule is the test's load-bearing constraint. If the trigger turn contains the established entity or preference, the test exercises retrieval-by-keyword, not memory-by-relevance, and the result is meaningless.
- This test should re-run before any v0.3.x or v0.4.0 release that touches memory. Add a row to the Test Results table with the variant identifier.
- After [§D outcome tags](../memory-quality-roadmap.md#d-outcome-tagged-importance-not-turn-count-importance) lands, instrument the test to capture the `outcome` tag emitted by the summarizer for each interaction — Leg 3's commitment leg should produce `outcome: commitment`.
- **Leg 4 is a one-way trigger** for [MQ-8 (RFC 0024 vector recall)](../v0.3.0-plan.md#memory-quality-follow-ups-v03x-and-beyond) — two or more consecutive V2/V3 fails on Leg 4 (with provenance showing recall miss, not reasoning miss) is the data signal that escalates RFC 0024 from deferred to in-scope.
- **Leg 5 is the only leg that tests the persona as a subject of its own facts**. Self-consistency drift is a distinct dementia mode from user-fact drift; do not collapse the two.
- **Referential follow-ups depend on RFC 0034.** This test's legs all establish a fact through a *self-contained* statement, then trigger across an interaction boundary — that path is RFC 0026's facts tier. A *referential* follow-up (`"I like it"`, where the referent sits in the persona's own prior turn) is a distinct failure mode: the fact extractor never sees the referent until [RFC 0034](../rfcs/0034-persona-conversational-working-memory.md) Phase 1 reconstructs the in-conversation transcript ([ISSUE-0052](../issues/ISSUE-0052-persona-conversational-working-memory-gap.md)). Expected to pass once RFC 0034 + RFC 0026 both merge in v0.3.1; [MT-PERSONA-CONVERSATION-001](MT-PERSONA-CONVERSATION-001.md) is the dedicated within-conversation acceptance test for that path. Without RFC 0034 this test's V2 expectations silently undercount — a fact established by reference is never extracted, so the leg fails as a *recall miss* with no fact in the `facts` slice.
