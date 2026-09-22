# RFC 0031 Amendment — The Relationship Row Reads the Same From Every Session

**Type**: amendment to [RFC 0031](0031-per-session-namespacing-channels.md) §D (Recall Semantics)
**Status**: ✅ Implemented — ratified by [#980](https://github.com/mkhomutov/Persatrix/pull/980).
**Author**: Maksim Khomutov
**Date**: 2026-09-22
**Trigger**: [ISSUE-0165](../issues/ISSUE-0165-relationship-hidden-outside-its-first-session.md) — a persona treated a peer as a stranger in every session except the one that first wrote the peer's relationship row.

---

## What changes

The relationship **row** is outside the four `sessions` modes of §D. Its key
has no session; the §C amendment calls it "cross-session shared, first-seen
tagged", with the per-session views drawn from `interactions`; and the §A
amendment has a relationship follow a peer across channels. Phase 2 PR 3
([#450](https://github.com/mkhomutov/Persatrix/pull/450)) filtered the row on
its first-seen `session_id` anyway. Once
[#459](https://github.com/mkhomutov/Persatrix/pull/459) gave every channel its
own session, that hid a peer seeded from `relationships:` config under a
start-up `PERSATRIX_SESSION_ID` in every channel, and a peer first met in one
session in every later one — along with that later session's own interactions
with them.

| | Before (Phase 2 PR 3) | After (this amendment) |
|---|---|---|
| `get_trust` | took `sessions`; row filtered on `session_id IN (active, legacy)` | no `sessions` argument; no session predicate |
| `get_relationship_summary` | row filtered, so a foreign-session row returned the "no relationship" summary before any history was read | row read unfiltered; `sessions` scopes the interaction history only |
| `get_all_relationships` | rows filtered on `r.session_id` | every row of the agent listed; `sessions` scopes each row's counted history |
| `get_identity` | already unfiltered | unchanged |

What stays per session is the interaction history — count, recent
interactions, first and last seen — as ISSUE-0080's Policy (C) set it.

## What does not change

- **Principal and epoch** stay unconditional strict equality with no
  carve-out, so the row still never crosses a tenant or a run.
- **The row's `session_id`** stays a first-seen record. Nothing reads it, and
  no read may take it as a filter key again; seeding still tags it so
  [MT-SESSION-001](../manual-tests/MT-SESSION-001.md) Step 7 holds.
- **No migration.** Rows already written are read the new way.
- **The tier stays outside the RFC 0037 §D gate**, per that RFC's Non-Goals,
  which exempt the *numeric* trust score. The tier's text — identity, and the
  note a trust change leaves — is held instead to the §C write-side rule:
  written only from a channel classified `internal` or below. `update_trust`,
  the only writer of that note, has no production caller and does not
  implement the rule, so a production writer must add it (or gate the read)
  first. `tests/unit/python/test_cross_session_read_sites.py` fails when such
  a caller appears.

## What a persona shows

Reading the row is not rendering it. The relationship block reaches a prompt
only where the persona has a closed interaction with that peer in that
channel, and only a DM records one
(`record_closed_interaction`). A configured trust level therefore reaches a
DM's prompt after its first close, and a group channel's prompt not at all —
the rule PR #60 set, that a configured score means nothing until the persona
has actually interacted.
