# RFC 0031 Amendment — Person-Keyed Note Recall Crosses Rooms

**Type**: amendment to [RFC 0031](0031-per-session-namespacing-channels.md) §D (Recall Semantics)
**Status**: ⛔ **Superseded** (v0.3.7) by [RFC 0031 Amendment — Person Identity Lives on the Cross-Room Relationship Tier](0031-amendment-person-identity-cross-room-tier.md) (F-7 Option D, ISSUE-0093, **PR D3**). Shipped as Option A ([test-findings PR plan](../v0.3.7-test-findings-pr-plan.md) PR 5, finding F-3b), then retired. Kept for the design history.
**Author**: Maksim Khomutov
**Date**: 2026-06-05
**Target**: v0.3.7
**Trigger**: Manual end-to-end testing of the v0.3.7 stack. A persona was told a person's name and favourite language in one channel (it called `store_note` under topic `contact:local`, confirmed in the SQLite store), then in a **fresh channel** answered *"I don't have any notes about your name"*. Root cause: `contact:<id>` identity notes inherited the §D room-scoped recall default, so a person met in one room was invisible in another.
**Supersedes**: nothing. **Narrows** the §D default for one note class (person-keyed `contact:*` topics), leaving every other note room-scoped.

> **⛔ Superseded (PR D3).** This amendment shipped F-7 **Option A** — scope decided by a topic-prefix rule threaded through *recall* (`recall_contact_notes` + the `contact:*` widening in `NoteStore.recall_notes`). [Option D](0031-amendment-person-identity-cross-room-tier.md) re-homed person identity onto the genuinely cross-room **relationship** tier (PK omits `session_id`), and **PR D3 retired the Option-A carve-out**: `recall_contact_notes` is gone, `recall_notes` is room-scoped again, and the cross-room recall seam cannot recur by construction (identity no longer lives in a room-scoped tier). The narrative below describes the now-removed Option A and is retained only for design history.

---

## Context

[`docs/memory-scope-axes.md`](../memory-scope-axes.md) separates the axes that the single word "session" had been conflating. Two are load-bearing here:

- **Session = room** — notes keyed `(agent, channel)`, **isolated by default**; cross-room recall is an explicit, opt-in path. This is correct and unchanged.
- **Relationship / person** — *"who is this person, to me?"* — keyed to the **individual**, **cross-room by design**. The `relationships` table already keeps `session_id` out of its primary key for exactly this reason.

The defect (finding F-3b): person-identity facts are saved as **notes** (the `memory-tool-usage` prompt instructs `store_note` under topic `contact:<user_id>`), and notes recall defaulted to `session_id IN (active, legacy)` ([RFC 0031 §D](0031-per-session-namespacing-channels.md#d-recall-semantics)). So identity — which belongs on the cross-room person axis — was trapped on the room axis. The persona genuinely had the note; recall scope hid it.

## The amendment

A note whose **topic is person-keyed** — the `contact:<participant_id>` convention the `memory-tool-usage` snippet already mandates — is recalled **cross-room**: from every session, not just the active one. The widening is bounded on three sides so it cannot become a general cross-room leak:

1. **Topic-exact, not query.** Only the literal `contact:<participant_id>` topic crosses the boundary — never arbitrary room notes that happen to mention a name. Implemented as an exact `topic = ?` match, no FTS/LIKE.
2. **Principal- and epoch-scoped still.** The session filter is dropped, but `principal_id` and `epoch_id` equality are still enforced — **cross-room, never cross-tenant or cross-epoch**. The `sessions="*"` debug sentinel is *not* used (RFC 0031 §Security Considerations pins the persona-runtime default path away from it); this is a dedicated topic-scoped query, not a `sessions` widening of general recall.
3. **General recall unchanged.** `recall_notes()` keeps the §D default (`session IN (active, legacy)`). Only the new dedicated path (`recall_contact_notes(participant_id)`) is cross-room.

### Where it fires

- **Auto-injection (primary).** `_inject_memory_context` recalls the **event sender's** contact note via `recall_contact_notes(sender_id)` and merges it into the `recent_notes` tier (contact-first, dedup by id). Because this path is sender/topic-driven rather than query-driven, the persona recalls *who it is talking to* in any room even when the inbound message shares no words with the stored note — the exact gap that defeated the room-scoped, lexical default.
- **Tool path (unchanged, deliberately).** The LLM-facing `recall_notes(query, limit)` tool stays room-scoped. Cross-room identity is delivered by injection, so the persona does not need a wider tool to know who it is talking to; widening the free-form tool would be a larger, fuzzier surface and is out of scope.

## §D wording delta

> **Before (§D default).** "The window filters on `event.channel_id` only … rows are admitted regardless of `chat_session_id` or `persatrix_session_id`." For notes: recall defaults to `session_id IN (active_session, legacy)`.
>
> **After (this amendment).** Unchanged for all notes **except** those whose topic matches `contact:<participant_id>`. Person-keyed notes are recalled across all sessions for the same `(agent_id, principal_id, epoch_id)`; every other note keeps the room-scoped default.

## Alternative considered — route identity to the facts tier

Person identity could instead flow through the RFC 0026 declarative-facts tier (cross-room when principal-scoped) rather than `contact:*` notes. In the live repro the facts table was **empty** — extraction is not capturing "my name is X" today — so that path would have required a second change (fact extraction) to deliver any user-visible fix. The notes path already exists and is the lower-risk v0.3.7 fix. Re-homing identity onto facts is tracked as a follow-up (candidate ISSUE-0093), sequenced with [memory-quality-roadmap.md](../memory-quality-roadmap.md), not this amendment.

## Files touched

- `agents/memory/_notes_recall.py` — `_recall_contact_notes` (exact-topic, no session clause, principal/epoch enforced).
- `agents/memory/notes.py` — `NoteStore.recall_contact_notes(participant_id, *, limit)`.
- `agents/memory/episodic_notes_api.py` — `EpisodicMemory.recall_contact_notes` delegation.
- `agents/persona_runtime/contact_section.py` — new tier helper: `recall_notes_for_event` (room-scoped query recall + cross-room contact merge) / `merge_sender_contact_notes`, mirroring the relationship / channel-history / facts tier helpers.
- `agents/persona_runtime/memory_context.py` — notes tier delegates to `recall_notes_for_event` (keeps the module under the 500-line cap).
- `prompts/runtime/safety/memory-tool-usage.md` — wording updated: person facts are remembered across conversations; other notes and the transcript stay per-conversation (completes the F-3a honesty change, which described only current room-scoped behaviour ahead of this PR).

## Test strategy

- Cross-room visibility, topic-exactness, preserved room isolation for non-contact notes, and epoch scoping — `tests/unit/python/test_contact_note_cross_room_recall.py`.
- Injection wiring: the sender's contact note is injected even when the inbound query does not match it — same module, `TestContactNoteInjectedCrossRoom`.
- Prompt honesty + byte identity — `test_memory_tool_usage_honesty.py`, `test_persona_section_composer.py`, `test_prompt_loader.py`, `test_memory_instructions.py`.
- Manual: [MT-PERSONA-008](../manual-tests/MT-PERSONA-008.md) — a person introduced in one channel is recalled in a fresh one.

## Security considerations

The widening keeps `principal_id` (tenant) and `epoch_id` (run isolation) equality, so it never crosses a tenant or a run-isolation boundary — it only relaxes the *room* axis, which is exactly the person axis memory-scope-axes.md designates as cross-room. The `sessions="*"` sentinel remains unreachable on the persona-runtime path.
