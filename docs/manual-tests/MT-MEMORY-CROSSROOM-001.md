# Manual Test MT-MEMORY-CROSSROOM-001: Memory that travels — a project fact taught in a DM is known in the standup

**Test ID**: `MT-MEMORY-CROSSROOM-001`
**Feature Area**: Memory (cross-room persona experience — RFC 0049 Phases 0–1 × the RFC 0037 confidentiality gate, plus the RFC 0031 F-7 person-identity tier)
**Version**: 1.1
**Created**: 2026-07-28
**Last Updated**: 2026-08-01
**Status**: Active — authored at RFC 0049 PR 5; **live execution is a v0.3.12 release-prep deliverable** (run against a real provider per [v0.3.12-plan §Acceptance](../v0.3.12-plan.md#acceptance-for-v0312)). **v1.1 adds Legs 1b/2b (the person half)**.

---

## Overview

**Purpose**: Verify the v0.3.12 headline promise live — **tell a persona something in one room and it knows it in every room it belongs to, without leaking what it learned in a confidential room**. This is [RFC 0049's worked-example scenario 2](../rfcs/0049-memory-consolidation-gradient.md#worked-example-the-two-test-scenarios), end-to-end on a real provider: a project fact taught in a DM ("Atlas ships Friday") is captured as `topic.*` knowledge at interaction close, recalled in a group channel the persona belongs to via the live L2 cross-room widening, and still recalled on the DM re-ask afterwards. A fresh-epoch leg confirms the walls that stay absolute.

**The promise has two halves.** *What* the persona knows rides the facts tier (Legs 1–3); ***who*** *it is talking to* rides a different one — the RFC 0031 F-7 person identity on the relationship row (Legs 1b/2b). They fail independently: v1.0 tested only the topic half, passed live at the v0.3.12 candidate, and [ISSUE-0119](../issues/ISSUE-0119-channel-publish-drops-human-participant-type.md) rode that green run — every human publishing into a group channel arrived untyped, so the persona greeted in the standup the person it had just met in the DM.

**Scope**: the three RFC 0049 Phase-1 amendments in their shipped **live** posture — [0026 topic predicates](../rfcs/0026-amendment-topic-subject-predicates.md) (the capture path), [0031 fact-scope](../rfcs/0031-amendment-fact-scope-by-consolidation-level.md) (L2 facts cross rooms, `memory.facts.cross_room: live`), [0049-L1](../rfcs/0049-amendment-l1-cross-room-availability.md) (episodic room-first ranking, `memory.episodic.cross_room: live`) — with every cross-room candidate passing the [RFC 0037 §D gate](../rfcs/0037-memory-confidentiality-channel-classification.md#d-the-hard-gate-at-memory-injection). The DM and the group channel are distinct rooms (sessions auto-mint per `(agent, channel)` — [sessions guide §3](../guides/sessions.md#3-the-per-request-auto-binding)), so the recall in Leg 2 is structurally cross-room.

Legs 1b/2b add the **person** tier: the [RFC 0031 F-7 identity read](../issues/ISSUE-0093-person-identity-cross-room-tier.md), which omits the §D session filter so identity stated in one room surfaces in every room for the same `(principal, epoch)`. It keys on `(participant_id, participant_type)` — that type axis is what makes it a *different* failure mode from the facts tier, which keys on the subject string alone.

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
- [ISSUE-0093](../issues/ISSUE-0093-person-identity-cross-room-tier.md) — the person-identity tier Legs 1b/2b exercise; [ISSUE-0119](../issues/ISSUE-0119-channel-publish-drops-human-participant-type.md) — the participant-type gap Leg 2b catches; [ISSUE-0120](../issues/ISSUE-0120-backfill-split-participant-type-relationship-rows.md) — the split rows its migration folds.
- [Sessions guide](../guides/sessions.md) — session = room continuity; [epochs guide](../guides/epochs.md) — the run-isolation axis Leg 4 exercises.
- [MT-MEMORY-005 — the dementia test](MT-MEMORY-005-dementia-test.md) — the single-room continuity gate this MT extends to the multi-room case (its V5 no-bleed leg is re-anchored to the **epoch** axis by §V6 — Leg 4 here asserts the same wall).

**Related Automated Tests** — the deterministic CI backbone of this MT:

- [`tests/integration/test_cross_room_seed_replay.py`](../../tests/integration/test_cross_room_seed_replay.py) — replays `EVAL-MEMORY-002` (shadow-pinned) + `EVAL-MEMORY-003` (live, the room-axis integration eval) and re-runs the promotion verdict on every CI run.
- [`tests/unit/python/test_cross_room_live.py`](../../tests/unit/python/test_cross_room_live.py) — the live-default injection path: facts `sessions="*"`, episodic ranked + reinforcing, no *ungated* widening.
- [`tests/integration/test_confidentiality_gate.py`](../../tests/integration/test_confidentiality_gate.py) — the §D gate end-to-end (learn-restricted → act-public withheld).
- [`internal/server/channel_participant_type_test.go`](../../internal/server/channel_participant_type_test.go) — Legs 1b/2b's wiring half at the wire (REST → router → dispatcher → a real gRPC receiver): a human sender arrives typed `user`. This MT is the qualitative half — does the persona *use* what arrives.

This live MT confirms the *operator-observable* behaviour on a real provider; the widening/gate invariants themselves are pinned in CI.

---

## Preconditions

1. The compose stack is up against a **real provider** (`make demo-anthropic` or equivalent — *not* the mock provider; capture quality and natural-phrasing recall need real replies).
2. A **clean store** (`make reset`, or a fresh `PERSATRIX_EPOCH` for the whole arc) so prior facts do not steer the run.
3. `agent-ember-owl` is up and is a member of `group:planning` (the bundled roster has it at `respond: addressed` — the Leg 2 trigger @-mentions it).
4. **The operator identity is a member of `group:planning`** — the publish path refuses a non-member sender (`403 sender is not a member of the channel`); the DM legs need nothing (a DM materialises its own membership). Join at runtime (`persatrix channel join planning --as alex`) — and tear the stack down (`make reset`) rather than restarting the orchestrator afterwards: a config-declared channel with runtime-divergent membership fails the strict reconcile at the next boot.
5. `persatrix` CLI on `PATH` pointed at the running orchestrator.
6. Defaults unchanged: `memory.facts.cross_room` and `memory.episodic.cross_room` both resolve `live` (the shipped default — verify no overlay pins `shadow`/`off`), DMs stamp `internal` (`dm_default_classification` absent), `group:planning` is `internal` (the bundled declaration).
7. Optional but recommended: `PERSATRIX_MEMORY_PROVENANCE=1` on the persona container, so a leg fail can be split into a **recall miss** (fact absent from the admitted `facts` slice) vs. a **reasoning miss** (admitted but ignored) — the MQ-11 discipline [MT-MEMORY-005 §Telemetry](MT-MEMORY-005-dementia-test.md#telemetry-required-for-diagnosis) established.

---

## Test Procedure

The arc is **teach + introduce (DM) → close → ask + be recognised (group) → re-ask (DM) → fresh-epoch wall**. Timing matters once: fact extraction runs at **interaction close** ([RFC 0020](../rfcs/0020-interaction-lifecycle.md)), and an idle DM interaction closes on the *next* event after the idle gap (default 600 s) — so Leg 1 ends with an explicit bridge turn that triggers the close before Leg 2 asks. Legs 1b/2b ride *inside* that timing rather than extending it (identity is written mid-turn, not at close).

### Leg 1 — Teach the fact in the DM

Open the DM (chat rides the channels wire model as `dm:<a>:<b>` — the chat-as-DM amendment) and teach one topic fact embedded in natural conversation:

```bash
persatrix chat ember-owl --user alex
```

> "Quick heads-up before I forget — **Atlas ships Friday**. The exec demo is that same afternoon."

**Expected**: a natural acknowledgement; no memory call-out required.

Now **close the teaching interaction** — **send [Leg 1b](#leg-1b--introduce-yourself-in-the-same-dm-no-close-required)'s turn first**, then leave the DM idle **≥ 11 minutes** and send a low-content bridge turn in the same DM:

> "Thanks — talk later."

The bridge turn trips the idle close of the teaching interaction (close runs before that turn's injection), and the close-path extractor writes the topic fact — expected shape `(atlas, topic.has_deadline | topic.has_status, …)`, stamped `internal` (inherited from the DM's classification, RFC 0037 §C).

**Optional verification** (debug, not a pass criterion): on the persona container,

```bash
docker compose exec -T agent-ember-owl python3 -c "
import sqlite3
for r in sqlite3.connect('/app/data/memory.db').execute(
        \"SELECT subject, predicate, object, protection_level, session_id FROM facts WHERE predicate LIKE 'topic.%'\"):
    print(' | '.join(str(x) for x in r))"
```

(The agent image ships no `sqlite3` CLI — the runtime's `python3` with the stdlib `sqlite3` module is the query surface.)

→ at least one `atlas` row with a `topic.*` predicate, `protection_level = internal`, and the **DM room's** session id.

### Leg 1b — Introduce yourself in the same DM (no close required)

**Send this as Leg 1's *second* turn — right after the teach turn and *before* the idle gap.** It is numbered separately for diagnosis, not sequenced after Leg 1's close. State who you are in ordinary conversation:

> "By the way, I don't think we've been introduced properly — I'm Maksim, I run releases here."

**Expected**: a natural acknowledgement.

**No idle window is needed** — a property, not a shortcut: identity does not wait for interaction close the way facts do. The write-through fires at the `store_note(contact:<id>)` tool boundary *during* the turn (the F-7 immediacy criterion).

**Optional verification** (debug, not a pass criterion): on the persona container,

```bash
docker compose exec -T agent-ember-owl python3 -c "
import sqlite3
for r in sqlite3.connect('/app/data/memory.db').execute(
        'SELECT other_participant_id, other_participant_type, identity FROM relationships WHERE identity IS NOT NULL'):
    print(' | '.join(str(x) for x in r))"
```

→ an `alex` row typed **`user`** with a JSON identity carrying the name. A row typed `agent` *here* is an [ISSUE-0068](../issues/ISSUE-0068-chat-peer-recorded-as-agent-participant-type.md) regression of the **DM** stamp (ISSUE-0119 is the channel path, Leg 2b's) and means Leg 2b will fail.

### Leg 2 — Ask in the standup (the headline: cross-room recall)

In the group channel — a **different room**, hence a different auto-minted session — ask about the topic **by name, without the fact's content** (no "Friday", no "ship date"):

```bash
persatrix channel send planning "What's the latest on Atlas — anything the team should plan around this week?" \
    --as alex --mention ember-owl
```

**Pass criterion**: ember-owl's reply surfaces the Friday ship date (and ideally the exec demo) — knowledge it was never told in this room. Under the hood: the inbound turn names `atlas` → deterministic topic seeding (word-boundary match, no LLM) → the live L2 widened recall (`sessions="*"`) finds the DM-taught fact → the §D gate admits it (`internal` fact, `internal` acting channel).

**Fail criterion**: the persona asks what Atlas is, gives a generic status answer, or invents a date. With provenance on, split the fail: fact absent from the admitted `facts` slice = recall miss (investigate capture/seeding); present but unused = reasoning miss.

> **The trigger must name the topic.** Topic seeding is deterministic subject matching — `atlas` (or a multi-word subject verbatim) has to appear in the trigger text. This is by design, and it is *not* the keyword-overlap foul of [MT-MEMORY-005](MT-MEMORY-005-dementia-test.md): the no-overlap rule protects the fact's **content** (the object — "ships Friday"), which the trigger here still never contains. A trigger that names neither the topic nor the counterparty exercises nothing.

### Leg 2b — Be recognised in the standup (the person half)

In `group:planning`, ask something whose answer requires knowing **who you are**, without stating it:

```bash
persatrix channel send planning "Morning — anything here that needs me specifically?" \
    --as alex --mention ember-owl
```

**Pass criterion**: the reply places you as the person it met in the DM — by name, or by the role you gave ("you run releases", "as release owner"). It knows who it is talking to in a room where it was never told. The ask presupposes no prior commitment, so a persona that does not know you can only answer generically.

**Fail criterion**: the persona treats you as unknown — asks who you are, answers in the abstract, or addresses you only by the bare `alex` id. **The raw sender id is a FAIL**, not a partial pass: it rides the wire regardless and proves no memory was consulted.

> **The trigger must name neither you nor the content** — the identity twin of Leg 2's discipline. Identity seeds from the **sender**, not the text, so a trigger naming nothing still exercises it. That is why this leg catches what Leg 2 cannot.

**Diagnosis on fail** — two modes, different places:

1. **Wrong participant type (the ISSUE-0119 class).** The persona read an agent-typed row and found nothing. Check the delivered event's `sender_participant_type` (must be `user`) and Leg 1b's row type. A **release-blocking regression** of the [#799](https://github.com/mkhomutov/Persatrix/pull/799) fix, not a quality miss.
2. **Reasoning miss.** With `PERSATRIX_MEMORY_PROVENANCE=1` the identity line is present in the injected `relationship_context` and the model ignored it. A quality finding.

Two `alex` rows in Leg 1b's query means a store predating ISSUE-0120's migration v17 — re-run on a `make reset` stack before filing.

### Leg 3 — Re-ask in the DM

Back in the original DM:

> "Remind me — what's our Atlas timeline?"

**Pass criterion**: the Friday date again (same-room recall, plus the fact survived the group-channel turn untouched). This is the "re-learn in DMs" half of scenario 2.

### Leg 4 — The wall that stays: a fresh epoch inherits nothing

Repeat the Leg 2 ask under a **fresh epoch** (run isolation — the axis that stays a hard wall, with `principal`) — but **not in `group:planning`**: Leg 2's reply put the Friday date into that channel's transcript, and the RFC 0034 conversation window is recent-N channel history, epoch-agnostic by design (the transcript is room-visible content, not memory) — so an in-channel probe can never observe absence. Probe on a **fresh channel** with an empty transcript instead (still a different room from the DM, so the recall is still structurally cross-room):

```bash
persatrix channel create epoch-probe --member ember-owl:addressed --member alex:addressed
persatrix channel send epoch-probe "What's the latest on Atlas?" \
    --as alex --mention ember-owl --epoch mt-crossroom-fresh
```

**Pass criterion**: **no** reference to Friday or the exec demo — absence is the promise. Cross-room widening ranges over *rooms*, never across epochs or tenants; a recall here reproduces the F-3 class of leak and is release-blocking. Diagnose a fail with provenance before filing: zero `tier_admitted` emissions on the failing turn means the *injection-path* wall held and the recall arrived through an agent-initiated **memory-tool call** — the executor-task scope gap tracked as [ISSUE-0118](../issues/ISSUE-0118-tool-recall-bypasses-epoch-session-scopes.md) (found by this leg's first live run, 2026-07-30), not a widening regression.

---

## Expected Results Summary

| Leg | Room | Trigger discipline | Pass criterion | Pass/Fail |
|-----|------|--------------------|----------------|-----------|
| 1 — Teach + close | DM | natural statement; ≥ 11 min idle + bridge turn | ack; (optional) `topic.*` fact row stamped `internal` with the DM session id | ☐ |
| 1b — Introduce yourself | DM | natural statement; **no idle window needed** | ack; (optional) an `alex` relationship row typed **`user`** carrying the identity JSON | ☐ |
| 2 — Standup ask | `group:planning` | names `atlas`, never the content | reply surfaces the Friday ship date, cross-room | ☐ |
| 2b — Be recognised | `group:planning` | names **neither** you nor the content | reply addresses you by the name/role given in the DM; the bare `alex` id is a fail | ☐ |
| 3 — DM re-ask | DM | names `atlas`, never the content | Friday recalled again in the original room | ☐ |
| 4 — Fresh epoch | `group:planning`, `--epoch` override | same ask, fresh epoch | **no** recall — the epoch wall holds | ☐ |

**Overall pass**: Legs 2, 2b, 3, and 4 all pass. A Leg 2/3 fail is diagnosed with provenance (recall vs. reasoning miss) before filing. A Leg 2b fail is triaged **wiring vs. reasoning first** (see its diagnosis note): a wrong participant type is a release-blocking regression of the ISSUE-0119 fix, while an admitted-but-unused identity line is a quality finding. A Leg 4 fail is a confidentiality/isolation regression — file immediately, release-blocking.

---

## Edge Cases & Error Scenarios

### Edge Case 1: the trigger never names the topic

**Scenario**: Leg 2 is phrased "anything shipping soon?" — no `atlas` token.

**Expected Behavior**: no topic seed derives, so the fact is unlikely to load; person seeds (sender/self) still apply. Not a failure — rewrite the trigger to name the topic. (Paraphrase-recall of *topics* is the MQ-8 / RFC 0024 vector territory, out of scope here.)

### Edge Case 2: the teaching interaction never closed

**Scenario**: Leg 2 runs a few minutes after Leg 1 with no idle gap or bridge turn.

**Expected Behavior**: the extractor has not run yet, so the group-channel ask recall-misses. Test invalid — redo the Leg 1 idle + bridge, confirm the close in the logs, re-ask.

### Edge Case 3: no identity row keyed on your sender id

**Scenario**: Leg 2b fails and Leg 1b's query shows no identity row for `alex` — nothing at all, or a row keyed on a name (`contact:maksim`).

**Expected Behavior**: not a wiring failure and not a Leg 2b fail — the capture missed. Identity lands only if the persona *elects* to call `store_note(contact:<id>)`, and lands on the id that topic names: the key is the *topic*, only the type is the sender's, so a name-keyed note is invisible to Leg 2b. Model behaviour, unlike the deterministic close-path fact pass — the person legs prove recall, not capture. Re-run Leg 1b with a fuller introduction (a name **and** a role parse better) and confirm the row before reading anything into Leg 2b. If it never lands on `alex`, file against the capture path, not cross-room recall.

### Edge Case 4: rollback lever

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
- **Session hygiene**: no `PERSATRIX_SESSION_ID` pinning here, unlike MT-MEMORY-005 — the whole point is the per-`(agent, channel)` auto-binding minting *different* rooms for the DM and the group channel. Pinning one session across both would make Legs 2/2b same-room and vacuous.
- **Why the person half gets its own legs** rather than an extra assertion on Leg 2: different tiers, keys, and timing — facts key on the subject string and are written at close; identity keys on `(participant_id, participant_type)` and is written mid-turn. One leg cannot fail informatively for both, and empirically did not. The cost is two extra turns on an arc that already pays for the stack and the idle window.
- **Classification posture**: both rooms sit at `internal` in this MT, so the §D gate admits silently. The gate's *withhold* half (teach in a `restricted` room, ask in an `internal` one) is `MT-PERSONA-CONFIDENTIALITY-001` territory — do not bolt it onto this arc; the two MTs are complementary halves of the headline sentence.
- The shadow→live promotion evidence (criteria, recorded numbers, deferred decisions) lives in the [fact-scope amendment's Promotion section](../rfcs/0031-amendment-fact-scope-by-consolidation-level.md#promotion-v0312-pr-4--the-measurement-gated-flip) and the [L1 amendment's](../rfcs/0049-amendment-l1-cross-room-availability.md#promotion-v0312-pr-4--the-measurement-gated-flip).
