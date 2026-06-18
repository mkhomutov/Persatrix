# RFC 0035 — PR Implementation Plan (Phase 1 ledger + Phase 2 inspection endpoint — v0.3.9 scope)

**RFC**: [0035-channel-membership-interval-ledger.md](0035-channel-membership-interval-ledger.md)
**Created**: 2026-06-17
**Branch prefix**: `feature/v039-rfc0035-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.9-plan.md Phase 1 — workstream 1a (RFC 0035 PR plan — row 1a)](../v0.3.9-plan.md#phase-1--implement-the-ledger--recall)

---

## Overview

RFC 0035 adds an **append-only `membership_intervals` ledger** to the channel store — one row per membership stint `(channel_id, participant_id, joined_at, left_at)` — so the store can answer the one question the current-state-only `memberships` table cannot: *"was participant X a member of channel Y at time T?"* `AddMember` opens an interval, `RemoveMember` closes the open one, `GetOrCreateDM` opens one per DM participant, and a v8→v9 migration backfills one open interval per current `memberships` row. The `memberships` projection keeps its exact shape and stays the publish hot path.

This RFC ships **infrastructure only — no user-visible behaviour.** It is the load-bearing substrate for [RFC 0036](0036-persona-message-recall.md) verbatim message recall, whose scoped-search query joins this ledger so the access rule is enforced in SQL. Per the [v0.3.9 master plan](../v0.3.9-plan.md), **RFC 0035 must merge before RFC 0036 Phase 1 begins** — the v9-before-v10 migration ordering is the operational expression of that hard dependency.

The work splits into **5 PRs**:

- **PR 1** lands the dormant schema: migration v9 (table, indexes, backfill) + migration tests. No Go read/write surface yet.
- **PR 2** adds the read surface: the `MembershipInterval` type, `GetMembershipIntervals`, the `InScope` predicate helper, and the `ChannelStore` interface method — queryable against the backfilled snapshot.
- **PR 3** is the **load-bearing correctness PR**: the three transactional write hooks that keep the ledger live and exact, plus the add/remove/re-add lifecycle tests. Everything RFC 0036 depends on exists after PR 3 merges.
- **PR 4** is RFC 0035 **Phase 2** (optional, cut-tolerant): the read-only operator inspection endpoint + `GetAccessibleChannels`.
- **PR 5** is review follow-ups + closeout (status flips, progress-overview fill).

**Prerequisites**: the channel store at **v8** before PR 1 (✅ — `channelStoreSchemaVersion` [`sqlite_schema.go:86`](../../internal/channels/sqlite_schema.go#L86), which PR 1 bumps 8 → 9); the [v0.3.9 master plan Phase 0](../v0.3.9-plan.md#phase-0--this-pr) merged so the RFC 0035 Master-Index row exists (✅ — [#669](https://github.com/mkhomutov/Persatrix/pull/669)). No dependency beyond the existing RFC 0011 channel store.

### Open-question resolutions locked at plan-authoring time

The three RFC 0035 open questions and the two [v0.3.9-plan](../v0.3.9-plan.md#open-question-status) scope locks that touch this workstream resolve here. The RFC's [Open Questions](0035-channel-membership-interval-ledger.md#open-questions) already carry the matching dispositions.

- **[OQ #1](0035-channel-membership-interval-ledger.md#open-questions) — backfill `joined_at` fidelity: no action.** The §D backfill trusts `memberships.joined_at` as the open interval's start. For a participant re-added *before* v9 shipped (earlier stint already lost), this is the best available value and the open interval is correct from that point forward. The "accepted gap" framing in [RFC §D](0035-channel-membership-interval-ledger.md#d-backfill) covers it; documented as a known recall limitation in RFC 0036's CHANGELOG notes, not a bug. **No code; no test obligation beyond asserting the backfill seeds exactly one open interval per current row.**
- **[OQ #2](0035-channel-membership-interval-ledger.md#open-questions) — Phase 2 inspection-endpoint authentication: inherit the channel-surface trust level.** The read-only membership-history `GET` endpoint (PR 4) must not ship more permissively than the surrounding channel REST surface, which is unauthenticated at the current single-tenant trust level; it inherits RFC 0009's auth model when that lands. This does **not** gate PR 1–3 (Phase 1 adds no external surface). Mirrors [RFC 0036 §OQ-1](0036-persona-message-recall.md#open-questions).
- **[OQ #3](0035-channel-membership-interval-ledger.md#open-questions) — pruning closed intervals: no pruning ships.** Negligible at the foreseeable workload; revisited only when observed-workload data shows the table size matters. Deferred post-soak.
- **[v0.3.9-plan scope lock] — RFC 0035 Phase 2 is IN, sequenced last, cut-tolerant.** PR 4 ships in v0.3.9, but it is **not** a recall dependency (RFC 0036 joins the ledger server-side, not via the endpoint). If the cut tightens it is the first thing to drop without touching the recall headline — see [§Risk and Mitigations](#risk-and-mitigations) and [v0.3.9-plan §Candidate fold-ins](../v0.3.9-plan.md#candidate-fold-ins-maintainer-decision).
- **Session / epoch axes are RFC 0036's concern, not this ledger's.** The ledger is keyed at channel grain and carries **no** `session_id` / `epoch_id` columns ([RFC §Non-Goals](0035-channel-membership-interval-ledger.md#non-goals)) — a channel already belongs to one session (v3) and one epoch (v6), and membership is presence on the channel. The [§OQ-6 epoch-hard-filter / session-span lock](../v0.3.9-plan.md#open-question-status) is applied at RFC 0036's `messages.epoch_id` query predicate; **this ledger needs no change for it**, and no PR here touches those axes.

### Sequencing

**Recommended merge order**: **PR 1 → PR 2 → PR 3 → PR 4 → PR 5.**

PR 1 lands the table as additive, dormant schema (the channel store's [migration runner](../../internal/channels/sqlite_migrations.go) header documents the one-`migrateV(N-1)ToVN`-per-PR pattern). PR 2 makes it queryable; its tests assert reads of the backfilled snapshot (all intervals open). PR 3 wires the three write hooks so the ledger goes *live and correct* — its tests need `GetMembershipIntervals` from PR 2 to assert the two-stint history, which is why read lands before write. PR 4 (Phase 2) is independent of nothing but PR 1–3 and is separately reviewable; it may be deferred or dropped. PR 5 closes out.

Between PR 1 and PR 3 merging, `main` carries a backfilled-but-not-yet-maintained ledger. That is a coherent additive state: the table reflects the current membership snapshot, has no consumer (RFC 0036 has not landed), and regresses no existing behaviour. The write hooks (PR 3) make it track changes going forward; the partial unique index (PR 1) guards the invariant from the moment the table exists.

This plan runs **before** the [RFC 0036 PR plan](0036-pr-plan.md): RFC 0036 Phase 1 cannot begin until PR 3 here (the `membership_intervals` table *and* its write hooks) has merged.

---

## Dependency Graph

```
PR 1 (Migration v9: membership_intervals table + indexes + §D backfill; migration tests)
  ↓
