# Manual Test MT-SESSION-003: F-3 recall isolation + within-session continuity

**Test ID**: `MT-SESSION-003`
**Feature Area**: Sessions (RFC 0031 Phase 2 — per-session recall filtering)
**Version**: 1.1
**Created**: 2026-06-01
**Last Updated**: 2026-07-28
**Status**: Active — **re-anchored for v0.3.12** (see the note below): the Step-3 absence bar moved to the **epoch** axis; facts + episodic absence across sessions is no longer asserted (RFC 0049 cross-room recall is live, classification-gated).

> **v0.3.12 re-anchor ([RFC 0049](../rfcs/0049-memory-consolidation-gradient.md) Phases 0–1 live).** From v0.3.12, fact recall is
> **cross-room by default** and episodic recall is **room-first-ranked** (other-room
> episodes admissible, demoted), every cross-room candidate passing the
> [RFC 0037 §D gate](../rfcs/0037-memory-confidentiality-channel-classification.md#d-the-hard-gate-at-memory-injection).
> A Step-3 arc-two surface of arc-one's *fact* (or a demoted episode) is therefore
> **expected behaviour, not the F-3 reproduction** — the cross-run absence bar this
> MT pinned is carried by a fresh **`PERSATRIX_EPOCH`** ([MT-EPOCH-001](MT-EPOCH-001.md)),
> which must still surface nothing. Run Step 3 under a fresh epoch to assert absence;
> run it under a fresh session (same epoch) to observe the v0.3.12 cross-room
> continuity instead ([MT-MEMORY-CROSSROOM-001](MT-MEMORY-CROSSROOM-001.md)). The
> within-session continuity leg (Step 2) and the `legacy` carve-out (Step 4) are
> unchanged.

---

## Overview

**Purpose**: Verify the **headline v0.3.5 promise** — default persona-memory
recall is scoped to the active session, so a run under a *fresh*
`PERSATRIX_SESSION_ID` surfaces **none** of a prior session's memory across all
four tiers (episodes, relationships, facts, notes), **while** a within-session
arc still continues normally. This is the F-3 cross-run state-bleed fix at the
recall surface, and the OQ #1 tension made concrete: single-session default
recall *is* the dementia-test continuity path
([OQ #1 1a](../rfcs/0031-per-session-namespacing-channels.md#open-questions)).

This is the live counterpart to the structural gates
[`test_session_recall_isolation.py`](../../tests/integration/test_session_recall_isolation.py)
and [`test_session_continuity.py`](../../tests/integration/test_session_continuity.py).

**Scope**:
- Two arcs under two distinct session ids; the second arc must not recall the
  first by default.
- A within-session continuity check (the second turn in arc 1 sees arc 1's
  first turn).
- The always-visible `legacy` carve-out (a legacy-tagged row remains visible
  from any session).

**Out of Scope**:
- The operator verbs (`session new/use/...`) — [MT-SESSION-002](MT-SESSION-002.md).
- Structural (same-participant) residue that a fresh session *cannot* reach
  (relationship trust / person-facts keyed on `--user`) — that is the **epoch**
  axis, [MT-EPOCH-001](MT-EPOCH-001.md). This MT isolates by *session* (room),
  so it varies the channel/room, not the user.
- The full qualitative dementia arc with idle windows — [MT-MEMORY-005](MT-MEMORY-005-dementia-test.md) V5.
- Cross-session opt-in recall (`sessions="*"`): no operator entry point by design
  ([ISSUE-0086](../issues/ISSUE-0086-operator-all-sessions-recall-verb.md)).

---

## Related Documentation

- [docs/guides/sessions.md](../guides/sessions.md) — §1 session-scoped default recall, §7 cross-session opt-in.
- [docs/rfcs/0031-per-session-namespacing-channels.md §D](../rfcs/0031-per-session-namespacing-channels.md#d-recall-semantics) — recall semantics.
- [docs/rfcs/0031-phase2-pr-plan.md](../rfcs/0031-phase2-pr-plan.md) — Phase 2 recall-filtering PR sequence.
- [ISSUE-0051](../issues/ISSUE-0051-per-session-memory-namespacing-channels.md) — the F-3 root-cause issue this closes.

**Related Automated Tests**:
- `tests/integration/test_session_recall_isolation.py` — cross-session recall isolation across the tiers + the `legacy`/`*` behaviour.
- `tests/integration/test_session_continuity.py` — the single-session arc reads back through every tier; arc 2 default recall excludes arc 1.
- `tests/unit/python/test_episodic_session_scope.py` / `test_relationship_session_scope.py` / `test_facts_session_scope.py` / `test_notes_mutation_session_scope.py` — per-tier write+recall scoping.

---

## Preconditions

A live persona stack with `ANTHROPIC_API_KEY` set (the persona is LLM-backed —
arcs land real episode/fact rows from chat turns):

```bash
make demo-anthropic        # or any cloud/offline provider that returns real replies
```

Start from a clean memory surface so prior rows do not mask the result — a fresh
volume set (`make reset` then bring the stack up) is cleanest. The arcs below use
**ember-owl** with a distinct named fact ("Mira") that has no keyword overlap
with the trigger turn, mirroring the dementia-test discipline.

> **Scope an arc at the persona's boot, not per CLI invocation.** The persona
> writes its episodes/facts at *interaction close* (RFC 0020), in its background
> loop — outside any single chat request. That close-path write is tagged with
> the session the **persona-runtime snapshotted at boot** from `PERSATRIX_SESSION_ID`
> (the MT-SESSION-001 contract; the [dementia-test Setup](MT-MEMORY-005-dementia-test.md#setup)
> states the same — "the orchestrator + persona-runtime both snapshot the value
> at start"). A per-invocation `--session`/`PERSATRIX_SESSION_ID` on the CLI
> governs the *recall query* and the channel-dispatch binding for that call, but
> in a long-running persona it does **not** retag the asynchronous close-path
> write. So to land an arc's rows under `arc-one`, **boot the persona under that
> session**:
>
> - **Local**: `PERSATRIX_SESSION_ID=arc-one python -m persatrix_agents.server …`
>   (and the orchestrator likewise), as in [MT-SESSION-001](MT-SESSION-001.md).
> - **Docker**: thread `PERSATRIX_SESSION_ID` into the agent service env (the
>   stock compose does not) and `up` the agent under it, then switch the env and
>   bring it up again for `arc-two`.
>
> Also note `--session <label>` resolves against the registry first, so it only
> accepts a **registered** session ([cli/src/session_resolve.rs](../../cli/src/session_resolve.rs));
> an ad-hoc id like `arc-one` must come via `PERSATRIX_SESSION_ID` (which passes
> through unresolved) or be `session new`-registered first.

---

## Test Procedure

### Step 1: Arc 1 — boot the persona under `arc-one`, establish a named fact

**Action**:

Boot ember-owl under `PERSATRIX_SESSION_ID=arc-one` (see Preconditions), then:

```bash
echo "I'm picking up my daughter Mira from school later — she's seven." \
  | ./bin/persatrix chat ember-owl --user alex
```

(The `chat` REPL reads the message from stdin; pipe one line per turn.) Allow
the interaction to close (RFC 0020 idle window, or a lowered
`interaction_idle_timeout_sec`) so the episode persists.

**Expected**: persona acknowledges naturally; after close, an `episodes` row (and
ideally a `facts` row `(<alex>, has_child_named, "Mira")`) lands tagged
`session_id='arc-one'`.

**Verification**:
- [ ] Reply is a natural acknowledgement.
- [ ] In the persona container: `SELECT session_id, COUNT(*) FROM episodes GROUP BY session_id;`
  shows an `arc-one` row.

---

### Step 2: Arc 1 — within-session continuity (trigger, no keyword overlap)

**Action**:

Still booted under `arc-one`:

```bash
echo "What's a good weekend activity for a kid that age?" \
  | ./bin/persatrix chat ember-owl --user alex
```

**Expected**: the persona references **Mira** (or "your daughter") — the named
entity surfaces from arc-one memory, not the immediate prompt. This is the
single-session continuity guarantee (the dementia-test recall path).

**Verification**:
- [ ] The reply references Mira / "your daughter" by recall, with no keyword
  overlap in the trigger.

---

### Step 3: Arc 2 — fresh session, same trigger, must NOT recall arc 1

**Action**:

Re-boot the persona under a fresh `PERSATRIX_SESSION_ID=arc-two`, same user, then:

```bash
echo "What's a good weekend activity for a kid that age?" \
  | ./bin/persatrix chat ember-owl --user alex
```

**Expected**: under the fresh session `arc-two`, default recall surfaces
**none** of arc-one's rows — the persona does **not** reference Mira, has no
knowledge of a child, and gives generic advice or asks for context. Absence is
the v0.3.5 promise; a reference here would be the F-3 reproduction.

**Verification**:
- [ ] The reply makes **no** reference to Mira / a daughter / age seven.
- [ ] `SELECT session_id, epoch_id FROM episodes;` shows both `arc-one` and
  `arc-two` rows coexisting in storage (isolation is a *recall* filter, not a
  delete). The default-recall predicate (`session_id IN (active, 'legacy')`)
  under `arc-two` returns only the `arc-two` + `legacy` rows.

---

### Step 4: Confirm the `legacy` carve-out stays visible

**Action**:

A row written while the persona booted with `PERSATRIX_SESSION_ID` **unset**
lands under `legacy` and stays visible from *every* session — the always-visible
carve-out that let sessions ship without a backfill. Confirm a `legacy`-tagged
row is in the default-recall set under a named session:

```bash
# In the persona container, against the real memory.db:
#   default recall under arc-two = rows WHERE session_id IN ('arc-two','legacy')
# A legacy episode appears for arc-two; an arc-one episode does NOT.
```

**Expected**: `legacy` rows are in `arc-two`'s default-recall set (contrast
Step 3, where `arc-one` is excluded).

**Verification**:
- [ ] The default-recall set under `arc-two` is `arc-two ∪ legacy` — legacy
  visible, arc-one excluded.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|------------------|-----------|
| 1 | Arc 1 establishes the Mira fact under `arc-one` | ☐ |
| 2 | Within-session continuity: arc 1 recalls Mira (no keyword overlap) | ☐ |
| 3 | Fresh session `arc-two` recalls **nothing** of arc 1 (F-3 closed) | ☐ |
| 4 | `legacy` rows stay visible from a named session | ☐ |

**Release gate**: Step 2 **and** Step 3 both pass — continuity is preserved
*and* cross-session bleed is closed. A pass on only one is a fail (recall that is
either too broad or too narrow).

---

## Edge Cases & Error Scenarios

### Edge Case 1: Same room, same user, fresh session — structural residue

A fresh **session** isolates *room* memory but a relationship/fact keyed on
`--user alex` can still carry forward if the *room/channel* is reused under a
session whose rows already exist. The clean structural-isolation guarantee
("same room + same user, inherit nothing") is the **epoch** axis, not the session
axis — see [MT-EPOCH-001](MT-EPOCH-001.md). Do not expect a fresh session alone
to reset participant-keyed trust.

### Edge Case 2: Forgetting to pin the session id mid-arc

If `--session` / `PERSATRIX_SESSION_ID` is dropped between turns of an arc, the
turn resolves a *different* session (the pointer, or `legacy`), producing a
spurious continuity miss in Step 2. Re-pin before every turn — this is the most
common false-fail (called out in [MT-MEMORY-005 Setup](MT-MEMORY-005-dementia-test.md#setup)).

### Edge Case 3: Cross-session opt-in has no operator verb

An operator *dump* verb across sessions (`sessions="*"` or an explicit id
list) remains unbuilt — deliberately
([ISSUE-0086](../issues/ISSUE-0086-operator-all-sessions-recall-verb.md),
[sessions guide §7](../guides/sessions.md#7-cross-room-recall--the-v0312-posture)). Since
v0.3.12, Step 3 surfacing arc-one *facts* needs no flag at all — that is the
classification-gated runtime widening (the re-anchor note above), not an
operator read.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-06-01 | Claude (Opus 4.8) | macOS + Docker (OpenAI stack) | Pass | Recall isolation verified **live on persisted rows** on `main` `3ceb400`: persona booted under `PERSATRIX_SESSION_ID=arc-one` persisted an `arc-one` episode (coexisting with `legacy`); default recall under `arc-two` = `{legacy}` only — the `arc-one` episode is excluded (F-3 closed), `legacy` stays visible. Within-session continuity + multi-session no-bleed carried by green [`test_session_continuity.py`](../../tests/integration/test_session_continuity.py). Finding folded into Preconditions: scope an arc at the persona **boot**, not per CLI invocation. See [v0.3.5-execution-report.md](v0.3.5-execution-report.md#mt-session-003--f-3-recall-isolation-live--gate). |

---

## Notes

- The "no keyword overlap" rule on the Step 3 trigger is load-bearing: a trigger
  containing "Mira" would exercise retrieval-by-keyword, not session-scoped
  recall, and make the result meaningless (same discipline as
  [MT-MEMORY-005](MT-MEMORY-005-dementia-test.md)).
- Session isolation is a **recall filter**, not a delete — Step 3's storage check
  confirms both arcs' rows coexist; only what default recall *surfaces* changes.
- This MT varies the **session** (room), holding the user constant, to isolate
  the room-continuity axis. Holding both room and user constant while varying the
  **epoch** is [MT-EPOCH-001](MT-EPOCH-001.md).
