# Manual Test MT-MEMORY-CROSSROOM-001: Memory that travels — a project fact taught in a DM is known in the standup

**Test ID**: `MT-MEMORY-CROSSROOM-001`
**Feature Area**: Memory (cross-room persona experience — RFC 0049 Phases 0–1 × the RFC 0037 confidentiality gate)
**Version**: 1.0
**Created**: 2026-07-28
**Last Updated**: 2026-07-28
**Status**: Active — authored at RFC 0049 PR 5; **live execution is a v0.3.12 release-prep deliverable** (run against a real provider per [v0.3.12-plan §Acceptance](../v0.3.12-plan.md#acceptance-for-v0312)).

---

## Overview

**Purpose**: Verify the v0.3.12 headline promise live — **tell a persona something in one room and it knows it in every room it belongs to, without leaking what it learned in a confidential room**. This is [RFC 0049's worked-example scenario 2](../rfcs/0049-memory-consolidation-gradient.md#worked-example-the-two-test-scenarios), end-to-end on a real provider: a project fact taught in a DM ("Atlas ships Friday") is captured as `topic.*` knowledge at interaction close, recalled in a group channel the persona belongs to via the live L2 cross-room widening, and still recalled on the DM re-ask afterwards. A fresh-epoch leg confirms the walls that stay absolute.

**Scope**: the three RFC 0049 Phase-1 amendments in their shipped **live** posture — [0026 topic predicates](../rfcs/0026-amendment-topic-subject-predicates.md) (the capture path), [0031 fact-scope](../rfcs/0031-amendment-fact-scope-by-consolidation-level.md) (L2 facts cross rooms, `memory.facts.cross_room: live`), [0049-L1](../rfcs/0049-amendment-l1-cross-room-availability.md) (episodic room-first ranking, `memory.episodic.cross_room: live`) — with every cross-room candidate passing the [RFC 0037 §D gate](../rfcs/0037-memory-confidentiality-channel-classification.md#d-the-hard-gate-at-memory-injection). The DM and the group channel are distinct rooms (sessions auto-mint per `(agent, channel)` — [sessions guide §3](../guides/sessions.md#3-the-per-request-auto-binding)), so the recall in Leg 2 is structurally cross-room.

**Out of Scope** — explicitly deferred, **not asserted** here:

- **The confidentiality matrix** (learn-`restricted` / act-`public` withhold, the tripwire leg) — `MT-PERSONA-CONFIDENTIALITY-001`, the RFC 0037 closeout artifact; the deterministic backbone is already pinned in [`tests/integration/test_confidentiality_gate.py`](../../tests/integration/test_confidentiality_gate.py).
- **The shadow measurement gate** — the promotion verdict is re-executed on every CI run ([`tests/integration/test_cross_room_seed_replay.py`](../../tests/integration/test_cross_room_seed_replay.py)); this MT does not re-derive it.
- **Live cross-channel relay** (RFC 0038 §C–§E) — v0.4.0; this MT is the *persisted-memory* complement.
- **A dedicated L1 episodic leg** — room-first ranking has no clean human-observable trigger (FTS relevance on natural channel turns rarely surfaces a cross-room episodic delta); the ranking property is pinned quantitatively in the unit/integration suites and the `EVAL-MEMORY-001` continuity goldens.

---

## Related Documentation

- [RFC 0049 — Memory Consolidation Gradient](../rfcs/0049-memory-consolidation-gradient.md) — the one law; scenario 2 is this MT's script.
- The three amendments: [0026 topic predicates](../rfcs/0026-amendment-topic-subject-predicates.md) · [0031 fact-scope](../rfcs/0031-amendment-fact-scope-by-consolidation-level.md) · [0049-L1](../rfcs/0049-amendment-l1-cross-room-availability.md) (each carries its Promotion section with the recorded verdict).
- [RFC 0037 — Memory Confidentiality](../rfcs/0037-memory-confidentiality-channel-classification.md) — the §D gate every cross-room candidate passes.
- [Sessions guide](../guides/sessions.md) — session = room continuity; [epochs guide](../guides/epochs.md) — the run-isolation axis Leg 4 exercises.
- [MT-MEMORY-005 — the dementia test](MT-MEMORY-005-dementia-test.md) — the single-room continuity gate this MT extends to the multi-room case (its V5 no-bleed leg is re-anchored to the **epoch** axis by §V6 — Leg 4 here asserts the same wall).

**Related Automated Tests** — the deterministic CI backbone of this MT:

- [`tests/integration/test_cross_room_seed_replay.py`](../../tests/integration/test_cross_room_seed_replay.py) — replays `EVAL-MEMORY-002` (shadow-pinned) + `EVAL-MEMORY-003` (live, the room-axis integration eval) and re-runs the promotion verdict on every CI run.
- [`tests/unit/python/test_cross_room_live.py`](../../tests/unit/python/test_cross_room_live.py) — the live-default injection path: facts `sessions="*"`, episodic ranked + reinforcing, no *ungated* widening.
- [`tests/integration/test_confidentiality_gate.py`](../../tests/integration/test_confidentiality_gate.py) — the §D gate end-to-end (learn-restricted → act-public withheld).

This live MT confirms the *operator-observable* behaviour on a real provider; the widening/gate invariants themselves are pinned in CI.

---

## Preconditions

1. The compose stack is up against a **real provider** (`make demo-anthropic` or equivalent — *not* the mock provider; capture quality and natural-phrasing recall need real replies).
2. A **clean store** (`make reset`, or a fresh `PERSATRIX_EPOCH` for the whole arc) so prior facts do not steer the run.
3. `agent-ember-owl` is up and is a member of `group:planning` (the bundled roster has it at `respond: addressed` — the Leg 2 trigger @-mentions it).
4. `persatrix` CLI on `PATH` pointed at the running orchestrator.
5. Defaults unchanged: `memory.facts.cross_room` and `memory.episodic.cross_room` both resolve `live` (the shipped default — verify no overlay pins `shadow`/`off`), DMs stamp `internal` (`dm_default_classification` absent), `group:planning` is `internal` (the bundled declaration).
6. Optional but recommended: `PERSATRIX_MEMORY_PROVENANCE=1` on the persona container, so a leg fail can be split into a **recall miss** (fact absent from the admitted `facts` slice) vs. a **reasoning miss** (admitted but ignored) — the MQ-11 discipline [MT-MEMORY-005 §Telemetry](MT-MEMORY-005-dementia-test.md#telemetry-required-for-diagnosis) established.

---

## Test Procedure

The arc is **teach (DM) → close → ask (group) → re-ask (DM) → fresh-epoch wall**. Timing matters once: fact extraction runs at **interaction close** ([RFC 0020](../rfcs/0020-interaction-lifecycle.md)), and an idle DM interaction closes on the *next* event after the idle gap (default 600 s) — so Leg 1 ends with an explicit bridge turn that triggers the close before Leg 2 asks.

### Leg 1 — Teach the fact in the DM

Open the DM (chat rides the channels wire model as `dm:<a>:<b>` — the chat-as-DM amendment) and teach one topic fact embedded in natural conversation:

```bash
persatrix chat ember-owl --user alex
```

> "Quick heads-up before I forget — **Atlas ships Friday**. The exec demo is that same afternoon."

**Expected**: a natural acknowledgement; no memory call-out required.

Now **close the teaching interaction**: leave the DM idle **≥ 11 minutes**, then send a low-content bridge turn in the same DM:

> "Thanks — talk later."

The bridge turn trips the idle close of the teaching interaction (close runs before that turn's injection), and the close-path extractor writes the topic fact — expected shape `(atlas, topic.has_deadline | topic.has_status, …)`, stamped `internal` (inherited from the DM's classification, RFC 0037 §C).

**Optional verification** (debug, not a pass criterion): on the persona container,

```bash
docker compose exec agent-ember-owl sqlite3 /app/data/memory.db \
  "SELECT subject, predicate, object, protection_level, session_id FROM facts WHERE predicate LIKE 'topic.%';"
```

→ at least one `atlas` row with a `topic.*` predicate, `protection_level = internal`, and the **DM room's** session id.

### Leg 2 — Ask in the standup (the headline: cross-room recall)

In the group channel — a **different room**, hence a different auto-minted session — ask about the topic **by name, without the fact's content** (no "Friday", no "ship date"):

```bash
persatrix channel send planning "What's the latest on Atlas — anything the team should plan around this week?" \
    --as alex --mention ember-owl
```

**Pass criterion**: ember-owl's reply surfaces the Friday ship date (and ideally the exec demo) — knowledge it was never told in this room. Under the hood: the inbound turn names `atlas` → deterministic topic seeding (word-boundary match, no LLM) → the live L2 widened recall (`sessions="*"`) finds the DM-taught fact → the §D gate admits it (`internal` fact, `internal` acting channel).

**Fail criterion**: the persona asks what Atlas is, gives a generic status answer, or invents a date. With provenance on, split the fail: fact absent from the admitted `facts` slice = recall miss (investigate capture/seeding); present but unused = reasoning miss.

> **The trigger must name the topic.** Topic seeding is deterministic subject matching — `atlas` (or a multi-word subject verbatim) has to appear in the trigger text. This is by design, and it is *not* the keyword-overlap foul of [MT-MEMORY-005](MT-MEMORY-005-dementia-test.md): the no-overlap rule protects the fact's **content** (the object — "ships Friday"), which the trigger here still never contains. A trigger that names neither the topic nor the counterparty exercises nothing.

### Leg 3 — Re-ask in the DM

Back in the original DM:

> "Remind me — what's our Atlas timeline?"

**Pass criterion**: the Friday date again (same-room recall, plus the fact survived the group-channel turn untouched). This is the "re-learn in DMs" half of scenario 2.

### Leg 4 — The wall that stays: a fresh epoch inherits nothing

Repeat the Leg 2 ask under a **fresh epoch** (run isolation — the axis that stays a hard wall, with `principal`):

```bash
persatrix channel send planning "What's the latest on Atlas?" \
    --as alex --mention ember-owl --epoch mt-crossroom-fresh
```

**Pass criterion**: **no** reference to Friday or the exec demo — absence is the promise. Cross-room widening ranges over *rooms*, never across epochs or tenants; a recall here reproduces the F-3 class of leak and is release-blocking.

---

## Expected Results Summary

| Leg | Room | Trigger discipline | Pass criterion | Pass/Fail |
|-----|------|--------------------|----------------|-----------|
| 1 — Teach + close | DM | natural statement; ≥ 11 min idle + bridge turn | ack; (optional) `topic.*` fact row stamped `internal` with the DM session id | ☐ |
| 2 — Standup ask | `group:planning` | names `atlas`, never the content | reply surfaces the Friday ship date, cross-room | ☐ |
| 3 — DM re-ask | DM | names `atlas`, never the content | Friday recalled again in the original room | ☐ |
| 4 — Fresh epoch | `group:planning`, `--epoch` override | same ask, fresh epoch | **no** recall — the epoch wall holds | ☐ |

**Overall pass**: Legs 2, 3, and 4 all pass. A Leg 2/3 fail is diagnosed with provenance (recall vs. reasoning miss) before filing. A Leg 4 fail is a confidentiality/isolation regression — file immediately, release-blocking.

---

## Edge Cases & Error Scenarios

### Edge Case 1: the trigger never names the topic

**Scenario**: Leg 2 is phrased "anything shipping soon?" — no `atlas` token.

**Expected Behavior**: no topic seed derives, so the fact is unlikely to load; person seeds (sender/self) still apply. Not a failure — rewrite the trigger to name the topic. (Paraphrase-recall of *topics* is the MQ-8 / RFC 0024 vector territory, out of scope here.)

### Edge Case 2: the teaching interaction never closed

**Scenario**: Leg 2 runs a few minutes after Leg 1 with no idle gap or bridge turn.

**Expected Behavior**: the extractor has not run yet, so the group-channel ask recall-misses. Test invalid — redo the Leg 1 idle + bridge, confirm the close in the logs, re-ask.

### Edge Case 3: rollback lever

**Scenario**: a live-posture problem is found and the widenings need to come off the prompt path.

**Expected Behavior**: `memory.facts.cross_room: shadow` and `memory.episodic.cross_room: shadow` (per-agent config) restore the pre-promotion posture — candidates are computed and traced, never injected. `off` disables the widened read entirely. Both are per-tier and independent.

---

## Test Results

| Date | Tester | OS | Provider | Result | Notes |
|------|--------|----|----------|--------|-------|
| — | — | — | — | — | Live execution scheduled for v0.3.12 release-prep ([v0.3.12-plan §Acceptance](../v0.3.12-plan.md#acceptance-for-v0312)). |

---

## Notes

- **Why this MT exists when CI already replays the scenario**: `EVAL-MEMORY-003` drives the *runtime* through the recorded arc deterministically; this MT is the qualitative gate that a real provider, real idle-close timing, and natural operator phrasing produce the same behaviour an operator would actually see — the same recall@k-vs-dementia distinction MT-MEMORY-005 draws.
- **Session hygiene**: no `PERSATRIX_SESSION_ID` pinning here, unlike MT-MEMORY-005 — the whole point is the per-`(agent, channel)` auto-binding minting *different* rooms for the DM and the group channel. Pinning one session across both would make Leg 2 same-room and vacuous.
- **Classification posture**: both rooms sit at `internal` in this MT, so the §D gate admits silently. The gate's *withhold* half (teach in a `restricted` room, ask in an `internal` one) is `MT-PERSONA-CONFIDENTIALITY-001` territory — do not bolt it onto this arc; the two MTs are complementary halves of the headline sentence.
- The shadow→live promotion evidence (criteria, recorded numbers, deferred decisions) lives in the [fact-scope amendment's Promotion section](../rfcs/0031-amendment-fact-scope-by-consolidation-level.md#promotion-v0312-pr-4--the-measurement-gated-flip) and the [L1 amendment's](../rfcs/0049-amendment-l1-cross-room-availability.md#promotion-v0312-pr-4--the-measurement-gated-flip).