PR 2 (Read surface: MembershipInterval struct, GetMembershipIntervals, InScope helper,
      ChannelStore interface method; read/predicate tests against the backfilled snapshot)
  ↓
PR 3 (Write hooks: AddMember transactional + open, RemoveMember close (rollback on zero),
      GetOrCreateDM open-per-DM-participant; add/remove/re-add lifecycle + integration tests)
  ↓                                          ← RFC 0036 Phase 1 may begin once PR 3 merges
PR 4 (Phase 2, optional/cut-tolerant: read-only GET membership-history endpoint
      + GetAccessibleChannels; handler tests)
  ↓
PR 5 (Review follow-ups + Phase 1/2 closeout — RFC status → Implemented; ROADMAP/progress flips)
```

PR 1 is pure additive schema; no Go behaviour change. PR 3 is the hard dependency for RFC 0036.

---

## PR Sequence

### PR 1: `feature/v039-rfc0035-migration` — Channel-Store Migration v9 (`membership_intervals`)

**Depends on**: Nothing (v8 channel store).
**Purpose**: Land the `membership_intervals` table, its two indexes, and the §D backfill as channel-store schema migration **v9** — dormant additive schema, no Go read/write surface. Landing the migration in its own PR keeps it bisectable and matches the runner's one-step-per-PR convention.

#### Scope

| File | Change |
|------|--------|
| [`internal/channels/sqlite_migrations.go`](../../internal/channels/sqlite_migrations.go) | Add a `case 9:` arm in `applyMigration` dispatching to `migrateV8ToV9` — which lives in the sibling file `sqlite_membership_intervals_migration.go` to keep this file under the 500-line cap. That function runs in one transaction: `CREATE TABLE membership_intervals`, `CREATE INDEX idx_membership_intervals_lookup`, `CREATE UNIQUE INDEX ux_membership_intervals_open … WHERE left_at IS NULL`, the §D backfill `INSERT … SELECT … FROM memberships`, then `stampUserVersionTx(tx, 9)` as the final statement before `tx.Commit()` (the L3 atomicity rule the file header documents). A header comment block mirrors `migrateV6ToV7` / `migrateV7ToV8` style, naming the table as RFC 0035's substrate and the partial unique index as the open-interval invariant guard. |
| [`internal/channels/sqlite_schema.go`](../../internal/channels/sqlite_schema.go#L86) | Bump `channelStoreSchemaVersion` 8 → 9; extend the migration-history header comment with the v9 line. |
| `internal/channels/sqlite_membership_intervals_migration_test.go` (new) | Migration tests (see below), modelled on [`sqlite_epoch_migration_test.go`](../../internal/channels/sqlite_epoch_migration_test.go) / [`sqlite_schema_user_version_test.go`](../../internal/channels/sqlite_schema_user_version_test.go). |

#### Schema (per [RFC §B](0035-channel-membership-interval-ledger.md#b-schema--membership_intervals))

```sql
CREATE TABLE membership_intervals (
    id             INTEGER PRIMARY KEY,           -- rowid alias; append-only, no AUTOINCREMENT
    channel_id     TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    participant_id TEXT NOT NULL,                 -- no FK, exactly like memberships
    joined_at      DATETIME NOT NULL,
    left_at        DATETIME                       -- NULL while the stint is open
);
CREATE INDEX idx_membership_intervals_lookup
    ON membership_intervals(channel_id, participant_id, joined_at);
