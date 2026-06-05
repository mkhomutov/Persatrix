# Manual Test MT-PERSONA-008: A Person Introduced in One Channel Is Recalled in Another (Cross-Room Person Memory)

**Test ID**: `MT-PERSONA-008`
**Feature Area**: Persona
**Version**: 1.0
**Created**: 2026-06-05
**Last Updated**: 2026-06-05
**Status**: Active

---

> **Origin**: A persona told a person's name in channel A (it called
> `store_note` under topic `contact:<id>`, confirmed in the store) said *"I
> don't have any notes about your name"* in a fresh channel B —
> person-identity notes inherited the room-scoped recall default. Tracked
> as finding **F-3b** in the [v0.3.7 conversation test-findings PR plan §PR 5](../v0.3.7-test-findings-pr-plan.md);
> resolved by the [RFC 0031 person-keyed note-recall amendment](../rfcs/0031-amendment-person-keyed-note-recall.md).

---

## Overview

**Purpose**: Verify that a persona recalls a person it learned about in a
different channel — identity (name, role, stable preferences) crosses
rooms — while non-identity (room) notes stay scoped to their channel.

**Scope**: `recall_contact_notes` (cross-session, principal/epoch-scoped,
topic-exact) and its wiring into `_inject_memory_context`.

**Out of Scope**: Cross-tenant or cross-epoch recall (must **not** cross —
see Step 3); re-homing identity onto the RFC 0026 facts tier (follow-up).

---

## Related Documentation

- [RFC 0031 person-keyed note-recall amendment](../rfcs/0031-amendment-person-keyed-note-recall.md)
- [docs/memory-scope-axes.md](../memory-scope-axes.md) — session = room vs. cross-room person axis.
- [docs/v0.3.7-test-findings-pr-plan.md §PR 5](../v0.3.7-test-findings-pr-plan.md) — F-3b.

**Related Automated Tests**:
- [`tests/unit/python/test_contact_note_cross_room_recall.py`](../../tests/unit/python/test_contact_note_cross_room_recall.py) — cross-room recall, topic-exactness, preserved room isolation, epoch scoping, and the injection wiring.

---

## Preconditions

- ☐ Persona with memory permissions in [`config/agents.yaml`](../../config/agents.yaml) (e.g. `ember-owl`).
- ☐ Provider key for the live steps.
- ☐ **Rebuild the agent image** after the change: `docker compose up -d --build` (prompts/code are baked into the image).
- ☐ Same OS user / `PERSATRIX_PRINCIPAL_ID` / `PERSATRIX_EPOCH` across the two channels (cross-room is *not* cross-tenant or cross-epoch).

---

## Test Procedure

### Step 1: Deterministic Gate

**Action**:

```bash
.venv/bin/python -m pytest tests/unit/python/test_contact_note_cross_room_recall.py -v
```

**Expected Result**: All pass — cross-room recall, topic-exact, room
isolation preserved, epoch-scoped, and the injection test
(`test_sender_contact_note_injected_despite_query_miss`).

---

### Step 2: Live — Introduce in Channel A, Recall in Channel B

**Action**: In channel A, introduce yourself; then ask in a **fresh**
channel B (same user identity):

```bash
bin/persatrix channel send group:room-a "Hi, I'm Max and my favorite language is Rust. Please remember that." --as local
# (wait for the persona to acknowledge / store)
bin/persatrix channel send group:room-b "Based on what you know about me, what's my favorite language?" --as local
bin/persatrix channel history group:room-b --limit 2
```

**Expected Result**: In channel B the persona recalls **Rust** (and may
greet you by name) — it knows who you are despite never having spoken to
you in this channel before. The recall works even though the channel-B
question shares no wording with the stored note.

**Verification**:
- [ ] Channel B reply correctly recalls the cross-room fact (Rust / name)
- [ ] No "I don't have any notes about you" denial

---

### Step 3: Live — Room Notes Do NOT Leak (Belt-and-Braces)

**Action**: In channel A, have the persona note a **room-specific** fact
(not about a person), e.g. "for this channel, our standup is at 10am".
Then in channel B ask about it.

**Expected Result**: The room note does **not** cross — channel B does not
surface "standup at 10am". Only person-identity (`contact:*`) notes cross
rooms; general notes stay room-scoped.

**Verification**:
- [ ] Room-specific note from channel A is not recalled in channel B

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Cross-room recall unit tests pass | ☐ |
| 2 | Person fact introduced in A is recalled in B | ☐ |
| 3 | Room-specific note from A does not leak into B | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Different Epoch / Principal

**Scenario**: Channel B runs under a different `PERSATRIX_EPOCH` (a fresh
run) or a different principal.

**Expected Behavior**: The person fact is **not** recalled — cross-room is
not cross-epoch or cross-tenant. This is the intended isolation, not a
regression (pinned by `test_contact_recall_respects_epoch`).

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-06-05 | _pending_ | _pending_ | _pending_ | Initial manual run alongside v0.3.7 test-findings PR plan §PR 5 merge. Supersedes MT-PERSONA-007 Step 3 (which captured the pre-fix "no notes" answer). |

---

## Notes

- This is the substantive half of F-3 (PR 5); MT-PERSONA-007 (PR 4) made
  the prompt *honest* about scope, and its Step 3 "empty recall admitted"
  expectation is superseded here once cross-room recall lands.
- Delivery is via **auto-injection** of the sender's contact note — the
  persona does not need to call `recall_notes` for cross-room identity; the
  fact is already in its context.
