# RFC 0031 Amendment — Person Identity Lives on the Cross-Room Relationship Tier

**Type**: amendment to [RFC 0031](0031-per-session-namespacing-channels.md) §C (Storage Model) + §D (Recall Semantics), and to [RFC 0026](0026-declarative-facts-tier.md) (facts-tier scope boundary)
**Status**: 🚧 In progress — **PR D1 (storage foundation) implemented**; D2 (write-through + render) and D3 (backfill + Option-A retirement) remain. Target **v0.3.x** (deliberately not v0.3.8). Tracks [ISSUE-0093](../issues/ISSUE-0093-person-identity-cross-room-tier.md) (F-7 Option D).
**Author**: Maksim Khomutov
**Date**: 2026-06-05
**Target**: v0.3.x — after this design is ratified; sequenced on [memory-quality-roadmap.md](../memory-quality-roadmap.md) row 10.
**Trigger**: F-7 ([cross-room recall seam](../v0.3.7-f7-cross-room-recall-seam.md)) was closed for robustness by **Option A** ([#550](https://github.com/mkhomutov/Persatrix/pull/550)), which special-cased `contact:*` note recall to bypass the session filter. That is a topic-prefix workaround threaded through *recall*, not a property of *where identity lives*. Live re-verification on the running stack confirmed the underlying split: two personas told a person-fact in a DM diverged in a group — one had filed it under `topic=contact:local` (crossed rooms), the other under `topic=Persatrix` (stayed room-scoped) — because scope is decided by a string the LLM happens to pick at write time.
**Supersedes**: on implementation of the retirement step (PR D3 below), this **reverts** the [RFC 0031 person-keyed note-recall amendment](0031-amendment-person-keyed-note-recall.md): once identity is recalled cross-room from the relationship tier, the `contact:*` recall carve-out (§D) is removed and `recall_notes` returns to a single room-scoped shape.

---

## Context

[`memory-scope-axes.md`](../memory-scope-axes.md) establishes that scope is **subject-dependent**: narrative and topic/room facts ride the **session = room** axis (isolated by default); knowledge and affect *about a person* ride the **person** axis (cross-room by design). The `relationships` table already encodes this — its primary key deliberately omits `session_id`.

Today, person identity (name, role, stable preferences) is captured as **room-scoped notes** (`store_note(topic="contact:<id>")`), then retrofitted to cross-room behavior by the Option-A recall carve-out. This amendment removes the retrofit by **storing identity where its scope is intrinsic**: the relationship record. After this lands, scope is a property of the tier, not of a topic prefix, and the F-7 seam **cannot recur by construction** — there is no second, narrower read path because identity no longer lives in a room-scoped tier.

This amendment does **not** claim to make *classification* deterministic. Deciding "is this fact about the person or about a topic?" remains a judgment the persona makes at capture time (as it must — see [§ Non-goals](#non-goals)). What it makes deterministic is **scope, once classified**: an identity write is cross-room because of the tier it lands on, never because of how the topic string was spelled.

## Non-goals

- **Eliminating the capture-time classification judgment.** Something always decides what counts as identity; this RFC fixes *where it is stored*, not *whether the model recognises it*. Prompt hardening of that judgment is a separate, orthogonal lever (Option C).
- **Re-homing topic/room facts.** Topic facts (`"this channel shipped Friday"`, `"Persatrix is an environment…"`) stay room-scoped, in `notes`/`facts`. Only person-subject identity moves.
- **Changing tenant/run isolation.** The relationship PK already includes `principal_id` and `epoch_id`, so cross-room is automatically never cross-tenant or cross-epoch — no new boundary work.

---

## Decisions (resolving the three ISSUE-0093 decision points)

### D-1 — Capture mechanism: **write-through from `store_note(contact:<id>)` to the relationship record** (option (a))

The existing per-turn `store_note(topic="contact:<id>", …)` call — already mandated by [`memory-tool-usage.md`](../../prompts/runtime/safety/memory-tool-usage.md) and reliably emitted by the model for name/role — is **intercepted at the store boundary** and upserts identity onto the relationship record, rather than (eventually: instead of) writing a room-scoped note.

Rejected alternatives:
- **(b) dedicated relationship-note tool** — new LLM-facing surface; the model must learn *when* to call it; churns prompt goldens. No immediacy or correctness advantage over reusing the call the model already makes.
- **(c) eager identity extraction (a per-turn extractor LLM call)** — adds a second model round-trip to every turn (latency + cost) to recover a signal `store_note(contact:*)` already carries explicitly. Reject for the first step; may revisit if the write-through proves too sparse.

**Why (a) preserves immediacy.** The gating constraint from ISSUE-0093 is that the persona must know your name *within the first conversation*, which the close-time facts extractor (`summarize_close`) cannot guarantee. `store_note(contact:*)` already fires mid-conversation, the moment the model learns the fact — so routing it preserves today's immediacy exactly, with no new latency.

**Inheriting classification non-determinism is acceptable and bounded.** Routing the existing call means we inherit the model's choice of the `contact:` prefix as the identity signal. That is fine: the goal is intrinsic *scope*, not perfect *classification*. When the model does file under `contact:`, identity now lands cross-room deterministically; when it mis-files under a topic, the result is no worse than today (a room-scoped fact). Tightening that judgment is Option C, tracked separately.

### D-2 — Representation: **structured identity, merged by key, on a dedicated column**

Identity is stored as a **small structured object** (`{name, role, prefs: […]}`), JSON-encoded in a **new `identity` column** on `relationships`, **separate from the existing `notes` column**.

- **Separate column, not `notes`.** `notes` is overwritten on every `update_trust(reason)` call (`notes = ?` in the upsert, [relationship_mutations.py:118](../../agents/memory/relationship_mutations.py)) — it holds the latest *trust* reason. Co-locating identity there would let a trust change clobber the name. A dedicated column keeps affect (trust) and identity cleanly separated.
- **Structured, not prose.** A key/value object gives deterministic **supersede semantics**: a new `name` replaces the old `name` (last-writer-wins per key), a new preference unions into `prefs`. Free-text prose has no merge rule and grows unbounded. The structure stays small enough to render as one injected line (`Identity: Name Max; Role …`).
- **Merge, never overwrite.** The write API is an **upsert that shallow-merges** the incoming keys into the existing object, so partial updates across turns accumulate instead of clobbering.

### D-3 — Facts tier role: **leave `facts` session-scoped; identity lives on relationship only**

The RFC 0026 facts tier stays room-scoped (its recall resolves a session list via `_resolve_session_list` + `session_in_clause`). Identity does **not** flow through facts. This is the simpler, lower-risk split recommended in ISSUE-0093: one cross-room home (relationship) for person identity, one room-scoped home (facts/notes) for everything else — matching the subject-scope rule directly. Giving facts a principal-scoped recall is explicitly deferred (not needed for identity).

---

## §C / §D wording delta

**RFC 0031 §C (Storage Model).** Add: the `relationships` row carries person **identity** (`identity` column, structured) in addition to trust/affect (`notes`). Identity is keyed by the relationship PK `(participant_id, participant_type, other_participant_id, other_participant_type, principal_id, epoch_id)` — cross-room, principal/epoch-scoped — by construction.

**RFC 0031 §D (Recall Semantics).** On retirement (PR D3): the [person-keyed note-recall amendment](0031-amendment-person-keyed-note-recall.md) is reverted — `recall_notes()` and all three recall helpers return to the single `session IN (active, legacy)` shape; the `contact:*` bypass is gone. Person identity is no longer recalled from `notes` at all; it is delivered by the relationship-summary injection, which is already cross-room.

**RFC 0026 (facts tier).** Clarify the boundary: the facts tier is room-scoped and is **not** the home for person identity; identity is the relationship tier's responsibility. (No behavioral change to facts.)

---

## Design

### Write path (capture)

```
store_note(topic="contact:<id>", content) ──► [store boundary intercept]
        │  topic matches contact:<participant_id>
        ▼
  RelationshipStore.upsert_identity(other_id, fields)   # new, non-destructive merge
        │  shallow-merge {name, role, prefs} into relationships.identity
        ▼
  relationships row (PK excludes session_id) — cross-room by construction
```

- New method `upsert_identity(other_id, fields, *, participant_type, other_participant_type, principal_id, epoch_id)` on the relationship store — merges, does not overwrite, and never touches the trust `notes` column.
- The intercept lives at the `NoteStore.store_note` / `store_note` tool boundary so the prompt and the model's behavior are unchanged. During transition (PR D2) it may **dual-write** (relationship identity *and* the legacy room-scoped contact note) so recall can be migrated and verified before the note write is dropped.
- A light structuring step parses `content` (e.g. `"Name: Max. Favorite language: Rust."`) into `{name, prefs}`. This is deliberately simple (regex/keyed-prefix), not an LLM call — the prompt already asks for a keyed shape; anything unparsed is stored under a `raw` key so nothing is lost.

### Read path (recall)

Essentially free. `recall_relationship_summary` already runs every turn and `get_relationship_summary` already returns the relationship row; `render_relationship_section` already injects `notes`. The only change is to **also render the new `identity` field** as an injected line (`relationship_section.py` `render_relationship_section`, [../../agents/persona_runtime/relationship_section.py](../../agents/persona_runtime/relationship_section.py)). No new query, no new recall path, no `sessions` plumbing — the relationship tier is cross-room intrinsically.

### Schema migration

- Add nullable `identity TEXT` (JSON) column to `relationships` (new migration version, additive — no rewrite of the PK or existing rows).
- **Backfill** (PR D3): one-time pass that reads existing `contact:<id>` notes and `upsert_identity`-merges them onto the matching relationship row, so personas don't lose identity learned before the cutover.

### Retirement (after D2 is verified)

Remove the Option-A carve-out — sized at ~11 core lines + 3 test files:
- `agents/memory/_notes_recall.py` — delete `_notes_session_clause`, `_recall_contact_notes`, `CONTACT_TOPIC_PREFIX`; the three recall helpers (`_recall_notes_fts5` / `_like` / `_recency`) revert to `session_in_clause`.
- `agents/memory/notes.py` + `episodic_notes_api.py` — drop `recall_contact_notes`.
- `agents/persona_runtime/contact_section.py` — `merge_sender_contact_notes` no longer needed (identity now arrives via the relationship section); `recall_notes_for_event` collapses back to plain room-scoped notes recall.
- Keep `session_in_predicate` — it is a clean primitive independent of this.
- Revert the prose in `memory-tool-usage.md` and update the tests in `test_contact_note_cross_room_recall.py` to assert the *new* contract (identity crosses rooms via relationship; `contact:*` notes, if any, do **not**).

---

## PR sequencing

| PR | Scope | Gates |
|----|-------|-------|
| **D1** | Schema migration (`identity` column) + `upsert_identity` merge API on the relationship store, with tests. No behavior change yet. | — |
| **D2** | Write-through intercept (`store_note(contact:*)` → `upsert_identity`, dual-write during transition) + render `identity` in the relationship section. **Parity + immediacy tests.** | D1 |
| **D3** | Backfill existing `contact:*` notes → relationship identity; **retire** the Option-A carve-out; revert RFC 0031 §D to single room-scoped shape; stop the legacy contact-note write. | D2 verified live |

Roughly **3 PRs + this amendment**; the weight is design risk on D2 (parsing/merge + the dual-write→cutover) and the D1 migration, not lines of code.

> **D1 status**: ✅ implemented. Migration **v13** adds a nullable `identity TEXT` (JSON) column to `relationships` ([`_migration_identity.py`](../../agents/memory/_migration_identity.py)) — additive, no PK rebuild (identity is per-row payload, not a key column, so it follows the simple v7 `ADD COLUMN` skeleton rather than the v11/v12 rebuild). The merge rule lives in the pure [`merge_identity`](../../agents/memory/relationship_types.py) (scalar last-writer-wins; `prefs` order-preserving union; `None` skipped). `RelationshipMemory.upsert_identity` writes only the `identity` column (never the trust `notes`), creating the row at neutral trust if absent; `RelationshipMemory.get_identity` reads it with principal/epoch strict equality but **no session filter** — the cross-room property by construction (no `sessions="*"` sentinel; the room axis is not part of the tier's key). No prompt-facing change yet (rendering is D2). Coverage: [`test_relationship_identity.py`](../../tests/unit/python/test_relationship_identity.py) (merge purity, migration schema/idempotency, upsert merge/supersede, notes↔identity non-clobber, cross-room recall, principal/epoch isolation) + the bumped migration-count discipline pins in `test_episodic_memory_core.py` / `test_episodic_memory_retention.py`.

## Test strategy

- **Cross-room without notes.** Identity stated in room A is recalled in room B from the **relationship** tier, with no `contact:*` note involved (after D3, asserting the carve-out is gone).
- **Room isolation intact.** A topic fact (`"Persatrix is an environment…"`) stated in room A is **not** recalled in room B — confirming only identity crosses.
- **Immediacy.** Identity is available within the first conversation (write-through fires mid-turn), not only after interaction close.
- **Merge/supersede.** A later `name` supersedes an earlier one; a new preference unions; a trust-reason write does not clobber identity (and vice versa).
- **Boundary.** Principal/epoch equality still holds — cross-room never cross-tenant or cross-epoch (free from the PK, but pinned by a test).
- **Migration/backfill.** Pre-cutover `contact:*` notes land on the relationship `identity` after backfill.

## Security considerations

The relationship PK already includes `principal_id` and `epoch_id`, so identity recall is cross-*room* only — never cross-tenant or cross-epoch — by construction, with no `sessions="*"` sentinel anywhere on the path. This is strictly **narrower** than the Option-A carve-out it replaces (which relaxed the room filter at the query layer); here the room axis simply isn't part of the tier's identity at all.

## Alternatives considered

- **Keep Option A indefinitely.** Functionally fine, but leaves scope decided by a topic-prefix string at the query layer and keeps the two-path structure that produced the seam. Rejected as the end state; retained as the interim (it ships today).
- **Route identity through the facts tier (RFC 0026).** Facts are session-scoped today and extraction only fires at interaction close — so this would need *both* a principal-scoped facts recall *and* an eager extractor to match today's immediacy. Higher risk than reusing the already-cross-room, already-injected relationship tier. Deferred (D-3).
- **Dedicated relationship-note tool / per-turn extractor.** See D-1 — more surface and/or latency for no gain over reusing the existing `store_note(contact:*)` signal.
