---
id: RFC-0035
title: Channel Membership Interval Ledger
summary: Add an append-only join/leave ledger to the channel store so the system can answer "was participant X a member of channel Y at time T" — the membership history a current-state-only `memberships` table cannot reconstruct after a remove or a rejoin.
type: architecture
status: implementing
author: Maksim Khomutov
created: 2026-05-16
target: v0.3.9
depends_on:
  - RFC-0011
---

# RFC 0035 — Channel Membership Interval Ledger

**Type**: architecture
**Status**: 🚧 Implementing
**Author**: Maksim Khomutov
**Date**: 2026-05-16
**Target**: v0.3.9
**Depends on**: RFC 0011 (Channels — provides the `memberships` table and the SQLite channel store this RFC extends)
**Relates to**: RFC 0009 (Agent Identity, Security & Sandboxing — the audit subsystem that records add/remove events at the orchestrator level), RFC 0036 (Persona Verbatim Message Recall — the first consumer; recall scoping is a SQL join against this ledger)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Why a separate ledger table](#a-why-a-separate-ledger-table)
  - [B. Schema — `membership_intervals`](#b-schema--membership_intervals)
  - [C. Write path — opening and closing intervals](#c-write-path--opening-and-closing-intervals)
  - [D. Backfill](#d-backfill)
  - [E. Query surface](#e-query-surface)
  - [F. The in-scope predicate](#f-the-in-scope-predicate)
  - [G. Channel deletion and DM creation](#g-channel-deletion-and-dm-creation)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

The channel store records membership as **current state only**. The
`memberships` table ([`internal/channels/sqlite_schema.go:100-107`](../../internal/channels/sqlite_schema.go#L100-L107))
has one row per `(channel_id, participant_id)` pair with a single
`joined_at` timestamp; `RemoveMember` **hard-deletes** that row
([`sqlite_query.go:130-175`](../../internal/channels/sqlite_query.go#L130-L175)).
There is no `left_at`, no event log, and no second row for a second
membership stint. A join → leave → rejoin cycle therefore leaves the
store unable to answer the only question that matters for
membership-scoped data access: **"was this participant a member of
this channel at time T?"**

This RFC adds an **append-only `membership_intervals` ledger** to the
channel store: one row per membership stint, `(channel_id,
participant_id, joined_at, left_at)`, with `left_at` NULL while the
stint is open. `AddMember` opens an interval, `RemoveMember` closes the
open one, and a one-time backfill seeds an open interval for every
participant currently in `memberships`. The existing `memberships`
table is untouched in shape and keeps serving as the hot-path
current-state projection.

This RFC ships **infrastructure only**. The first consumer —
membership-scoped verbatim message recall — is
[RFC 0036](0036-persona-message-recall.md), which joins message rows
against this ledger. RFC 0035 has no user-visible behaviour of its
own.

## Motivation

### The defect

`memberships` is a projection of *who is in a channel right now*. That
is the correct shape for the operations it was built for — the
publish-time membership probe
([`sqlite_messages.go:113`](../../internal/channels/sqlite_messages.go#L113)),
the response gate, `GetMembers`. It is the **wrong** shape for any
question about the past:

| Question | Answerable today? | Why not |
|---|---|---|
| Is X a member of Y now? | ✅ | `SELECT 1 FROM memberships …` |
| When did X join Y? | ⚠️ partially | `joined_at` holds the *first* join; a rejoin keeps the stale value (see below) |
| Was X a member of Y last Tuesday? | ❌ | No `left_at`; removal hard-deletes the row |
| X was removed and re-added — which messages fall in which stint? | ❌ | No record that two distinct stints ever existed |

The rejoin case is actively wrong, not merely absent. `AddMember` is
`INSERT … ON CONFLICT(channel_id, participant_id) DO NOTHING`
([`sqlite_query.go:301`](../../internal/channels/sqlite_query.go#L301)).
After a remove the row is gone, so a re-add inserts a fresh row with a
**new** `joined_at` — good. But while the participant is *still*
present, a redundant `AddMember` no-ops and keeps the original
`joined_at` — also fine. The gap is purely the lost history: nothing
records that the *first* stint ended, so the second stint's
`joined_at` is the only timestamp that survives and the first stint is
unrecoverable.

### Why this blocks RFC 0036

[RFC 0036](0036-persona-message-recall.md) lets a persona recall the
verbatim text of past conversations, scoped to **the channels and time
windows it had access to**. The canonical example: a group channel
exists, a persona is added, later removed, later re-added. The persona
should be able to recall messages from *both* of its membership
stints — and from neither the pre-join period nor the removal gap.

That scoping rule is a time-range filter: a message at timestamp `T`
is in scope iff some membership stint `[joined_at, left_at)` for that
`(channel, persona)` contains `T`. The filter is **unimplementable on
the current schema** — there are no stint boundaries to filter
against. The membership ledger is the missing substrate; RFC 0036 is
the consumer.

### Why not do nothing

Without the ledger, RFC 0036 has only two unacceptable options: scope
recall to *current* membership (a re-added persona loses its first
stint, a removed persona loses everything — fails the example
outright), or scope recall to *nothing but the current channel*
(no cross-stint recall at all). Both discard the exact history the
feature exists to surface. The ledger is small, additive, and reusable
beyond recall (audit reconstruction, future analytics); it is the
right substrate to build once.

## Goals

1. The channel store can answer "was participant X a member of channel
   Y at time T" for any T after this RFC ships, and for the *current*
   stint of every participant present at backfill time.
2. Every `AddMember` that admits a not-currently-present participant
   opens exactly one membership interval; every `RemoveMember` that
   removes a present participant closes exactly one.
3. A join → leave → rejoin cycle produces two distinct, non-overlapping
   closed/open intervals — the history the current schema destroys.
4. The `memberships` table keeps its exact current shape and remains
   the hot-path current-state source. No publish-path query changes.
5. The ledger is append-only: an interval row, once written, is only
   ever mutated by the single `left_at` close-out. No deletes except
   the `ON DELETE CASCADE` that fires with channel deletion.
6. At most one open interval (`left_at IS NULL`) exists per
   `(channel_id, participant_id)` at any time — enforced by a partial
   unique index, not by convention.
7. A new read method exposes a participant's intervals so RFC 0036 (and
   any later consumer) can apply the in-scope predicate without
   re-deriving it.

## Non-Goals

- **Message recall, FTS, or any persona-facing feature.** RFC 0035 is
  the substrate. [RFC 0036](0036-persona-message-recall.md) is the
  consumer and owns everything user-visible.
- **Reconstructing membership history that predates this RFC.** The
  backfill (§D) recovers one open interval per *currently present*
  participant. A participant removed before this RFC shipped left no
  `memberships` row and no recoverable join/leave timestamps; that
  history is permanently absent. Accepted — see §D.
- **Replacing the RFC 0009 audit trail.** The security audit subsystem
  already records add/remove events at the orchestrator level. The
  ledger is a *queryable, store-local* membership record optimised for
  time-range joins; it complements the audit log and does not subsume
  it. The two are written independently.
- **A membership-change event stream / webhook.** The ledger is a
  table, not a notification surface. Anything reactive is out of scope.
- **`respond_policy` history.** Policy changes (`SetMemberPolicy`) are
  not stint boundaries and are not recorded as intervals. Only
  presence (join/leave) is.
- **Operator-facing membership-history UI.** A read-only inspection
  endpoint is scoped as an optional Phase 2 deliverable, not a goal.
- **Session / epoch scoping.** The ledger is keyed at channel grain
  (`channel_id, participant_id`); a channel already belongs to a single
  `session_id` (migration v3) and a single `epoch_id` (migration v6),
  and membership is presence on that channel, so the ledger carries no
  `session_id` / `epoch_id` columns. How verbatim recall composes with
  those run/test-isolation axes — `epoch_id` added to
  `messages`/`channels` *after* this RFC was first drafted, `session_id`
  already present at draft but unaddressed by the original §C query — is
  a query-time concern owned by [RFC 0036 §C and Open Question #6](0036-persona-message-recall.md),
  which filters `messages.epoch_id` directly. The ledger needs no
  change for it.

## Design / Implementation

### A. Why a separate ledger table

The instinct is to add `left_at` to `memberships` directly. That does
not work: `memberships` has `PRIMARY KEY (channel_id, participant_id)`
— exactly one row per pair. A join → leave → rejoin history needs
*two* rows for one pair. Widening `memberships` to carry history would
mean dropping its primary key, which in turn breaks the
`ON CONFLICT(channel_id, participant_id)` upsert in `AddMember` and the
`(channel_id, participant_id)` point lookups on the publish hot path.

So `membership_intervals` is a **separate, additive table**:

- `memberships` stays exactly as it is — the current-state projection,
  one row per present member, read on every publish. Unchanged shape,
  unchanged queries, zero hot-path risk.
- `membership_intervals` is the append-only history — many rows per
  pair, never read on the publish path, joined only by the recall
  query (RFC 0036) and the new read method (§E).

The two are kept consistent transactionally: the same transaction that
inserts/deletes a `memberships` row also opens/closes the matching
interval (§C).

### B. Schema — `membership_intervals`

Channel-store schema migration **v9** (this RFC bumps the store from v8
to v9 — `channelStoreSchemaVersion = 9`,
[`sqlite_schema.go:86`](../../internal/channels/sqlite_schema.go#L86);
v9 was the next free slot as of 2026-06-17). A new `case 9:` arm in
`applyMigration` — in
[`internal/channels/sqlite_migrations.go`](../../internal/channels/sqlite_migrations.go),
the dedicated migration runner the channel store extracted out of
`sqlite_schema.go` after this RFC was first drafted — dispatches to a
`migrateV8ToV9` function carried in its own sibling file
[`internal/channels/sqlite_membership_intervals_migration.go`](../../internal/channels/sqlite_membership_intervals_migration.go),
a split that keeps `sqlite_migrations.go` under the repo's 500-line
file cap, following the
existing one-step-per-PR migration pattern; the const (in
`sqlite_schema.go`) is bumped to 9 and `user_version` is stamped inside
the migration's transaction (the L3 atomicity rule the file header
documents).

```sql
CREATE TABLE membership_intervals (
    id             INTEGER PRIMARY KEY,
    channel_id     TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    participant_id TEXT NOT NULL,
    joined_at      DATETIME NOT NULL,
    left_at        DATETIME            -- NULL while the stint is open
);

-- Time-range scan for the in-scope predicate (§F) and RFC 0036's join.
CREATE INDEX idx_membership_intervals_lookup
    ON membership_intervals(channel_id, participant_id, joined_at);

-- Goal 6: at most one open stint per (channel, participant).
CREATE UNIQUE INDEX ux_membership_intervals_open
    ON membership_intervals(channel_id, participant_id)
    WHERE left_at IS NULL;
```

Notes:

- `id INTEGER PRIMARY KEY` is the rowid alias — a compact surrogate
  key. No `AUTOINCREMENT` is needed: nothing depends on id ordering or
  non-reuse (the ledger is scanned by `joined_at` / `left_at`, never by
  id), and a plain rowid can be reclaimed after an `ON DELETE CASCADE`,
  which is harmless for a time-keyed table.
- `joined_at` / `left_at` are `DATETIME`, matching `memberships.joined_at`
  and `messages.timestamp`. RFC 0036's join compares
  `membership_intervals.joined_at` against `messages.timestamp`
  directly; keeping the encodings identical keeps that comparison
  honest. (The `sessions` table chose `REAL` for unrelated ergonomics;
  this table deliberately does not — it is joined against `DATETIME`
  message timestamps.)
- `ON DELETE CASCADE` mirrors `memberships` and `messages`: deleting a
  channel discards its intervals, consistent with discarding its
  messages (§G).
- `participant_id` has no foreign key, exactly like `memberships`
  (participants are not a table; the column carries agent ids and
  human/CLI ids alike).
- The partial unique index `ux_membership_intervals_open` is the
  load-bearing invariant guard: a double-open (a bug in the write
  path) fails the INSERT loudly instead of silently corrupting the
  history.

### C. Write path — opening and closing intervals

Four call sites mutate `memberships` today; each gets a matching
interval write **in the same transaction**. (An earlier draft of this
section counted three — `AddMember` / `RemoveMember` / `GetOrCreateDM` —
and missed `CreateChannelWithMembers`, the atomic create-with-members
path the REST create handler and config reconcile use. It is the
*primary* way config-declared channels and their members enter the
store, so omitting its interval-open would leave every config persona
with no interval — silently breaking RFC 0036 recall for them — and make
a later `RemoveMember` trip the divergence guard below, a reachable REST
500. The fourth hook closes that gap; it is implemented and tested with
the other three.)

**`AddMember`** ([`sqlite_query.go:283`](../../internal/channels/sqlite_query.go#L283))
— currently a single `INSERT … ON CONFLICT DO NOTHING` with no
transaction. It must now (a) run in a transaction and (b) open an
interval **only when it actually inserted a `memberships` row**:

- `INSERT … ON CONFLICT DO NOTHING`, then check `RowsAffected()`.
- `RowsAffected() == 1` → a genuinely new (or post-removal re-add)
  membership. Insert an open interval
  `(channel_id, participant_id, joined_at = now, left_at = NULL)`.
  `now` is the same `time.Now().UTC()` used for `memberships.joined_at`,
  so the projection and the ledger agree.
- `RowsAffected() == 0` → the participant was already present; the
  `ON CONFLICT` no-op fired. No interval is opened — the open interval
  from the *existing* stint is still correct. (The `ux_…_open` index
  would reject a second one anyway; the `RowsAffected` check makes the
  intent explicit rather than relying on the index to catch it.)

`AddMember` stays idempotent — a redundant call still no-ops cleanly,
on both tables.

**`RemoveMember`** ([`sqlite_query.go:130`](../../internal/channels/sqlite_query.go#L130))
— already transactional. On the `RowsAffected() == 1` success path
(the `memberships` row was deleted), add, inside the same transaction:

```sql
UPDATE membership_intervals
   SET left_at = ?               -- time.Now().UTC()
 WHERE channel_id = ? AND participant_id = ? AND left_at IS NULL;
```

The backfill (§D) guarantees every participant present at v9 has an
open interval, and the `AddMember` hook guarantees every post-v9 join
opens one, so this `UPDATE` finds exactly one row to close on the
success path. If it instead closes **zero** rows — a `memberships` row
existed with no matching open interval — the open-interval invariant
(Goal 6) has been violated. `RemoveMember` MUST treat that as a hard
error and roll the transaction back rather than commit silently: the
projection and the ledger have diverged, and a silent commit would
leave a removed participant with an interval that never closes (a
data-*exposure* bug for RFC 0036). This mirrors the loud-failure
posture of the `AddMember` path, where the `ux_…_open` index already
rejects a spurious double-open by failing the INSERT. The `n == 0`
branch (member not present) closes nothing — that is the expected
no-op, not the invariant violation.

**`GetOrCreateDM`** ([`sqlite_query.go:406`](../../internal/channels/sqlite_query.go#L406))
— inserts the two DM participants' `memberships` rows directly inside
its own transaction, bypassing `AddMember`. It must open an interval
for each of the two participants in that same transaction, with
`joined_at = now` (the DM's creation time). DM membership is never
removed in normal operation, so these intervals stay open for the
life of the channel.

**`CreateChannelWithMembers`** ([`sqlite.go:328`](../../internal/channels/sqlite.go#L328))
— the atomic create-with-members path (REST create handler + config
reconcile). Already transactional. After each member's
`INSERT … ON CONFLICT DO NOTHING`, it opens an interval **only when it
actually inserted a row** (`RowsAffected() == 1`), exactly like
`AddMember`, with `joined_at` equal to the member's `memberships.joined_at`.
The `RowsAffected` gate makes a participant repeated in the input slice a
clean no-op on both tables. This is the hook that keeps the ledger
complete for config-declared channels — the common case at boot.

All four writes are transactional with their `memberships` mutation:
either both the projection and the ledger move, or neither does.

### D. Backfill

`migrateV8ToV9` seeds the ledger from the current `memberships` state
so the table is correct the instant it exists:

```sql
INSERT INTO membership_intervals (channel_id, participant_id, joined_at, left_at)
SELECT channel_id, participant_id, joined_at, NULL
  FROM memberships;
```

Every participant currently in a channel gets exactly one **open**
interval starting at their recorded `joined_at`. This satisfies
`ux_membership_intervals_open` (one open interval per pair) and means
the in-scope predicate (§F) is immediately correct for all present
members.

**The accepted gap.** A participant removed *before* v9 ships left no
`memberships` row, so the backfill cannot see them — that stint is
unrecoverable. Likewise, for a participant whose `joined_at` predates
their *actual* first interaction (none exist today, but defensively:
the backfill trusts `memberships.joined_at`), the open interval starts
at the recorded join. Consequence for RFC 0036: a persona that joined
a channel, was removed, and is re-added all *before* this RFC ships
can recall only from its current (re-added) stint forward. This is
documented as a known limitation of recall, not a correctness bug —
the ledger is exact for every membership change from v9 onward.

### E. Query surface

One new read method on the channel store, used by RFC 0036 and any
later consumer:

```go
// MembershipInterval is one row of the membership_intervals ledger.
// LeftAt is the zero Time while the interval is open.
type MembershipInterval struct {
    ChannelID     string
    ParticipantID string
    JoinedAt      time.Time
    LeftAt        time.Time // zero ⇒ open
}

// GetMembershipIntervals returns every interval for (channelID,
// participantID), ordered by joined_at ascending.
func (s *sqliteStore) GetMembershipIntervals(
    ctx context.Context, channelID, participantID string,
) ([]MembershipInterval, error)
```

RFC 0036's recall query does **not** go through this method — it joins
`membership_intervals` directly in SQL, server-side, co-located with
`messages` (see RFC 0036 §C). `GetMembershipIntervals` exists for
callers that need the interval list as data: the in-scope predicate
helper (§F), tests, and the optional inspection endpoint (Phase 2).

A second convenience method, `GetAccessibleChannels(ctx,
participantID) []string` (distinct `channel_id`s the participant has
ever had an interval in), is a Phase 2 nicety — RFC 0036's tool
defaults to searching all accessible channels via the SQL join and
does not need an explicit channel list, so this is not load-bearing.

### F. The in-scope predicate

The single rule every consumer applies. A message at timestamp `T` in
channel `C` is **in scope** for participant `P` iff some interval for
`(C, P)` satisfies:

```
joined_at <= T  AND  (left_at IS NULL OR T < left_at)
```

The interval is **half-open** — `[joined_at, left_at)`. A message
published at the exact instant of a join is in scope; one published at
the exact instant of a leave is not. This makes back-to-back
leave-then-rejoin unambiguous: the closing `left_at` of stint *n* and
the opening `joined_at` of stint *n+1* can be equal without a message
falling into both or neither.

A Go helper (`InScope(intervals []MembershipInterval, t time.Time) bool`)
pins the predicate in one place; RFC 0036's SQL `EXISTS` clause is the
same predicate expressed as a join. Both are tested against the same
table of join/leave/rejoin fixtures.

### G. Channel deletion and DM creation

- **Channel deletion** — `DeleteChannel` hard-deletes the `channels`
  row; `ON DELETE CASCADE` removes the channel's `memberships`,
  `messages`, *and now* `membership_intervals` rows together. A deleted
  channel has no messages and no intervals — consistently
  unrecallable. No special handling.
- **DM creation** — covered in §C: `GetOrCreateDM` opens intervals for
  both participants at DM-creation time.
- **Thread channels** — threads carry memberships like any channel;
  the same `AddMember`/`RemoveMember` hooks apply with no thread-specific
  logic.

## Security Considerations

- **No new external surface.** Phase 1 adds no REST endpoint, no proto
  field, no agent-facing API. The ledger is an internal store table
  written by existing membership operations. The attack surface is
  unchanged from RFC 0011's channel store.
- **Append-only integrity.** Interval rows are only ever written by
  the three transactional hooks in §C and mutated only by the single
  `left_at` close-out. The partial unique index
  `ux_membership_intervals_open` makes a double-open a hard failure.
  There is no code path that deletes an interval except channel-level
  `ON DELETE CASCADE`.
- **Transactional consistency.** Each interval write rides the same
  transaction as its `memberships` mutation. A crash mid-operation
  rolls back both — the ledger cannot diverge from the current-state
  projection. The
  `TestAddMember_OpensInterval_Atomically` /
  `TestRemoveMember_ClosesInterval_Atomically` tests pin this.
- **The ledger underpins an access-control decision.** RFC 0036 uses
  this table to decide which messages a persona may recall. An
  incorrect interval (a missed close, a spurious open) becomes a
  data-exposure or data-suppression bug *there*. That is why §C makes
  every write transactional and §B enforces the open-interval
  invariant with an index rather than trusting the write path — the
  correctness bar for this table is the bar for an authorization
  record, and the consumer RFC's Security Considerations cross-cites
  this section.
- **Optional Phase 2 inspection endpoint.** If the read-only
  membership-history endpoint ships, it exposes who-was-where-when.
  That is no more sensitive than `GetMembers` already is, but it MUST
  inherit whatever authentication the channel REST surface carries at
  that time (RFC 0009) and is explicitly gated on that — see Open
  Questions #2.

## Phased Implementation Plan

### Phase 1: Ledger, write hooks, backfill

The load-bearing phase — everything RFC 0036 depends on.

1. Channel-store schema migration **v9**: `migrateV8ToV9` creates
   `membership_intervals`, `idx_membership_intervals_lookup`, and
   `ux_membership_intervals_open`; runs the §D backfill; stamps
   `user_version = 9` inside its transaction. Bump
   `channelStoreSchemaVersion` to 9 and add the `case 9:` arm (the
   migration body lands in `sqlite_migrations.go`, the const bump in
   `sqlite_schema.go`).
2. `MembershipInterval` struct in `channels.go`; `GetMembershipIntervals`
   read method; `InScope` predicate helper.
3. Write hooks (§C): `AddMember` becomes transactional and opens an
   interval on a real insert; `RemoveMember` closes the open interval;
   `GetOrCreateDM` opens intervals for both DM participants.
4. `ChannelStore` interface gains `GetMembershipIntervals`.
5. Unit + migration tests per the Test Strategy.

Dependencies: none beyond the existing RFC 0011 channel store.

### Phase 2: Operator inspection endpoint (optional)

A read-only `GET` endpoint surfacing a participant's membership
history for operator debugging and audit reconstruction, plus the
`GetAccessibleChannels` convenience method. Independently reviewable;
**not** a dependency of RFC 0036 (which joins the table server-side).
May be cut or deferred without affecting recall.

Dependencies: Phase 1; the endpoint's auth gate depends on Open
Question #2.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Go orchestrator | `internal/channels/sqlite_membership_intervals_migration.go` (new) + `case 9:` arm in `internal/channels/sqlite_migrations.go` | `migrateV8ToV9`: `membership_intervals` table, indexes, §D backfill — the function lives in the sibling file so `sqlite_migrations.go` stays under the 500-line cap |
| Go orchestrator | `internal/channels/sqlite_schema.go` | Bump `channelStoreSchemaVersion` to 9; migration-history header comment |
| Go orchestrator | `internal/channels/sqlite_query.go` | `AddMember` (transactional, opens interval), `RemoveMember` (closes interval), `GetOrCreateDM` (opens DM intervals), new `GetMembershipIntervals` |
| Go orchestrator | `internal/channels/channels.go` | `MembershipInterval` type, `InScope` helper, `ChannelStore` interface method |
| Go orchestrator | `internal/server/channel_handlers.go`, `channel_types.go` | Phase 2 only — inspection endpoint |
| Tests | `internal/channels/sqlite_membership_intervals_test.go` (new) | Migration, backfill, write-hook, predicate tests |
| Docs | `docs/diagrams/memory-architecture.md` | Note the ledger as the substrate for membership-scoped access |

## Test Strategy

- **Unit tests**:
  - `AddMember` on a new participant opens exactly one interval;
    `joined_at` matches the `memberships` row.
  - A redundant `AddMember` on a present participant opens **no**
    second interval (`RowsAffected == 0` path).
  - `RemoveMember` closes the open interval; `left_at` is set; the
    interval is otherwise unchanged.
  - Join → leave → rejoin yields two intervals: one closed
    `[t0, t1)`, one open `[t2, NULL)`, non-overlapping.
  - `ux_membership_intervals_open` rejects a synthetic double-open.
  - `InScope` against a join/leave/rejoin fixture: messages before the
    first join, inside the gap, and after re-add classify correctly;
    boundary instants (`= joined_at`, `= left_at`) follow the half-open
    rule.
  - `GetOrCreateDM` opens one interval per DM participant.
  - `DeleteChannel` cascades intervals away.
- **Migration tests**:
  - v8 → v9 backfill: every `memberships` row produces exactly one
    open interval; `user_version` is stamped inside the migration
    transaction (extend the existing
    `Test…_StampsUserVersionInTransaction` pattern).
  - v9 migration is idempotent on reopen — no duplicate intervals, no
    resurrected indexes.
- **Integration tests**:
  - Via the channel store API: add, remove, re-add a participant; assert
    `GetMembershipIntervals` returns the two-stint history.
- **Manual tests**: none — RFC 0035 has no user-visible behaviour.
  RFC 0036's manual tests exercise the ledger transitively.

## Open Questions

1. **Backfill `joined_at` fidelity.** The backfill trusts
   `memberships.joined_at` as the open interval's start. For a
   participant re-added before v9 (where `joined_at` is the *latest*
   re-add, the earlier stint already lost), this is the best available
   value and the open interval is correct from that point forward.
   No action proposed — the §D "accepted gap" framing covers it. Listed
   for reviewer visibility.

2. **Phase 2 inspection-endpoint authentication.** A read-only
   membership-history endpoint should not ship more permissively than
   the surrounding channel REST surface. RFC 0011's publish/history
   endpoints are currently unauthenticated at the deployment's trust
   level (single-tenant local). Proposed resolution: the inspection
   endpoint inherits whatever auth RFC 0009 lands for the channel REST
   surface, and until then matches the existing surface's trust level.
   Since Phase 2 is optional, this question does not gate Phase 1.

3. **Pruning closed intervals.** The ledger grows by one row per
   membership change, forever. For the foreseeable workload (a handful
   of channels, infrequent membership churn) this is negligible. A
   retention policy for very old *closed* intervals can be revisited
   when observed-workload data shows the table size matters — not
   before. No pruning ships in this RFC.

## Decision / Next Steps

1. Land this RFC. It has no dependency beyond the existing RFC 0011
   channel store and can begin immediately.
2. Implement Phase 1. It is the hard dependency for
   [RFC 0036](0036-persona-message-recall.md) — RFC 0036 Phase 1
   cannot start until the `membership_intervals` table and its write
   hooks exist.
3. Phase 2 (inspection endpoint) is optional and may be scheduled
   independently or dropped; it does not block RFC 0036.
4. Regenerate `docs/rfcs/INDEX.md` via `make rfcs` (the index is
   auto-generated from front-matter — do not hand-edit).

## Related Documentation

- [RFC 0011 — Channels & Internal Agent Messaging](0011-channels-bridges.md) — the channel store and `memberships` table this RFC extends.
- [RFC 0036 — Persona Verbatim Message Recall](0036-persona-message-recall.md) — the consumer; recall scoping joins this ledger.
- [RFC 0009 — Agent Identity, Security & Sandboxing](0009-security-sandboxing.md) — the audit subsystem that records add/remove events independently of this ledger.
- [RFC 0034 — Persona Conversational Working Memory](0034-persona-conversational-working-memory.md) — the conversation window RFC 0036 retrofits with a membership filter.
- [Architecture spec](../ai-agents-orchestration-spec.md), [Extension spec](../persatrix-extension-spec.md).