CREATE UNIQUE INDEX ux_membership_intervals_open
    ON membership_intervals(channel_id, participant_id) WHERE left_at IS NULL;
```

Backfill (§D), seeding one **open** interval per current member:

```sql
INSERT INTO membership_intervals (channel_id, participant_id, joined_at, left_at)
SELECT channel_id, participant_id, joined_at, NULL FROM memberships;
```

#### Key implementation details

- **`DATETIME`, not `REAL`.** `joined_at` / `left_at` match `memberships.joined_at` and `messages.timestamp` so RFC 0036's join compares `membership_intervals.joined_at` against `messages.timestamp` directly. The `sessions` table's `REAL` choice is deliberately *not* mirrored ([RFC §B note](0035-channel-membership-interval-ledger.md#b-schema--membership_intervals)).
- **`ON DELETE CASCADE`** mirrors `memberships` / `messages`: deleting a channel discards its intervals together with its messages — consistently unrecallable (§G). Foreign keys must be enabled on the connection (they already are for the channel store); the migration relies on the existing PRAGMA, not a per-migration toggle.
- **The partial unique index is the invariant guard**, not a convention — a double-open fails the INSERT loudly rather than silently corrupting history. PR 3's write path leans on it.
- **Additive, no rebuild.** No existing table or index is touched; every `memberships` / `messages` query is byte-identical post-v9. A single-world deployment with no membership churn reads back unchanged save for the new (backfilled) table.

#### Tests

- **Backfill exactness**: a store with N `memberships` rows produces exactly N intervals, all open (`left_at IS NULL`), each `joined_at` equal to its source row's `joined_at`; zero `memberships` ⇒ zero intervals.
- **`user_version` stamped inside the migration transaction** — extend the existing `Test…StampsUserVersionInTransaction` pattern ([`sqlite_schema_user_version_test.go`](../../internal/channels/sqlite_schema_user_version_test.go)); a forced failure after the table create rolls back the version bump.
- **Idempotent on reopen**: opening a v9 store again runs no migration, leaves no duplicate intervals, resurrects no indexes.
- **Invariant index present**: a synthetic second open-interval INSERT for the same `(channel_id, participant_id)` fails with a UNIQUE violation; an INSERT with a non-NULL `left_at` for an already-open pair succeeds (the index is partial).
- **Cascade**: deleting a channel removes its `membership_intervals` rows (assert via a direct count after `DeleteChannel`).

#### PR checklist

- [ ] `go test ./internal/channels/ -run 'MembershipInterval|Migration|UserVersion' -count=1` passes.
- [ ] `make test` (Go lane) green; `go vet ./internal/channels/...` clean.
- [ ] `channelStoreSchemaVersion == 9`; migration-history header updated.
- [ ] No edits to `sqlite_query.go` / `channels.go` (read/write surface is PR 2/PR 3).
- [ ] No new REST endpoint, proto field, or agent-facing surface (Phase 1 adds none — [RFC §Security](0035-channel-membership-interval-ledger.md#security-considerations)).
- [ ] [v0.3.9-plan Master Progress Overview](../v0.3.9-plan.md#master-progress-overview) row 1a → 🔄 In progress; RFC 0035 Master-Index status note `📋 Proposed → 🚧 Implementing` (and RFC front-matter `status: proposed → implementing`, then `make rfcs` to regenerate [INDEX.md](INDEX.md)) on this PR opening.

---

### PR 2: `feature/v039-rfc0035-read-surface` — `MembershipInterval`, `GetMembershipIntervals`, `InScope`

**Depends on**: PR 1 merged.
**Purpose**: Make the ledger queryable from Go — the `MembershipInterval` type, the `GetMembershipIntervals` read method, the `InScope` predicate helper, and the `ChannelStore` interface method. No write hooks yet; tests assert reads of the backfilled snapshot.

#### Scope

| File | Change |
|------|--------|
| `internal/channels/membership_intervals.go` (new) | The `MembershipInterval` struct (`ChannelID`, `ParticipantID`, `JoinedAt time.Time`, `LeftAt time.Time` — zero ⇒ open) and the `InScope(intervals []MembershipInterval, t time.Time) bool` helper expressing the half-open `[joined_at, left_at)` predicate (§F). A **new file** rather than `channels.go`, which sits at 499 lines — one line under the [`file_size.py --strict`](../../scripts/checks/file_size.py) 500-line code cap — so the type + helper cannot land there without evicting an unrelated line. |
| `internal/channels/sqlite_membership_intervals.go` (new) | The `(*sqliteStore).GetMembershipIntervals(ctx, channelID, participantID) ([]MembershipInterval, error)` read method — `SELECT … ORDER BY joined_at ASC`, scanning `left_at` through a `sql.NullTime` into the zero-`Time`-when-open convention. A **new file** rather than `sqlite_query.go` (465 lines) to stay clear of the cap and to co-locate the ledger read with PR 3's ledger writes. |
| [`internal/channels/store.go`](../../internal/channels/store.go) | `ChannelStore` interface gains `GetMembershipIntervals(ctx context.Context, channelID, participantID string) ([]MembershipInterval, error)` — the interface is defined here, **not** in `channels.go`. One line; `store.go` is 197 lines, so no cap concern. |
| `internal/channels/membership_intervals_test.go` (new) | Read-method + `InScope` unit tests (see below). |

#### Key implementation details

- **`InScope` is the single source of the predicate.** RFC 0036's SQL `EXISTS` clause is the *same* predicate as a join; both are tested against the same join/leave/rejoin fixture table ([RFC §F](0035-channel-membership-interval-ledger.md#f-the-in-scope-predicate)). Half-open means a message at the exact join instant is in scope, one at the exact leave instant is not — so back-to-back leave-then-rejoin is unambiguous.
- **`LeftAt` zero-value = open.** The struct uses `time.Time` (not `*time.Time` or `sql.NullTime`) for ergonomics; `GetMembershipIntervals` maps a NULL `left_at` to the zero `Time`, and `InScope` treats `LeftAt.IsZero()` as "still open". Documented on the struct.
- **`GetMembershipIntervals` is for callers that need the list as data** — the `InScope` helper, tests, and PR 4's inspection endpoint. RFC 0036's recall query does **not** route through it; it joins `membership_intervals` directly in SQL ([RFC §E](0035-channel-membership-interval-ledger.md#e-query-surface)). Noted so a reviewer does not expect the recall path to call this method.
- **`GetAccessibleChannels` is deferred to PR 4** — it is a Phase 2 convenience, not load-bearing (RFC 0036's tool defaults to "all accessible channels" via the SQL join), so it lands with the endpoint that needs it.

#### Tests

- `GetMembershipIntervals` on the backfilled store returns one open interval per current member, ordered by `joined_at` ascending; an unknown `(channel, participant)` returns an empty slice, not an error.
- `InScope` against a hand-built join → leave → rejoin fixture (`[t0,t1)`, `[t2,NULL)`): a `t < t0` (pre-join), a `t1 ≤ t < t2` (gap), and a `t ≥ t2` (post-re-add) classify correctly; the boundary instants `t == t0`, `t == t1`, `t == t2` follow the half-open rule (`t0` in, `t1` out, `t2` in).
- `InScope` with an empty interval slice is `false` for any `t`.
- The interface is satisfied — `*sqliteStore` is handed back as `ChannelStore` by [`NewSQLiteStore`](../../internal/channels/sqlite.go#L95), so the package fails to compile if the new interface method is left unimplemented; confirm the build is green. (There is **no** standalone `var _ ChannelStore = (*sqliteStore)(nil)` assertion in the package — conformance rides the constructor's return type.)

#### PR checklist

- [ ] `go test ./internal/channels/ -run 'MembershipInterval|InScope' -count=1` passes.
- [ ] `make test` (Go lane) green; `go vet` clean.
- [ ] Type/helper/read-method live in the new files; the interface method lands in `store.go` (197 → 198 lines, far under cap). `channels.go` is untouched (stays 499).
- [ ] No write-path edits (`AddMember` / `RemoveMember` / `GetOrCreateDM` untouched — PR 3).
- [ ] Package builds (interface conformance is enforced by `NewSQLiteStore`'s `ChannelStore` return type — there is no separate assertion to update); any in-repo `ChannelStore` fakes/mocks gain the new method (grep for `ChannelStore` implementers).

---

### PR 3: `feature/v039-rfc0035-write-hooks` — Transactional Interval Open/Close (load-bearing)

**Depends on**: PR 2 merged.
**Purpose**: Wire the three `memberships`-mutating call sites to open/close intervals **in the same transaction**, so the ledger is live and exact. This is the hard dependency for [RFC 0036](0036-persona-message-recall.md) Phase 1 — it cannot begin until this merges.

#### Scope

| File | Change |
|------|--------|
| [`internal/channels/sqlite_query.go`](../../internal/channels/sqlite_query.go#L283) — `AddMember` | Currently a **single non-transactional** `INSERT … ON CONFLICT DO NOTHING` ([L301](../../internal/channels/sqlite_query.go#L301)). Wrap it in a `BeginTx`; check `RowsAffected()`. `== 1` (genuine new join or post-removal re-add) → INSERT an open interval `(channel_id, participant_id, joined_at = now, left_at = NULL)` in the same tx, where `now` is the **same** `time.Now().UTC()` written to `memberships.joined_at` so projection and ledger agree. `== 0` (already present) → open no interval (the existing stint's open interval is still correct; the `ux_…_open` index would reject a second anyway). Idempotency preserved on both tables. |
| `internal/channels/sqlite_membership_intervals.go` | The interval-write helpers (`openInterval(tx, …)`, `closeOpenInterval(tx, …) (int64, error)`) called by the three hooks, co-located with PR 2's read method. Keeps the write SQL in one ledger file rather than scattering it across `sqlite_query.go`. |
| [`internal/channels/sqlite_query.go`](../../internal/channels/sqlite_query.go#L130) — `RemoveMember` | Already transactional (act-first-then-disambiguate). On the `RowsAffected() == 1` success path, before `tx.Commit()`, run `UPDATE membership_intervals SET left_at = ? WHERE channel_id = ? AND participant_id = ? AND left_at IS NULL` with `now`. If it closes **zero** rows on this path, the open-interval invariant (Goal 6) is violated (a `memberships` row with no matching open interval) → **roll back and return a hard error**, never commit silently (a never-closing interval is a data-*exposure* bug for RFC 0036). The existing `n == 0` member-not-present branch closes nothing — that is the expected no-op, not the violation. |
| [`internal/channels/sqlite_query.go`](../../internal/channels/sqlite_query.go#L406) — `GetOrCreateDM` | Already transactional. After the two `memberships` inserts, open one interval per DM participant in the same tx with `joined_at = now` (the DM's creation time). DM membership is never removed in normal operation, so these stay open for the channel's life. |
| `internal/channels/sqlite_membership_intervals_test.go` (new) | Write-hook unit tests + lifecycle (see below). |
| `internal/channels/membership_intervals_integration_test.go` (new) | Integration test through the store API (add → remove → re-add). |

#### Key implementation details

- **`AddMember` becomes transactional — a behaviour-shape change worth review.** Today it is one `ExecContext`; PR 3 makes it `BeginTx` → INSERT-on-conflict → conditional interval INSERT → commit. The `RowsAffected`-gated interval open keeps the no-op-on-redundant-add semantics exact. The foreign-key-violation mapping to `ErrChannelNotFound` is preserved inside the tx.
- **`RemoveMember` zero-row close is loud by design.** The rollback-on-divergence posture mirrors `AddMember`'s reliance on `ux_…_open` to reject a spurious double-open ([RFC §C](0035-channel-membership-interval-ledger.md#c-write-path--opening-and-closing-intervals)). A distinct sentinel error (e.g. wrapping a package `errMembershipLedgerDivergence`) lets the test assert the rollback rather than a generic failure; it surfaces as a 500 to REST (an invariant breach, not a client error).
- **`now` consistency.** Each hook computes `time.Now().UTC()` once and uses the same value for both the `memberships` write and the interval write, so the half-open boundary math (§F) is exact across a leave-then-immediate-rejoin.
- **Transactional atomicity is the security property.** Each interval write rides its `memberships` mutation's transaction — a crash mid-op rolls back both; the ledger cannot diverge from the projection ([RFC §Security](0035-channel-membership-interval-ledger.md#security-considerations)). This is the correctness bar for an *authorization record*, because RFC 0036's `EXISTS` clause *is* the access-control decision.

#### Tests

Per [RFC §Test Strategy](0035-channel-membership-interval-ledger.md#test-strategy):

- **`AddMember` on a new participant** opens exactly one interval; its `joined_at` equals the `memberships` row's `joined_at` (same `now`).
- **Redundant `AddMember` on a present participant** opens **no** second interval (the `RowsAffected == 0` path) — assert still exactly one open interval.
- **`RemoveMember`** sets `left_at` on the open interval, leaves the rest of the row unchanged, and `GetMembershipIntervals` shows the interval closed.
- **Join → leave → rejoin** yields two non-overlapping intervals: one closed `[t0, t1)`, one open `[t2, NULL)`.
- **Atomicity** (`TestAddMember_OpensInterval_Atomically` / `TestRemoveMember_ClosesInterval_Atomically`): a forced failure after the `memberships` write rolls back the interval write too — neither table moves.
- **Invariant-violation rollback**: synthesize a `memberships` row with no open interval (direct SQL), then `RemoveMember` → assert the sentinel error and that the transaction rolled back (the `memberships` row is still present).
- **`GetOrCreateDM`** opens exactly one interval per DM participant, both open, `joined_at == DM creation time`.
- **Integration (store API)**: add, remove, re-add a participant; `GetMembershipIntervals` returns the two-stint history in order.

#### PR checklist

- [ ] `go test ./internal/channels/ -run 'MembershipInterval|AddMember|RemoveMember|GetOrCreateDM|Atomic' -count=1` passes.
- [ ] `make test` (Go lane) green; `go vet` clean; race detector clean on the channels package (`go test -race ./internal/channels/`).
- [ ] `sqlite_query.go` re-verified ≤ 500 lines (write SQL lives in `sqlite_membership_intervals.go`); if over, split further.
- [ ] No publish-path query change — `sqlite_messages.go` membership probe untouched ([RFC Goal 4](0035-channel-membership-interval-ledger.md#goals)); a publish regression test still green.
- [ ] **RFC 0036 Phase 1 is now unblocked** — note this in the PR description and in [v0.3.9-plan row 1a](../v0.3.9-plan.md#master-progress-overview).

---

### PR 4: `feature/v039-rfc0035-inspection-endpoint` — Phase 2 Operator Inspection Endpoint (optional, cut-tolerant)

**Depends on**: PR 3 merged. **Cut-tolerant** — not an RFC 0036 dependency; may be deferred or dropped without affecting the recall headline ([v0.3.9-plan §Candidate fold-ins](../v0.3.9-plan.md#candidate-fold-ins-maintainer-decision)).
**Purpose**: A read-only `GET` endpoint surfacing a participant's membership history for operator debugging / audit reconstruction, plus the `GetAccessibleChannels` convenience method.

#### Scope

| File | Change |
|------|--------|
| `internal/channels/sqlite_membership_intervals.go` | Add `GetAccessibleChannels(ctx, participantID) ([]string, error)` — distinct `channel_id`s the participant has ever held an interval in (`SELECT DISTINCT channel_id … ORDER BY channel_id`). Add to the `ChannelStore` interface in `store.go` (one line). |
| `internal/server/channel_membership_handlers.go` (new) | The read-only `GET` handler returning a participant's intervals as JSON. A **new file** rather than [`channel_handlers.go`](../../internal/server/channel_handlers.go) (494 lines — five under the cap). Proposed route: `GET /api/v1/channels/{channel_id}/members/{participant_id}/history` → `{ "intervals": [ { "joined_at", "left_at" }, … ] }` (`left_at` omitted/null while open). The exact path is a review decision; it must read naturally alongside the existing channel-member routes. |
| `internal/server/channel_types.go` | Response type for the interval-history payload. |
| `internal/server/channel_membership_handlers_test.go` (new) | Handler tests (see below). |

#### Key implementation details

- **Auth inherits the channel-surface trust level (OQ #2).** The endpoint adds no new auth model; it matches the existing unauthenticated channel REST surface at the current single-tenant trust level and picks up RFC 0009's model when that lands. The handler MUST NOT ship more permissively than its neighbours. Exposing who-was-where-when is no more sensitive than `GetMembers` already is.
- **Read-only.** `GET` only — no mutation surface on the ledger from REST. The append-only invariant stays owned by the three Go write hooks (PR 3).
- **`GetAccessibleChannels` lands here, not PR 2**, because this endpoint is its only Phase-2 consumer; RFC 0036 does not need it.

#### Tests

- Handler returns a participant's intervals for a channel they joined → left → rejoined: two entries, the first with `left_at`, the second open.
- A participant with no intervals in the channel → empty list, 200 (not 404) — consistent with the read-only "history" framing; a non-existent channel → 404, matching the existing channel-handler convention.
- `GetAccessibleChannels` returns the distinct set across closed and open intervals, deduped and ordered.

#### PR checklist

- [ ] `go test ./internal/channels/ ./internal/server/ -run 'AccessibleChannels|MembershipHistory|Inspection' -count=1` passes.
- [ ] `make test` green; `channel_handlers.go` untouched / still ≤ 500 lines.
- [ ] Endpoint registered in the channel-router wiring next to the existing member routes; route documented in the handler header comment.
- [ ] Auth posture documented inline as inheriting the channel-surface trust level (OQ #2); no bespoke auth added.
- [ ] If the cut tightens, this PR is the drop candidate — confirm with the maintainer before deferring.

---

### PR 5: `feature/v039-rfc0035-close` — Review Follow-Ups + Closeout

**Depends on**: PR 4 merged (or PR 3, if PR 4 is dropped).
**Purpose**: Fold in review findings from PRs 1–4 and mark RFC 0035 implemented. Follows the house "From PR N review" convention — each finding paraphrased inline, **never** linking a local review report ([.github/copilot-instructions.md](../../.github/copilot-instructions.md)).

#### Scope

| File | Change |
|------|--------|
| (various) | Review follow-ups surfaced in PRs 1–4, each paraphrased inline under a `From PR N review` subsection. Populated as PRs are reviewed. |
| [`docs/rfcs/0035-channel-membership-interval-ledger.md`](0035-channel-membership-interval-ledger.md) | Status → `✅ Implemented` (front-matter `status:` + the header badge); append an "Implemented in v0.3.9" note to Decision/Next Steps; `make rfcs` to regenerate [INDEX.md](INDEX.md). If PR 4 was dropped, status → `⚠️ Partially Implemented (Phase 1)` and the note records Phase 2 as deferred. |
| [`ROADMAP.md`](../../ROADMAP.md) | RFC 0035 Master-Index row → `✅ Implemented` (or `⚠️ Partially Implemented (Phase 1)`); `Last updated` refresh. |
| [`docs/rfcs/0035-pr-plan.md`](0035-pr-plan.md) | [Progress Overview](#progress-overview) rows filled with merged-PR numbers + dates. |

Doc-only unless a review finding requires a code change.

#### PR checklist

- [ ] All PR 1–4 review findings addressed inline or downgraded to tracked issues with rationale.
- [ ] `make test` + Go lint clean.
- [ ] RFC 0035 status flipped; `make rfcs` regenerated `INDEX.md` (front-matter is the source of truth — do not hand-edit the index).
- [ ] [ROADMAP RFC Master Index](../../ROADMAP.md#rfc-master-index) and [v0.3.9-plan row 1a](../v0.3.9-plan.md#master-progress-overview) reflect the final state.

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| The ledger is an **access-control record**; a missed close or spurious open becomes a data-exposure / data-suppression bug in RFC 0036 recall. | PR 3 makes every interval write transactional with its `memberships` mutation; the partial unique `ux_membership_intervals_open` (PR 1) makes a double-open a hard INSERT failure; `RemoveMember` rolls back rather than commit on a zero-row close. The ledger's transactional-consistency tests and recall's scope tests are reviewed as **one** correctness surface ([v0.3.9-plan §Why this plan exists #3](../v0.3.9-plan.md#why-this-plan-exists)). |
| `main` carries a **backfilled-but-not-maintained** ledger between PR 1 and PR 3. | Coherent additive state: the table reflects the current snapshot, has no consumer (RFC 0036 has not landed), regresses no existing behaviour, and the invariant index guards it from creation. PR 3 makes it track changes; RFC 0036 Phase 1 is gated on PR 3, not PR 1. |
| `AddMember` going transactional **regresses the add path** (error semantics, idempotency, FK-violation mapping). | PR 3 preserves the `ON CONFLICT DO NOTHING` + `RowsAffected`-gated open, so a redundant add still no-ops on both tables; the `ErrChannelNotFound` FK mapping moves inside the tx unchanged; existing `AddMember` tests run green, plus the new no-second-interval test. |
| **Pre-ship history is unrecoverable** — a persona removed before v9 has no backfillable stint. | Accepted and documented ([RFC §D](0035-channel-membership-interval-ledger.md#d-backfill), OQ #1): the backfill seeds one open interval per *currently present* member; the ledger is exact from v9 forward. Surfaced as a known recall limitation in RFC 0036's CHANGELOG Upgrade Notes, not a bug. |
| **File-size cap** (`channels.go` 499, `sqlite_query.go` 465, `channel_handlers.go` 494) is breached by the additions. | New code is routed into new files (`membership_intervals.go`, `sqlite_membership_intervals.go`, `channel_membership_handlers.go`); the one-line interface additions land in `store.go` (197 lines, far under the cap), not the near-cap files. Each PR checklist re-verifies the cap. |
| **Phase 2 (PR 4) reads as scope creep** in an infra-only RFC. | It is explicitly cut-tolerant and **not** a recall dependency — RFC 0036 joins the ledger server-side. If the v0.3.9 cut tightens, PR 4 is the first drop, leaving the recall headline intact ([v0.3.9-plan §Candidate fold-ins](../v0.3.9-plan.md#candidate-fold-ins-maintainer-decision)). |

---

## ROADMAP Hygiene

Per [.github/copilot-instructions.md §Status Hygiene](../../.github/copilot-instructions.md) and [v0.3.9-plan §ROADMAP hygiene](../v0.3.9-plan.md#roadmap-hygiene):

- **PR 1 opens (this PR plan goes in flight)** → RFC 0035 Master-Index status note `📋 Proposed → 🚧 Implementing`; advance the RFC front-matter `status: proposed → implementing` and run `make rfcs` so [INDEX.md](INDEX.md) regenerates in step; [v0.3.9-plan row 1a](../v0.3.9-plan.md#master-progress-overview) → 🔄 In progress.
- **Each PR merges** → fill the [Progress Overview](#progress-overview) row; `Last updated` refresh on each status flip.
- **PR 3 merges** → note RFC 0036 Phase 1 is unblocked (the substrate exists).
- **PR 5 merges** → RFC 0035 → `✅ Implemented` (or `⚠️ Partially Implemented (Phase 1)` if PR 4 dropped); row 1a → ✅; `Last updated` refresh.

---

## Progress Overview

| # | Title | Branch | Status | GitHub PR | Merged |
|---|-------|--------|--------|-----------|--------|
| 1 | Migration v9 — `membership_intervals` table + indexes + backfill | `feature/v039-rfc0035-migration` | ✅ Merged | [#671](https://github.com/mkhomutov/Persatrix/pull/671) | 2026-06-18 |
| 2 | Read surface — struct, `GetMembershipIntervals`, `InScope`, interface | `feature/v039-rfc0035-read-surface` | 🔀 PR open | [#672](https://github.com/mkhomutov/Persatrix/pull/672) | — |
| 3 | Write hooks — transactional interval open/close (load-bearing) | `feature/v039-rfc0035-write-hooks` | ⬜ Not started | — | — |
| 4 | Phase 2 — inspection endpoint + `GetAccessibleChannels` (cut-tolerant) | `feature/v039-rfc0035-inspection-endpoint` | ⬜ Not started | — | — |
| 5 | Review follow-ups + closeout | `feature/v039-rfc0035-close` | ⬜ Not started | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ⏭ Deferred

---

## Related Documentation

- [RFC 0035 — Channel Membership Interval Ledger](0035-channel-membership-interval-ledger.md) — canonical spec; §B schema, §C write path, §D backfill, §F in-scope predicate.
- [RFC 0036 — Persona Verbatim Message Recall](0036-persona-message-recall.md) / [RFC 0036 PR plan](0036-pr-plan.md) — the consumer; recall scoping joins this ledger. RFC 0036 Phase 1 is gated on this plan's PR 3.
- [v0.3.9-plan.md](../v0.3.9-plan.md) — master plan (row 1a is this workstream); locks the Phase-2-IN and §OQ-6 scope decisions.
- [RFC 0011 — Channels & Internal Agent Messaging](0011-channels-bridges.md) — the channel store and `memberships` table this RFC extends.
- [RFC 0009 — Agent Identity, Security & Sandboxing](0009-security-sandboxing.md) — the auth model PR 4's endpoint (OQ #2) defers to, and the independent audit subsystem this ledger complements.
- [RFC 0034 PR plan](0034-pr-plan.md) — structural template for this plan.
- [BRANCHING.md](../BRANCHING.md) — squash-merge + file-size-cap conventions.
