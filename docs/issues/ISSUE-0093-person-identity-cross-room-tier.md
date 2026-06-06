---
id: ISSUE-0093
summary: "F-7 Option D (architectural target) — re-home person identity off room-scoped notes onto a tier whose scope is intrinsic, so the F-7 cross-room recall seam cannot recur by construction. F-7 PR A (#550) closed the seam by special-casing `contact:*` note recall to bypass the session filter; that is a topic-prefix workaround threaded through recall, not a property of where identity lives. The genuinely cross-room tier is `relationships` (its primary key deliberately excludes `session_id`; already auto-injected via `recall_relationship_summary`); the `facts` tier (RFC 0026) is itself session-scoped today (recall uses `_resolve_session_list` + `session_in_clause`) so it is NOT an automatic cross-room home, and fact extraction only fires at interaction close (`persona_runtime/fact_extractor` via `summarize_close`) — which is why the live repro's facts table was empty (the probe conversations never closed). Re-homing identity onto the relationship record (cross-room core) lets `recall_notes` revert to purely room-scoped and retires the PR-A special-case. Implementation carries an RFC 0031 §C/§D + RFC 0026 amendment and depends on an eager identity-capture path; design captured here, sequenced on the memory-quality roadmap."
status: resolved
severity: medium
area: agents/memory
created: 2026-06-05
closed: 2026-06-06
closed_pr:
refs:
  - docs/rfcs/0031-amendment-person-identity-cross-room-tier.md
  - docs/v0.3.7-f7-cross-room-recall-seam.md
  - docs/memory-scope-axes.md
  - docs/memory-quality-roadmap.md
  - docs/rfcs/0031-amendment-person-keyed-note-recall.md
  - agents/memory/_notes_recall.py
  - agents/memory/relationship.py
  - agents/memory/_migration_identity_backfill.py
  - agents/persona_runtime/fact_extractor.py
---

## Context

F-7 (the [cross-room recall seam](../v0.3.7-f7-cross-room-recall-seam.md)) was closed for robustness by **Option A** ([#550](https://github.com/mkhomutov/Persatrix/pull/550)): `NoteStore.recall_notes` now special-cases `contact:*` topics to bypass the session filter (cross-room), so the LLM-facing `recall_notes` tool and auto-injection agree. That works, but **scope is still being decided by a topic-prefix rule threaded through recall**, not by where the data lives. This issue tracks **Option D** — the architectural end-state where *scope is a property of the tier*, after which the PR-A special-case can be retired.

## What the investigation found

- **`relationships` is the genuinely cross-room tier.** Its primary key `(participant_id, participant_type, other_participant_id, other_participant_type)` deliberately omits `session_id` ([memory-scope-axes.md §Relationship](../memory-scope-axes.md#relationship--cross-room-per-individual)); the aggregate is cross-room by construction and is already auto-injected every turn via `recall_relationship_summary`.
- **`facts` (RFC 0026) is session-scoped today.** `FactStore.recall` resolves a session list and applies `session_in_clause` — so re-homing identity onto facts would *not* be cross-room for free; it would need its own principal-scoped recall. The relationship tier is the lower-risk home.
- **Identity capture is split and ill-fit.** The `memory-tool-usage` prompt has the persona write identity via `store_note(topic="contact:<id>")` — a **room-scoped** notes write (the workaround PR A then special-cased on read). The `facts` extractor only runs at **interaction close**, so identity facts land late and were absent in the live repro (the probe conversations never closed). Neither path writes the cross-room relationship record.

## Target design

Person identity (name, role, stable preferences) is **stored on, and recalled from, the cross-room relationship record** — the tier whose scope is intrinsically the person, not the room. Then:

- `recall_notes` reverts to **purely room-scoped** — the PR-A `contact:*` bypass (and `_notes_session_clause`) is removed; `session_in_clause` has one shape again.
- The recall seam **cannot recur by construction**: there is no second, narrower path, because identity no longer lives in a room-scoped tier.

## Decision points (for the implementation RFC amendment)

> **Resolved** in the draft amendment [RFC 0031 — Person Identity Lives on the Cross-Room Relationship Tier](../rfcs/0031-amendment-person-identity-cross-room-tier.md): (1) **write-through** from `store_note(contact:<id>)` to the relationship record (preserves immediacy, no new tool/latency); (2) **structured identity** (`{name, role, prefs}`) merged by key on a **dedicated `identity` column** (kept off the trust `notes` field so trust writes can't clobber it); (3) **facts stays session-scoped** — identity lives on the relationship tier only. The points below are the original open questions, retained for context.

1. **Capture mechanism.** There is no LLM tool today for "record what I learned about this person" onto the relationship record. Options: (a) route the existing `store_note(contact:*)` call to *also/instead* write the relationship record; (b) a dedicated relationship-note tool; (c) eager identity extraction (not only at interaction close). Whichever is chosen must preserve today's *immediacy* (the persona knows your name within the first conversation), which extraction-at-close does not.
2. **Structured vs prose.** The relationship record's `notes` is free text; decide whether identity is structured (name/role/prefs fields) or prose, and how it merges/supersedes across turns.
3. **Facts tier role.** Either leave `facts` session-scoped (identity lives on relationship only) or give the facts tier a principal-scoped recall and use it for identity — the former is simpler and is the recommended first step.

## Retirement of the PR-A workaround

**✅ Done (PR D3).** Identity is recalled cross-room from the relationship tier, so the Option-A read carve-out was removed: `_notes_recall._notes_session_clause` + the `contact:*` recall widening are gone (the three recall helpers revert to `session_in_clause`), and `recall_contact_notes` (`NoteStore` + the `EpisodicMemory` delegate) is dropped. The D2 dual-write note is also dropped — `store_note(contact:*)` now writes identity only (falling back to a note on failure / no handle, so nothing is lost). `session_in_predicate` is kept (a clean primitive independent of this). **✅ Done (PR D4).** The one-time backfill of pre-cutover `contact:*` notes onto relationship identity rows shipped as migration **v14** ([`_migration_identity_backfill.py`](../../agents/memory/_migration_identity_backfill.py)). The participant-type design choice it carried is resolved by not guessing: a note pins `(agent_id, principal_id, epoch_id, other_id)` + `participant_type='agent'`, leaving `other_participant_type` as the only unrecorded PK axis, so the backfill **inherits** it from existing relationship rows for that tuple (or, for an orphan with no such row, creates one under the default `"agent"` type at neutral trust). With D4, ISSUE-0093 (F-7 Option D) is complete.

## Dependencies & sequencing

- An eager identity-capture path (decision point 1) — the gating dependency.
- An **RFC amendment** (RFC 0031 §C/§D person axis + RFC 0026), since this changes where identity is authored/recalled.
- **Target: v0.3.x — after the capture-mechanism decision** (row 10 on the [memory-quality roadmap](../memory-quality-roadmap.md)). Deliberately **not v0.3.8** (that milestone is brainstorm convergence — RFC 0030 governance — and this adds no new *user* capability, since Option A already delivers cross-room recall functionally). Not blocking: F-7 is functionally closed by Option A; this is architectural cleanup.

## Test strategy (when implemented)

- Identity stated in room A is recalled in room B from the **relationship** tier (cross-room), with no `contact:*` note involved.
- `recall_notes` is purely room-scoped again (a `contact:*` note, if any remain, does **not** cross rooms) — i.e. the PR-A carve-out is gone.
- Principal/epoch boundaries still hold (cross-room ≠ cross-tenant/epoch).
- Immediacy: identity is available within the first conversation, not only after interaction close.
