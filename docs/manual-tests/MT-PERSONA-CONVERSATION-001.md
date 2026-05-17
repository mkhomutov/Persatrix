# Manual Test MT-PERSONA-CONVERSATION-001: Persona Conversational Continuity (DM)

**Test ID**: `MT-PERSONA-CONVERSATION-001`
**Feature Area**: Persona Runtime (RFC 0034 Phase 1 — Conversation Window, DM channels)
**Version**: 1.0
**Created**: 2026-05-16
**Last Updated**: 2026-05-17
**Status**: Active (promoted from Draft scaffold — passed both legs in the [v0.3.1 release-prep execution report](v0.3.1-execution-report.md); RFC 0034 Phase 1 landed)

---

## Overview

**Purpose**: Verify that a persona over a DM channel sees the in-progress
conversation as a transcript — it can recall its own previous question
and resolve a referential follow-up within one chat session. This is the
operator-facing acceptance walkthrough for
[RFC 0034](../rfcs/0034-persona-conversational-working-memory.md)
Phase 1, closing [ISSUE-0052](../issues/ISSUE-0052-persona-conversational-working-memory-gap.md).

Before RFC 0034 the persona's LLM call carried only the current message
in the `messages` array — no in-progress transcript — so within two
turns the persona treated every turn as the first turn. Phase 1
reconstructs the `messages` array from the channel store each turn
([RFC 0034 §A](../rfcs/0034-persona-conversational-working-memory.md#a-where-the-fix-lives)).

**Scope**:
- DM channel only — Phase 1 is DM-only ([RFC 0034 §Future Phases](../rfcs/0034-persona-conversational-working-memory.md)).
- Two legs over one chat session:
  - **Leg 1 — self-reference**: the persona poses a question; the next
    turn asks it to recall that question.
  - **Leg 2 — referential follow-up**: the persona asks a preference
    question; the user answers with a referential fragment (`"the
    second one"`); the persona resolves the referent.

**Out of Scope**:
- Group-channel transcript reconstruction (RFC 0034 Phase 2 — v0.3.x).
- Instrumentation / tuning of `max_turns` / `max_tokens` (RFC 0034
  Phase 3 — v0.3.x).
- Long-term cross-interaction recall — that is the
  [MT-MEMORY-005](MT-MEMORY-005-dementia-test.md) dementia-test surface
  (RFC 0026). This test exercises *within-conversation* continuity only.
- The `messages`-array *shape* contract is covered by the automated
  integration test `tests/integration/test_conversational_continuity.py`;
  this MT asserts the model's *prose* behaviour, which is not suitable
  for an automated assertion.

---

## Related Documentation

- [docs/rfcs/0034-persona-conversational-working-memory.md](../rfcs/0034-persona-conversational-working-memory.md)
  — canonical spec.
- [docs/rfcs/0034-pr-plan.md](../rfcs/0034-pr-plan.md) — PR sequence;
  this MT is the PR 3 deliverable, executed in v0.3.1-plan Phase 4 PR 1.
- [docs/issues/ISSUE-0052-persona-conversational-working-memory-gap.md](../issues/ISSUE-0052-persona-conversational-working-memory-gap.md)
  — operational driver.
- [tests/integration/test_conversational_continuity.py](../../tests/integration/test_conversational_continuity.py)
  — automated substrate test (`messages`-array shape).
- [MT-MEMORY-005 — Dementia Test](MT-MEMORY-005-dementia-test.md) — the
  paired long-term-memory acceptance surface; its referential follow-up
  legs depend on this conversation window being in place.

---

## Preconditions

Same baseline as
[MT-SESSION-001 § Preconditions](MT-SESSION-001.md#preconditions): a
local repo checkout with `make build` already run so `bin/persatrix.exe`
and a recent orchestrator binary are on disk, on a v0.3.1 (or newer)
build that includes RFC 0034 Phase 1.

This MT **requires** `ANTHROPIC_API_KEY` — the persona is LLM-backed and
the test asserts model behaviour.

The walkthrough writes to `data/channels.db` and `data/memory.db`. A
fresh persona is not required (the conversation window is per-channel,
not per-session), but a clean DM channel keeps the transcript short and
the legs unambiguous.

---

## Test Procedure

### Step 1: Start the stack

**Action**:

```pwsh
./bin/orchestrator.exe --env=development 2>&1 | Tee-Object orchestrator-mt-pc-001.log
# (in a second shell)
python -m persatrix_agents.server --agent ember-owl 2>&1 | Tee-Object persona-mt-pc-001.log
```

**Expected**:
- The orchestrator and persona-runtime start cleanly.
- The persona-runtime log shows the agent registering with the
  orchestrator.

**Verification**:
- [ ] Both processes are running; no startup errors in either log.

---

### Step 2: Open a chat session

**Action**:

```pwsh
./bin/persatrix.exe chat --agent ember-owl
```

Keep this interactive session open for the remaining steps — Leg 1 and
Leg 2 are consecutive turns in the **same** session, over the same DM
channel (`dm:ember-owl:<user>`).

**Expected**:
- The chat REPL opens and the persona greets or awaits input.

**Verification**:
- [ ] The REPL is interactive and accepts input.

---

### Step 3: Leg 1 — self-reference (turn 1: elicit a question)

**Action** — send:

> Ask me one question to get to know my preferences.

**Expected**:
- The persona replies with a single, clear question — for example
  *"What's your favourite season?"* or *"What do you like to do on
  weekends?"*. Note the exact question it asked.

**Verification**:
- [ ] The persona's reply contains a question directed at the user.

---

### Step 4: Leg 1 — self-reference (turn 2: recall the question)

**Action** — send, as the very next turn:

> What did you just ask me?

**Pass criterion**: the persona restates the question it asked in
Step 3 — verbatim or a faithful paraphrase (e.g. *"I asked what your
favourite season is."*).

**Fail criterion**: the persona says it has not asked anything, treats
this as the start of the conversation, asks *"asked about what?"*, or
invents a different question it did not pose. This is the exact
[ISSUE-0052](../issues/ISSUE-0052-persona-conversational-working-memory-gap.md)
symptom — *"this appears to be the start of our conversation"*.

**Verification**:
- [ ] The persona correctly recalls its own Step 3 question.

---

### Step 5: Leg 2 — referential follow-up (turn 3: a question with options)

**Action** — send:

> Give me two options for a focus area next quarter, then ask me which I prefer.

**Expected**:
- The persona offers two labelled options (e.g. *"1. reducing tech
  debt; 2. shipping the new onboarding flow — which do you prefer?"*)
  and asks which the user prefers. Note the two options.

**Verification**:
- [ ] The persona's reply lists two distinguishable options and asks
  the user to choose.

---

### Step 6: Leg 2 — referential follow-up (turn 4: answer with a fragment)

**Action** — send a reply that names the option **only by reference**,
never by its content:

> The second one.

**Pass criterion**: the persona resolves *"the second one"* to the
second option it offered in Step 5 and responds about that specific
option — without asking *"the second what?"* or re-listing the options.

**Fail criterion**: the persona cannot resolve the referent — it asks
for clarification, guesses the wrong option, or treats the fragment as
a new, contextless statement. This is the referential-follow-up symptom
from [ISSUE-0052 Impact](../issues/ISSUE-0052-persona-conversational-working-memory-gap.md#impact)
(the `"I like it"` class).

**Verification**:
- [ ] The persona resolves *"the second one"* to the correct Step 5
  option.

---

## Expected Results Summary

| Step | Leg | Expected Outcome | Pass/Fail |
|------|-----|------------------|-----------|
| 1 | — | Stack starts cleanly | ☐ |
| 2 | — | Chat session opens | ☐ |
| 3 | 1 | Persona poses a question | ☐ |
| 4 | 1 | Persona recalls its own prior question | ☐ |
| 5 | 2 | Persona offers two options and asks the user to choose | ☐ |
| 6 | 2 | Persona resolves the referential fragment to the right option | ☐ |

**Overall pass**: both Leg 1 (Step 4) and Leg 2 (Step 6) pass. A fail on
either leg is a fail — the conversation window is the load-bearing fix
for both symptom classes.

---

## Edge Cases & Error Scenarios

### Edge Case 1: LLM provider transient error during a trigger turn

**Scenario**: the LLM call for Step 4 or Step 6 fails or returns an
unrelated response.

**Expected Behavior**: re-run the trigger turn. If the failure is
reproducible, capture the trace and treat as inconclusive — not a
conversation-window failure.

### Edge Case 2: Conversation window disabled

**Scenario**: the persona's `conversation_window.enabled` is set to
`false` in `config/agents.yaml` (the operator escape hatch — [RFC 0034 §F](../rfcs/0034-persona-conversational-working-memory.md#f-caching-and-fetch-policy)).

**Expected Behavior**: the test reproduces the *pre-RFC-0034* failure —
Step 4 and Step 6 both fail. This is the intended diagnostic: it
confirms the legs actually exercise the conversation window and not some
other tier. Re-enable the block before recording a release result.

### Edge Case 3: Orchestrator history endpoint unreachable mid-session

**Scenario**: the orchestrator REST surface goes down between turns.

**Expected Behavior**: the persona degrades gracefully — the window
falls back to the current event alone ([RFC 0034 §F](../rfcs/0034-persona-conversational-working-memory.md#f-caching-and-fetch-policy)),
so the persona still replies but loses continuity until the endpoint
recovers. The persona-runtime log carries a WARN with
`reason=conversation_window_fetch_failed`. Not a test failure if the
orchestrator was deliberately stopped; a real outage is an operational
signal, not a memory bug.

---

## Test Results

| Date | Tester | OS | Build | Result | Notes |
|------|--------|----|-------|--------|-------|

---

## Notes

- Each leg's load-bearing constraint is that the trigger turn (Step 4,
  Step 6) carries **no restated content** — it refers back only. If the
  trigger turn repeats the question or names the option, the test
  exercises the current message, not the conversation window, and the
  result is meaningless.
- Phase 1 is DM-only. Repeating this walkthrough on a group channel is
  expected to fail until RFC 0034 Phase 2 ships — that is not a v0.3.1
  regression.
- Re-run this MT before any v0.3.x release that touches the persona
  action loop or the channel-history fetch path. Add a row to the Test
  Results table.
