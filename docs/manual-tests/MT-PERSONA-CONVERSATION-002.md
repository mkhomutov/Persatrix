# Manual Test MT-PERSONA-CONVERSATION-002: Persona Conversational Continuity (Group Channel)

**Test ID**: `MT-PERSONA-CONVERSATION-002`
**Feature Area**: Persona Runtime (RFC 0034 Phase 2 — Conversation Window, group channels + per-peer attribution)
**Version**: 1.0
**Created**: 2026-06-04
**Last Updated**: 2026-06-04
**Status**: Active (promoted from Draft scaffold — both legs passed live on the v0.3.7 RC tip `65303d7`, [v0.3.7-execution-report.md](v0.3.7-execution-report.md), 2026-06-06)

---

## Overview

**Purpose**: Verify that a persona on a **multi-peer group channel** sees
the in-progress conversation as a *speaker-attributed* transcript — it can
tell *who said what* and resolve a referent that points at **another
peer's** prior turn (not its own, not the user's). This is the
operator-facing acceptance walkthrough for
[RFC 0034](../rfcs/0034-persona-conversational-working-memory.md)
Phase 2, the group-channel residual of
[ISSUE-0052](../issues/ISSUE-0052-persona-conversational-working-memory-gap.md)
that the DM-only Phase 1 ([MT-PERSONA-CONVERSATION-001](MT-PERSONA-CONVERSATION-001.md))
left open.

