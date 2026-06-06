# RFC 0031 Amendment — Person Identity Lives on the Cross-Room Relationship Tier

**Type**: amendment to [RFC 0031](0031-per-session-namespacing-channels.md) §C (Storage Model) + §D (Recall Semantics), and to [RFC 0026](0026-declarative-facts-tier.md) (facts-tier scope boundary)
**Status**: ✅ Implemented — **PR D1 (storage foundation) + D2 (write-through + render) + D3 (Option-A retirement) + D4 (one-time backfill of pre-cutover `contact:*` notes) all landed**. Target **v0.3.x** (deliberately not v0.3.8). Tracks [ISSUE-0093](../issues/ISSUE-0093-person-identity-cross-room-tier.md) (F-7 Option D).
**Author**: Maksim Khomutov
**Date**: 2026-06-05
**Target**: v0.3.x — after this design is ratified; sequenced on [memory-quality-roadmap.md](../memory-quality-roadmap.md) row 10.
**Trigger**: F-7 ([cross-room recall seam](../v0.3.7-f7-cross-room-recall-seam.md)) was closed for robustness by **Option A** ([#550](https://github.com/mkhomutov/Persatrix/pull/550)), which special-cased `contact:*` note recall to bypass the session filter. That is a topic-prefix workaround threaded through *recall*, not a property of *where identity lives*. Live re-verification on the running stack confirmed the underlying split: two personas told a person-fact in a DM diverged in a group — one had filed it under `topic=contact:local` (crossed rooms), the other under `topic=Persatrix` (stayed room-scoped) — because scope is decided by a string the LLM happens to pick at write time.
**Supersedes**: as of the retirement step (**PR D3, shipped**), this **reverts** the [RFC 0031 person-keyed note-recall amendment](0031-amendment-person-keyed-note-recall.md): identity is recalled cross-room from the relationship tier, the `contact:*` recall carve-out (§D) is removed, and `recall_notes` is back to a single room-scoped shape.

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
- **Backfill** (PR D4, shipped): one-time pass (migration **v14**) that reads existing `contact:<id>` notes and merges their parsed identity onto the matching relationship row, so personas don't lose identity learned before the cutover. Split to a dedicated PR because it carries a genuine design choice (notes don't record `other_participant_type`, which is part of the relationship PK) that should not ride the retirement. **Resolved** (see the D4 status note below): a note pins `(agent_id, principal_id, epoch_id, other_id)` and `participant_type='agent'`, leaving `other_participant_type` as the *only* unrecorded PK axis — the backfill **inherits** it from existing relationship rows for that tuple, or, for an orphan note with no such row, creates one under the default `"agent"` type (the same fallback both the write-through and recall sides use when the sender type is unbound). The other three axes are never fabricated, so the tenant/epoch isolation guarantee is preserved.

### Retirement (PR D3 — shipped)

The Option-A carve-out is removed (cross-room recall) and the D2 dual-write note is dropped, so identity lives on the relationship tier alone:
- `agents/memory/_notes_recall.py` — deleted `_notes_session_clause`, `_recall_contact_notes`, `CONTACT_TOPIC_PREFIX`; the three recall helpers (`_recall_notes_fts5` / `_like` / `_recency`) revert to `session_in_clause`.
- `agents/memory/notes.py` + `episodic_notes_api.py` — dropped `recall_contact_notes`.
- `agents/persona_runtime/contact_section.py` → **renamed** `notes_section.py`; `merge_sender_contact_notes` deleted (identity now arrives via the relationship section); `recall_notes_for_event` collapses to plain room-scoped notes recall.
- `agents/tools/identity_write_through.py` — owns `CONTACT_TOPIC_PREFIX` now; `maybe_write_through_identity` returns a bool so `store_note(contact:*)` writes identity **only** (no note), falling back to the note write on no-handle / unparseable / upsert-failure so nothing is ever lost.
- Keep `session_in_predicate` — it is a clean primitive independent of this.
- Tests: `test_contact_note_cross_room_recall.py` → **renamed** `test_contact_note_room_scoped.py` asserting the *new* contract (notes are uniformly room-scoped; `recall_contact_notes` is gone); the F-7 Option-A pin `test_recall_notes_scope.py` is removed; `test_identity_write_through.py` updated for identity-only + the fallback safety net. (Cross-room identity is proven by `test_identity_render.py::TestIdentityImmediacyCrossRoom`, unchanged.)

---

## PR sequencing

| PR | Scope | Gates |
|----|-------|-------|
| **D1** | Schema migration (`identity` column) + `upsert_identity` merge API on the relationship store, with tests. No behavior change yet. | — |
| **D2** | Write-through intercept (`store_note(contact:*)` → `upsert_identity`, dual-write during transition) + render `identity` in the relationship section. **Parity + immediacy tests.** | D1 |
| **D3** | **Retire** the Option-A carve-out; revert RFC 0031 §D to a single room-scoped shape; stop the legacy contact-note write (identity served from the relationship tier alone). | D2 verified live |
| **D4** (follow-up) | One-time **backfill** (migration v14) of pre-cutover `contact:*` notes → relationship identity (split out of D3 — carries a participant-type design choice). ✅ shipped. | D3 |

Roughly **3 PRs + a backfill follow-up + this amendment**; the weight is design risk on D2 (parsing/merge + the dual-write→cutover) and the D1 migration, not lines of code.

> **D1 status**: ✅ implemented. Migration **v13** adds a nullable `identity TEXT` (JSON) column to `relationships` ([`_migration_identity.py`](../../agents/memory/_migration_identity.py)) — additive, no PK rebuild (identity is per-row payload, not a key column, so it follows the simple v7 `ADD COLUMN` skeleton rather than the v11/v12 rebuild). The merge rule lives in the pure [`merge_identity`](../../agents/memory/relationship_types.py) (scalar last-writer-wins; `prefs` order-preserving union; an *absent* incoming value — `None` **or** empty string `""` — skipped, for both scalar overwrite and `prefs` items, so a failed/partial extraction can never null or pollute a stored field). `RelationshipMemory.upsert_identity` writes only the `identity` column (never the trust `notes`), creating the row at neutral trust if absent; `RelationshipMemory.get_identity` reads it with principal/epoch strict equality but **no session filter** — the cross-room property by construction (no `sessions="*"` sentinel; the room axis is not part of the tier's key). No prompt-facing change yet (rendering is D2). Coverage: [`test_relationship_identity.py`](../../tests/unit/python/test_relationship_identity.py) (merge purity, migration schema/idempotency, upsert merge/supersede, notes↔identity non-clobber, cross-room recall, principal/epoch isolation) + the bumped migration-count discipline pins in `test_episodic_memory_core.py` / `test_episodic_memory_retention.py`.

> **D2 status**: ✅ implemented. **Write-through** lives at the `store_note` tool boundary ([`agents/tools/builtin.py`](../../agents/tools/builtin.py) `create_memory_tools`, now wired with the relationship tier in [`persona.py`](../../agents/persona.py)): a `store_note(topic="contact:<id>")` call additionally upserts structured identity onto the cross-room relationship row — a **dual-write** (the legacy room-scoped note is still written; D3 drops it), best-effort so an identity failure never fails the note tool. The free-text `content` is structured by the pure, deterministic [`parse_identity_fields`](../../agents/memory/identity_parse.py) (keyed `Name:`/`Role:`/`Favorite …:` clauses + a narrow natural-name phrase; anything unkeyed preserved under `raw`) — **no LLM call** (decision D-1). The `other_participant_type` is the inbound sender's, bound task-locally for the event by [`sender_type.py`](../../agents/sender_type.py) (folded into [`request_scope.py`](../../agents/request_scope.py) alongside session/principal/epoch), so identity lands on the same relationship row the recall side later queries. **Render**: `RelationshipSummary` gains an `identity` field, attached by [`recall_relationship_summary`](../../agents/persona_runtime/relationship_section.py) via the dedicated session-filter-free `get_identity` read (not the session-scoped summary row read); `render_relationship_section` emits an `Identity:` line right after the header and now renders an **interaction-free** relationship when identity is present (the immediacy path), gating the per-session trust/count/last-seen/cadence lines on `interaction_count > 0`. Coverage: [`test_identity_write_through.py`](../../tests/unit/python/test_identity_write_through.py) (parser; sender-type binding; write-through dual-write / non-contact no-op / absent-handle + failure resilience / merge-supersede) + [`test_identity_render.py`](../../tests/unit/python/test_identity_render.py) (identity-only render, empty-without-identity regression guard, render-with-interactions, and the full write-through → cross-room → immediacy path through `create_persona_agent`).
>
> **D2 review hardening** (PR #554 deep-review): three fixes so the "nothing is lost / same row" contracts actually hold across turns. (1) The unkeyed `raw` remainder now *unions clause-wise* across notes ([`merge_identity`](../../agents/memory/relationship_types.py) `_IDENTITY_TEXT_UNION_KEYS`) instead of scalar last-writer-wins — a fact captured in one note ("Lives in Berlin") survives a later note that adds another, matching the parser's preserve-everything claim that D3 will rely on once the dual-written note safety net is dropped. (2) The greedy natural-name phrase ("I am …" / "call me …") is gated by `_is_namelike` (capitalized, ≤4 words) so prose is no longer mis-promoted to the load-bearing `name` field (it stays under `raw`); the explicit `Name:` key is still trusted as-is. (3) The sender participant type is resolved through one shared [`normalize_sender_type`](../../agents/sender_type.py) on both the write (scope-binding) and read (`recall_relationship_summary`) sides, so a whitespace-padded / non-string metadata value can no longer write under one type and read under another and silently miss the row.

> **D3 status**: ✅ implemented (retirement slice; backfill split to the D4 follow-up). The dual-write note is dropped — `store_note(topic="contact:<id>")` now routes through [`maybe_write_through_identity`](../../agents/tools/identity_write_through.py), which returns a bool: `True` (identity persisted) means the room-scoped note is **skipped**, so identity lives on the relationship tier alone and the cross-room recall seam cannot recur by construction. It returns `False` — and `store_note` falls through to the note write as a safety net — for a non-contact topic, an absent relationship handle, unparseable content, or an `upsert_identity` failure, so the model's data is never silently dropped (the dual-write's loss-avoidance, now contingent rather than permanent). `CONTACT_TOPIC_PREFIX` moves to that module (its only remaining consumer). The Option-A read carve-out is gone: `_notes_recall` deletes `_notes_session_clause` / `_recall_contact_notes` and its three helpers revert to `session_in_clause`; `NoteStore.recall_contact_notes` + the `EpisodicMemory` delegate are removed; `contact_section.py` is renamed `notes_section.py` (room-scoped recall only). Coverage: [`test_contact_note_room_scoped.py`](../../tests/unit/python/test_contact_note_room_scoped.py) (contact note not recalled cross-room; helper removed; same-room recall intact) + the updated [`test_identity_write_through.py`](../../tests/unit/python/test_identity_write_through.py) (identity-only write + the two fallback legs); the F-7 Option-A pin `test_recall_notes_scope.py` is removed. Cross-room identity remains proven by `test_identity_render.py::TestIdentityImmediacyCrossRoom` (relationship tier, unchanged).

> **D4 status**: ✅ implemented. Migration **v14** ([`_migration_identity_backfill.py`](../../agents/memory/_migration_identity_backfill.py)) is a one-time **data backfill** (not DDL — a callable handler reading/writing both tables with individual `db.execute` calls + one tail commit, never `executescript`): it reads every pre-cutover `contact:<id>` note in `created_at` order, structures each `content` through the same pure [`parse_identity_fields`](../../agents/memory/identity_parse.py), and [`merge_identity`](../../agents/memory/relationship_types.py)-merges the result onto the relationship row. **The participant-type design choice is resolved by not guessing the three axes a note *does* record.** A `contact:<id>` note carries `agent_id` (→ `participant_id`, `participant_type='agent'`), `principal_id`, `epoch_id` and — from the topic — `other_participant_id`; the *only* unrecorded relationship-PK axis is `other_participant_type`. So the backfill **inherits** it: identity merges onto *every* existing relationship row for the recorded `(agent_id, principal_id, epoch_id, other_id)` tuple, landing exactly where recall later anchors — no type invented. For an **orphan** note (no relationship row for that tuple), it creates one under `normalize_sender_type(None)` (`"agent"`, the shared default both write-through and recall fall back to), at neutral trust, interaction-free (the D2 immediacy render already handles interaction-free identity rows), so the identity is preserved without fabricating a tenant/epoch axis. Idempotent (re-merge of identical fields is stable; an orphan-created row is found as an existing row on replay), and a clean no-op when either table is absent or the v13 `identity` column has not landed. Coverage: [`test_identity_backfill.py`](../../tests/unit/python/test_identity_backfill.py) (inherit-onto-existing-type, chronological multi-note merge, non-clobber of a prior identity, trust/notes untouched, orphan→default-`agent` row, principal/epoch isolation, non-contact + unparseable + blank-id skips, double-run idempotency, empty-table no-op) + the bumped migration-count discipline pins in `test_episodic_memory_core.py` and the forward-compat probe in `test_episodic_memory_retention.py` (v14 → v15).

## Test strategy

- **Cross-room without notes.** Identity stated in room A is recalled in room B from the **relationship** tier, with no `contact:*` note involved (after D3, asserting the carve-out is gone).
- **Room isolation intact.** A topic fact (`"Persatrix is an environment…"`) stated in room A is **not** recalled in room B — confirming only identity crosses.
- **Immediacy.** Identity is available within the first conversation (write-through fires mid-turn), not only after interaction close.
- **Merge/supersede.** A later `name` supersedes an earlier one; a new preference unions; a trust-reason write does not clobber identity (and vice versa).
- **Boundary.** Principal/epoch equality still holds — cross-room never cross-tenant or cross-epoch (free from the PK, but pinned by a test).
- **Migration/backfill.** ✅ Pre-cutover `contact:*` notes land on the relationship `identity` after the v14 backfill — inheriting the existing row's `other_participant_type`, or creating a default-`agent` orphan row — and the pass is idempotent.

## Security considerations

The relationship PK already includes `principal_id` and `epoch_id`, so identity recall is cross-*room* only — never cross-tenant or cross-epoch — by construction, with no `sessions="*"` sentinel anywhere on the path. This is strictly **narrower** than the Option-A carve-out it replaces (which relaxed the room filter at the query layer); here the room axis simply isn't part of the tier's identity at all.

## Alternatives considered

- **Keep Option A indefinitely.** Functionally fine, but leaves scope decided by a topic-prefix string at the query layer and keeps the two-path structure that produced the seam. Rejected as the end state; retained as the interim (it ships today).
- **Route identity through the facts tier (RFC 0026).** Facts are session-scoped today and extraction only fires at interaction close — so this would need *both* a principal-scoped facts recall *and* an eager extractor to match today's immediacy. Higher risk than reusing the already-cross-room, already-injected relationship tier. Deferred (D-3).
- **Dedicated relationship-note tool / per-turn extractor.** See D-1 — more surface and/or latency for no gain over reusing the existing `store_note(contact:*)` signal.