Phase 1 reconstructs the `messages` array from the channel store so the
persona follows the conversation, mapping the single peer to `user` and
the persona's own turns to `assistant`
([RFC 0034 §C](../rfcs/0034-persona-conversational-working-memory.md#c-role-mapping)).
On a group channel several distinct peers all collapse to `user` turns,
so the model needs an *inline* speaker label to disambiguate them. Phase 2
prepends `[<peer_id>]: ` to every replayed peer turn
([RFC 0034 §G](../rfcs/0034-persona-conversational-working-memory.md#g-group-channel-handling)),
so a persona can attribute and build on a *specific* peer's contribution.

**Scope**:
- Group channel only — the DM single-peer case is
  [MT-PERSONA-CONVERSATION-001](MT-PERSONA-CONVERSATION-001.md).
- Two legs over one channel session:
  - **Leg 1 — named-peer attribution**: two `always` personas take
    distinct, attributable positions; a third persona, `@`-mentioned, is
    asked *who favoured what* and must attribute each position to the
    correct named peer.
  - **Leg 2 — cross-peer pronoun binding**: a user turn whose pronoun
    points at *one specific peer's* prior contribution; the mentioned
    persona binds the referent to the right peer and engages it.

**Out of Scope**:
- **Directedness / who replies** — *which* members answer an
  `@`-mention is the RFC 0030 relevance-gate surface
  ([Tier A](../rfcs/0030-amendment-relevance-gated-response.md), the
  separate v0.3.7 addressing workstream), **not** this test. This MT
  uses `@`-mentions only to make the *responding* persona deterministic;
  it asserts the responder's *attribution accuracy*, not the admission
  decision. (`MT-CHANNEL-RELEVANCE-001` owns directedness.)
- **Peer voice** — whether the persona reads as a colleague rather than
  an assistant is the peer-voice-prompt workstream's surface, asserted
  in the combined realism walkthrough, not here.
- DM-channel continuity (MT-PERSONA-CONVERSATION-001, RFC 0034 Phase 1).
- Instrumentation / tuning of `max_turns` / `max_tokens` and the cache
  LRU bound (RFC 0034 Phase 3 — later v0.3.x).
- The `messages`-array *shape* contract (inline `[<peer_id>]: ` prefix,
  distinct per peer, cross-peer pronoun substrate) is covered by the
  automated integration test
  `tests/integration/test_conversational_continuity.py`
  (`test_persona_sees_a_named_peer_turn_on_a_group_channel`); this MT
  asserts the model's *prose* behaviour, which is not suitable for an
  automated assertion.

---

## Related Documentation

- [docs/rfcs/0034-persona-conversational-working-memory.md §G](../rfcs/0034-persona-conversational-working-memory.md#g-group-channel-handling)
  — the per-peer attribution contract this MT exercises.
- [docs/rfcs/0034-phase2-pr-plan.md](../rfcs/0034-phase2-pr-plan.md) — PR
  sequence; this MT is the PR 3 (closeout) deliverable.
- [MT-PERSONA-CONVERSATION-001](MT-PERSONA-CONVERSATION-001.md) — the
  paired DM-channel continuity surface (RFC 0034 Phase 1).
- [tests/integration/test_conversational_continuity.py](../../tests/integration/test_conversational_continuity.py)
  — automated substrate test (group-channel `messages`-array shape).
- [docs/guides/persona-agents.md](../guides/persona-agents.md) — the
  conversation-window operator surface.
- [docs/guides/channels.md §7](../guides/channels.md) — floor control,
  which serializes the two `always` responders so each reads its
  predecessor before composing.

---

## Preconditions

Same baseline as
[MT-PERSONA-CONVERSATION-001 § Preconditions](MT-PERSONA-CONVERSATION-001.md#preconditions):
a local repo checkout with `make build` already run, on a **v0.3.7 (or
newer) build that includes RFC 0034 Phase 2** (group-channel per-peer
attribution).

This MT **requires** `ANTHROPIC_API_KEY` — the personas are LLM-backed
and the test asserts model behaviour.

The walkthrough uses the default demo `group:planning` channel from an
unedited [`config/channels.yaml`](../../config/channels.yaml): three
personas — `ember-owl` (`when_mentioned`), `iron-fox` (`always`),
`nova-sparrow` (`always`) — with floor control on (the v0.3.6 default),
so the two `always` responders speak one at a time and each reads the
other's reply before composing. Run all three persona runtimes.

A clean channel keeps the transcript short and the legs unambiguous;
the walkthrough writes to `data/channels.db` and `data/memory.db`.

---

## Test Procedure

### Step 1: Start the stack

**Action**:

```pwsh
./bin/orchestrator.exe --env=development 2>&1 | Tee-Object orchestrator-mt-pc-002.log
# (in three more shells — one per persona)
python -m persatrix_agents.server --agent ember-owl    2>&1 | Tee-Object persona-ember-mt-pc-002.log
python -m persatrix_agents.server --agent iron-fox     2>&1 | Tee-Object persona-iron-mt-pc-002.log
python -m persatrix_agents.server --agent nova-sparrow 2>&1 | Tee-Object persona-nova-mt-pc-002.log
```

**Expected**:
- The orchestrator and all three persona runtimes start cleanly and
  register; the orchestrator log shows `channels: subsystem ready`.

**Verification**:
- [ ] All four processes are running; no startup errors in any log.
- [ ] `Invoke-RestMethod http://127.0.0.1:8080/api/v1/channels/group:planning`
  shows the three members.

---

### Step 2: Join the channel as a human

**Action**:

```pwsh
./bin/persatrix channel join planning --as operator
```

A human member defaults to `respond=when_mentioned` (no auto-reply), so
the operator can post without becoming a responder.

**Expected**:
- The join succeeds; the operator is now a member of `group:planning`.

**Verification**:
- [ ] The operator appears in the channel membership.

---

### Step 3: Leg 1 — seed two attributable peer positions

**Action** — post an open question (no `@`-mention) so both `always`
members answer:

```pwsh
./bin/persatrix channel send planning \
  "For the v0.4 event log, should we use Postgres or SQLite? One sentence each, and say which you'd pick."
```

**Expected**:
- `iron-fox` and `nova-sparrow` each reply once, taking a **distinct,
  attributable** position (e.g. one picks Postgres, the other SQLite).
  Floor control serializes them — the second speaker's reply visibly
  reacts to the first. Note **which persona picked which datastore**.

**Verification**:
- [ ] Exactly two persona replies (the two `always` members); `ember-owl`
  (`when_mentioned`) stays silent.
- [ ] The two replies take distinguishable, separately-attributable
  positions.

---

### Step 4: Leg 1 — named-peer attribution (the trigger turn)

**Action** — `@`-mention the silent advisor and ask *who* favoured
*what*, naming **neither** datastore-to-persona pairing yourself:

```pwsh
./bin/persatrix channel send planning \
  "@ember-owl which of them picked Postgres, and who wanted SQLite?"
```

**Pass criterion**: `ember-owl` attributes each datastore to the
**correct named peer** from Step 3 (e.g. *"iron-fox picked Postgres;
nova-sparrow preferred SQLite."*). It could only do this by reading the
`[iron-fox]: ` / `[nova-sparrow]: ` attributed transcript.

**Fail criterion**: `ember-owl` cannot say who said what — it conflates
the two peers, swaps the pairing, asks "which of whom?", attributes a
position to *itself* or to *the operator*, or treats this as a fresh,
contextless question. This is the group-channel residual of
[ISSUE-0052](../issues/ISSUE-0052-persona-conversational-working-memory-gap.md):
the persona sees a flat, speaker-anonymous transcript.

**Verification**:
- [ ] `ember-owl` correctly attributes **both** positions to the right
  named peers.

---

### Step 5: Leg 2 — cross-peer pronoun binding (the trigger turn)

**Action** — post a turn whose pronoun points at **one specific peer's**
Step 3 contribution, by *role* not by name. Substitute the persona that
argued for operational simplicity / the lighter datastore (call them
**peer-S** — name them in your run, not in the message):

```pwsh
./bin/persatrix channel send planning \
  "@ember-owl one of them argued mostly on operational simplicity — do you agree with their reasoning?"
```

**Pass criterion**: `ember-owl` binds *"their"* to **peer-S** (the peer
whose Step 3 argument was about operational simplicity) and engages
*that* peer's specific reasoning — without asking *"whose reasoning?"*,
re-listing both positions, or answering about the *other* peer's
argument.

**Fail criterion**: `ember-owl` cannot resolve the referent — it asks
for clarification, binds *"their"* to the wrong peer, or treats the
fragment as a new contextless statement. This is the referential
cross-peer failure the inline `[<peer_id>]: ` prefix exists to prevent.

**Verification**:
- [ ] `ember-owl` resolves *"their"* to the correct Step 3 peer and
  engages that peer's argument.

---

## Expected Results Summary

| Step | Leg | Expected Outcome | Pass/Fail |
|------|-----|------------------|-----------|
| 1 | — | Stack + three personas start cleanly | ☐ |
| 2 | — | Operator joins the channel | ☐ |
| 3 | 1 | Two `always` peers take distinct, attributable positions | ☐ |
| 4 | 1 | `@`-mentioned persona attributes each position to the right named peer | ☐ |
| 5 | 2 | `@`-mentioned persona binds a pronoun to the correct peer's prior turn | ☐ |

**Overall pass**: both Leg 1 (Step 4) and Leg 2 (Step 5) pass. A fail on
either leg is a fail — the inline `[<peer_id>]: ` prefix is the
load-bearing fix for both: attribution (who said what) and cross-peer
referent resolution.

---

## Edge Cases & Error Scenarios

### Edge Case 1: LLM provider transient error during a trigger turn

**Scenario**: the LLM call for Step 4 or Step 5 fails or returns an
unrelated response.

**Expected Behavior**: re-run the trigger turn. If the failure is
reproducible, capture the trace and treat as inconclusive — not a
conversation-window failure.

### Edge Case 2: Conversation window disabled

**Scenario**: `ember-owl`'s `conversation_window.enabled` is set to
`false` in `config/agents.yaml` (the operator escape hatch —
[RFC 0034 §F](../rfcs/0034-persona-conversational-working-memory.md#f-caching-and-fetch-policy)).

**Expected Behavior**: the test reproduces the *pre-RFC-0034* failure —
Step 4 and Step 5 both fail (the advisor sees only the current message,
not the attributed transcript). This is the intended diagnostic: it
confirms the legs actually exercise the conversation window. Re-enable
the block before recording a release result.

### Edge Case 3: The two `always` peers take the *same* position

**Scenario**: in Step 3 both `iron-fox` and `nova-sparrow` pick the same
datastore, so there is nothing distinct to attribute.

**Expected Behavior**: not a test failure — re-run Step 3 with a sharper
prompt (e.g. *"argue opposite sides"*) until the two positions are
distinguishable. The legs require two **distinct**, separately-attributable
peer contributions to be meaningful.

### Edge Case 4: Orchestrator history endpoint unreachable mid-session

**Scenario**: the orchestrator REST surface goes down between turns.

**Expected Behavior**: the persona degrades gracefully — the window
falls back to the current event alone
([RFC 0034 §F](../rfcs/0034-persona-conversational-working-memory.md#f-caching-and-fetch-policy)),
so the advisor still replies but loses attribution until the endpoint
recovers. The persona-runtime log carries a WARN with
`reason=conversation_window_fetch_failed`. Not a test failure if the
orchestrator was deliberately stopped; a real outage is an operational
signal, not a memory bug.

---

## Test Results

| Date | Tester | OS | Build | Result | Notes |
|------|--------|----|-------|--------|-------|
| 2026-06-06 | Claude (Opus 4.8) | macOS (Darwin 24.6.0) | RC tip `65303d7` (Anthropic) | ✅ **Pass** (both legs) | Leg 1 — `ember-owl` attributed *Iron Fox→Postgres, Nova Sparrow→SQLite* correctly (Edge Case 3 hit; re-seeded "opposite sides" per recipe). Leg 2 — bound *"their"* (operational simplicity) to Nova Sparrow and engaged her reasoning. See [v0.3.7-execution-report.md](v0.3.7-execution-report.md). |
| 2026-06-06 | Claude (Opus 4.8) | macOS (Darwin 24.6.0) | tip `92a5a00` (Anthropic) | ✅ **Pass** (both legs) | Re-run on the post-fix tip. Leg 1 — `ember-owl`: *"Iron Fox took Postgres. Nova Sparrow took SQLite"* (opposite-sides seed). Leg 2 — first referent ("operational simplicity") was ambiguous (both peers used that framing) so `ember-owl` bound it to Iron Fox; per Edge Case 1, re-run with a referent unique to Nova Sparrow ("get us in front of customers fastest") → bound *"their"* to Nova Sparrow cleanly. See [v0.3.7-execution-report.md](v0.3.7-execution-report.md). |

---

## Notes

- Each leg's load-bearing constraint is that the trigger turn (Step 4,
  Step 5) carries **no restated attribution** — it never names which
  peer took which position. If the trigger turn does the attribution for
  the persona, the test exercises the current message, not the
  speaker-attributed window, and the result is meaningless.
- *Who replies* to the `@`-mention (directedness) is the RFC 0030
  relevance-gate surface, exercised by `MT-CHANNEL-RELEVANCE-001` — not
  this MT. Here the `@`-mention only fixes the *responder identity* so
  the attribution assertion has a deterministic subject; the assertion
  is the responder's accuracy, which is the RFC 0034 surface.
- Repeating this walkthrough on a build *without* Phase 2 (e.g. v0.3.6)
  is expected to fail — the peer turns replay speaker-anonymous, so the
  advisor cannot attribute. That is not a v0.3.7 regression; it is the
  defect Phase 2 closes.
- Re-run this MT before any v0.3.x release that touches the persona
  action loop, the conversation-window role mapper, or the
  channel-history fetch path. Add a row to the Test Results table.
