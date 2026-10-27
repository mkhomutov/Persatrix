# RFC 0029 — Personal/Society Storage Split — PR Implementation Plan (Phase 3, v0.4.0 form)

**RFC**: [0029-personal-society-storage-split.md](0029-personal-society-storage-split.md)
**Status**: 🔄 In progress
**Created**: 2026-10-21
**Branch prefix**: `feature/v040-rfc0029-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Spawned from**: row R2 of the [v0.4.0 plan](../v0.4.0-plan.md#progress); its row A1 is this plan's phase, in the form the plan's society-backend lock rules. Phase 1 is in the [v0.3.2 plan](0029-pr-plan.md).

> Sizes are calibrated (1.7× naive, the v0.1 factor). Every PR stays under 500
> changed lines and writes its failing test first.

---

## Overview

v0.4.0's organizations need state that belongs to the society rather than to
one persona: who manages whom, the actions waiting for a person's approval,
and the key that signs those approvals. RFC 0029's
[2026-10-20 amendment](0029-personal-society-storage-split.md#amendment-2026-10-20--v040s-society-backend)
puts that state in society tables in the orchestrator's existing SQLite file,
`data/channels.db`, behind a society-store interface, while SQLite holds.
This plan delivers that in five PRs:

- the channel-time index the channels store lost in v0.3.1, which the
  [storage benchmark](../storage-architecture-benchmark.md) found SQLite
  needs before it can hold a society (ISSUE-0188);
- the society tables, one migration in the channels store's chain;
- the society-store interface and its SQLite implementation on the channels
  store's own connection;
- the wiring, and the Known Gap issue the v0.4.0 plan assigns to A1.

The `asyncpg` pool, `shared_pools`, `pool_entries`, `bonds_inbound`, the bonds
projection and its outbox, the Postgres channels store, Phase 4's migration
tooling and Phase 5's decision records stay unslotted, as the amendment says.
So do the RFC's open questions 4 and 7, which only a Postgres form raises.

### The benchmark this plan rests on

Ruling (n) and the amendment let the society tables into SQLite only while a
benchmark shows SQLite holds, and the v0.4.0 plan orders that benchmark before
this plan. It ran on 2026-10-21 and is recorded in
[storage-architecture-benchmark.md](../storage-architecture-benchmark.md).
Its two findings are this plan's first two design points:

- **As shipped, the channels store does not hold ten agents at one message a
  second.** Since the RFC 0031 session migration (channels-store v3) replaced
  `idx_messages_channel_ts` with `(channel_id, session_id, timestamp)`, every
  newest-first read of a channel sorts the whole channel, the cap's prune
  sorts it to find the oldest row, and the scoped window read
  (`GetHistoryScoped`) scans the whole table through the epoch index. With
  the one index restored, the same load passes with room to spare. The fault
  is a missing index, not SQLite: a Postgres store with the same indexes
  would sort the same way.
- **The society tables must share the channels store's connection.** On a
  second connection to the same file, a society write that commits between a
  publish's membership read and its insert makes the publish fail at once
  with `SQLITE_BUSY`; `busy_timeout` does not wait out a read transaction's
  upgrade. On the shared connection every call queues and none fails.
  Immediate transactions on both connections were not measured (S-2).

### What is already in place (do not rebuild)

- `internal/channels/sqlite.go` — `NewSQLiteStore`: WAL, foreign keys,
  `busy_timeout(5000)`, and one open connection (`SetMaxOpenConns(1)`), so
  every call on the store is serialised in Go.
- `internal/channels/sqlite_schema.go` — `applySchema`, which walks
  `PRAGMA user_version` up to v13 through `applyMigration`'s `case` arms in
  `sqlite_migrations.go`, each version's body in its own file since v9, and
  refuses a store newer than the binary ("downgrade unsupported").
- `internal/accounts/sqlite.go` — the other orchestrator-owned store, a file
  of its own (`accounts.db`), the pattern for a store package with its own
  constructor and tests.
- `agents/memory/store.py` — RFC 0029 Phase 1's `MemoryStore`, whose
  society-tier calls raise `SocietyBackendUnavailable`. Nothing in this plan
  changes it: the society tables are read through the orchestrator's routes,
  never by an agent directly.
- `internal/channels/society_bench*_test.go` — the benchmark harness, with a
  stand-in for the society tables that PR 3 replaces with the real store.

### What this plan provides to the RFC 0012, RFC 0028 and RFC 0009 plans

| Requested by | Request | Provided | Revision |
|--------------|---------|----------|----------|
| [RFC 0012](0012-pr-plan.md#what-this-plan-needs-from-the-prerequisite-plans) C1 PR 3 | Tables for organizations, roles, members and `manages` edges with a `scope` label, behind the society-store interface | PR 2 (tables), PR 3 (calls) | — |
| RFC 0012 C1 PR 3, C3 | An organization-graph version counter | PR 2, PR 3 | Raised by the caller in the same transaction as the graph write; the store never raises it on its own |
| RFC 0012 C3 PR 11 | One transaction spanning a pending approval and the organization tables | PR 3: one transaction call over every society table | — |
| [RFC 0028](0028-pr-plan.md#what-this-plan-needs-from-the-prerequisite-plans) PR 12 | The approval-signing key | PR 2 (table), PR 3 (create once, read) | — |
| RFC 0028 PRs 12–14a | Approval rows: action hash, `actor_id`, nonce, expiry, protection level, cascade depth, origin, a six-value state that is the used-nonce record, at most one open row per `(actor_id, action_hash)` | PR 2, PR 3 | Rows also carry a kind, a tie group, the held action's payload, who decided and when, and whether the agent has been told (S-3) |
| [RFC 0009](0009-phases-3-4-pr-plan.md) PRs 12–14 | The queue's rows and their transactions | PR 2, PR 3 | — |

---

## Phase 0 — open questions and design points

The amendment settled the backend. RFC 0029's open questions 4 and 7 do not
arise in this form, and question 2 is locked (the third form). What remains
is how the tables are laid out, settled by a new default in the PR named and
confirmed or overturned in its review:

- **S-1 The index comes back first, as part of A1's store change.** PR 1
  restores messages by channel in time order as its own migration,
  v13 → v14, before any society table lands, so the society tables are added
  to a store the benchmark passed. The v0.4.0 plan's store lock counts three
  store changes and lets the society tables into SQLite only while SQLite
  holds; SQLite holds only with this index, so this plan counts it within
  A1's change to the channels store, not as a fourth, and adds it to Upgrade
  Note 1. If review reads it as a fourth, an amendment rules on it before
  PR 1 opens. It also fixes every v0.3.x deployment, as
  [ISSUE-0188](../issues/ISSUE-0188-channels-store-lost-its-channel-time-index.md),
  which boards as a dependency this plan states (ruling q). The index takes
  a new name, `idx_messages_channel_time`, so the session migration's
  tests, which pin that `idx_messages_channel_ts` stays dropped and that the
  v1 schema never recreates it, keep their meaning. Building it reads every
  message once at the first start, seconds for a store at its caps. PR 1.
- **S-2 One connection, one migration chain.** SQLite keeps one
  `user_version` per file, so the society tables join the channels store's
  chain (v14 → v15) rather than starting a second. The society store runs on
  the channels store's single connection, handed over once at startup; it
  never opens its own (the benchmark's second finding). A society call
  therefore queues behind channel calls as they queue behind each other.
  The benchmark did not measure a second connection with both connections'
  write transactions begun as `IMMEDIATE` (`_txlock=immediate`), under
  which `busy_timeout` applies. PR 3 runs the benchmark's separate form that
  way first, and if it passes, its review may give the society store a
  connection of its own instead, which drops S-6's no-other-call rule.
  PRs 2–3.
- **S-3 What an approval row holds.** RFC 0028's columns, plus what the
  RFC 0009 and RFC 0012 plans need to run the queue: a `kind` — `action`,
  `graph_change` or `tie`, with what `actor_id` and `action_hash` mean for
  each as [RFC 0009 plan G-9](0009-phases-3-4-pr-plan.md#phase-0--open-questions-and-design-points)
  sets out; a nullable `tie_group`; the held action's payload as JSON,
  cleared when the row settles (G-11); `decided_by` and `decided_at`;
  `delivered_at` for the decision event (G-10); and `created_at`. A partial
  unique index keeps one open row per `(actor_id, action_hash)`; the nonce is
  unique; a check constraint keeps `state` to its six values and `kind` to
  its three. PR 2.
- **S-4 The organization tables follow RFC 0012 §B.** Organizations (id,
  name, topology), roles (organization, id, authority), members
  (organization, agent id, role, keyed by organization and agent) and
  `manages` edges (organization, manager, report, scope label, empty when
  the topology has none), and the graph version in a one-row meta table.
  The one-organization rule (RFC 0012 OQ 4) stays with validation, not a
  key, so Phase 5 lifts it without a migration. Clearance has no column: RFC
  0012's schema refuses the key (its D-3). PR 2.
- **S-5 The key table holds one row per purpose.** The approval-signing key is
  made by the store's first call for it, inside a transaction, so two
  starts cannot make two keys. It appears in no log and on no route. PR 3.
- **S-6 The interface is narrow and transactional.** `internal/society`
  defines the store's interface and its SQLite implementation: replace and
  read the organization graph with its version; insert, read, list and move
  an approval row only from a stated state, so a lost race is reported, not
  overwritten; read or create a key; and one call that runs a function inside
  a single transaction over all of them, which RFC 0009's approve hook and
  RFC 0012's graph change use. The function gets a handle that reads and
  writes inside that transaction, and every society read it needs, the key
  and the graph included, goes through it. With one connection, a call made
  any other way inside the function would wait for itself forever, so it
  makes no channels-store call and no network call, and events and audit
  lines go out after the commit. Callers never see `*sql.DB`. PR 3.
- **S-7 The benchmark stays the check.** It runs in no CI job (a gate run is
  45 minutes). PR 1 re-runs it on the real migration and PR 3 on the real
  society store, and each PR body records the result; a change that later
  adds a society table re-runs it.

---

## Progress Overview

| PR | Row | Title | Branch | Status | GitHub PR | Merged |
|----|-----|-------|--------|--------|-----------|--------|
| 1 | A1 | Restore the channel-time index (channels-store v14; ISSUE-0188) | `feature/v040-rfc0029-channel-time-index` | ✅ | [#1057](https://github.com/mkhomutov/Persatrix/pull/1057) | 2026-10-24 |
| 2 | A1 | The society tables (channels-store v15) | `feature/v040-rfc0029-society-tables` | ✅ | [#1060](https://github.com/mkhomutov/Persatrix/pull/1060) | 2026-10-25 |
| 3 | A1 | `internal/society`: the store on the channels connection | `feature/v040-rfc0029-society-store` (head branch `ccr-b7e6b467-72zd06`, assigned by the session that opened it) | ✅ | [#1063](https://github.com/mkhomutov/Persatrix/pull/1063) | 2026-10-27 |
| 4 | A1 | Startup wiring; the single-node Known Gap issue | `feature/v040-rfc0029-society-wiring` | ⬜ | — | — |
| 5 | — | Review follow-ups and closeout | `feature/v040-rfc0029-closeout` | ⬜ | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ✂️ Cut

> A PR's own row still says 🔀 when it merges, so `main`'s plan-status
> check fails until a later PR flips it. PRs 1 to 3 each needed that
> follow-up: see [ISSUE-0190](../issues/ISSUE-0190-merged-pr-plan-row-reddens-main.md).

## Dependency graph

```
R2 benchmark (done) ──► this plan
PR 1 (v14) ──► PR 2 (v15) ──► PR 3 ──► PR 4 ──► PR 5
A1 done ──► RFC 0009 A4 PR 12 · RFC 0028 B3 · RFC 0012 C1
```

The hard edges:

- **The benchmark before this plan**, as the v0.4.0 plan orders, and PR 1
  before PR 2, so no society table lands on a store that failed it.
- **Each migration before its reader** — PR 2's tables before PR 3's calls;
  A1 complete, PR 4's wiring included, before RFC 0009 A4 PR 12, RFC 0028
  PR 12 and RFC 0012 C1 PR 3, their first writers.
- **This plan's v15 before RFC 0012's `channels.authority`** — RFC 0012
  PR 5 takes the next version after A1's.

---

## PR Sequence

Every PR: the failing test first and confirmed red; `make test-go` and
`gofmt -l` clean; docs and status moved in the same PR; review findings
recorded under the PR, numbered `F-n`.

### PR 1 — `feature/v040-rfc0029-channel-time-index`

**Scope**: `internal/channels/sqlite_channel_time_index_migration.go` (new),
its `case` arm in `applyMigration` and `channelStoreSchemaVersion`, and its test
file; the benchmark's
"indexed" variant retired, since the shipped schema now has the index.
RFC 0029 moves to 🚧 Implementing in this PR if it has not already.
**Implementation**: S-1. `CREATE INDEX IF NOT EXISTS idx_messages_channel_time
ON messages(channel_id, timestamp DESC)` in one transaction that stamps the
version. No query changes: the planner picks the index for the newest-first
read, the prune's oldest-first read and the scoped window read. The store
runs `PRAGMA optimize` when it opens, so that choice rests on statistics
rather than the no-statistics guess that now takes `idx_messages_epoch`.
**Tests**: the migration on a populated v13 store; a query-plan test for each
of those three reads that fails if the plan sorts the channel or scans by
epoch (`USE TEMP B-TREE` or `idx_messages_epoch` in `EXPLAIN QUERY PLAN`), so
a later migration cannot silently drop the index again, each run on a store
with and without statistics; the session migration's tests still pass
unchanged.
**Migration**: **channels store**, v13 → v14.
**Acceptance**: the benchmark's gate passes on the shipped schema, recorded
in the PR body; ISSUE-0188 closed.
**Estimate**: ~250 lines.

**Review findings** (PR 1):

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| P-1 | Low | This section said the session migration's tests pass unchanged; one pinned v3's covering index as the index `GetHistory` walks, and the planner now takes the new one | Fixed in-PR: that assertion moves to the new index |
| F-1 | Low | The comment on the open-time `PRAGMA optimize` said its 0x10000 bit is what makes an open analyze, and that it is otherwise a no-op. 0x02 alone analyzes a table with an unanalyzed index; 0x10000 adds the tenfold-growth check; every form takes the write lock, so an open waits out `busy_timeout` behind another writer | Fixed in-PR (comment) |
| F-2 | Low | No test pinned the mask: the statistics test passed with plain `PRAGMA optimize` | Fixed in-PR: a store grown twentyfold is re-analyzed at open, a test that fails with 0x02 |
| F-3 | Low | Statistics are gathered only at open. Taken while `membership_intervals` holds one row, they turn the scoped read's membership check into a scan of that table, until the next open after it grows tenfold | → deferred to PR 5 |
| F-4 | Low | The edited v3 test's comment did not say what its body checks, and it still explained a hand-copied query | Fixed in-PR: it explains `historyQuery` |
| F-5 | Low | The plan's dated note cited P-1 with no finding behind it, and left out the mask | Fixed in-PR |
| F-6 | Low | Two comments still said v3's index kept the history scan cheap, and one dated the scoped read's epoch scan to v6, before the read existed | Fixed in-PR |
| F-7 | Low | This PR's flip of the RFC 0028 plan's PR 1 row dated #1054 in UTC; `merged-prs.md` uses the author date, 2026-10-24 | Moved out: #1058 flipped that row on main, dated 2026-10-24, so this PR leaves it alone |
| F-8 | Info | The shipped schema runs with planner statistics; the benchmark's 2026-10-21 indexed variant had the index without them | Noted in the benchmark's re-run section |
| F-9 | Info | The gate re-run shared the machine with unrelated load (load average ~8.6 on 8 cores), and its room step overlapped a disk-wide scan for two minutes | Noted: both shared steps passed; the record says so |
| F-10 | Info | ~880 changed lines against the ~250 estimate and the 500-line target, ~570 of them in test files | Noted: the migration stays with its tests |
| F-11 | Medium | Statistics an open took from a store of 70 messages or fewer turn the cap prune's DELETE into a scan of the whole table, kept until an open finds it grown tenfold: a store that restarted early and then ran to the cap paid ~11 ms a publish at 50 000 messages, against ~1 ms | Fixed in-PR: the open gathers statistics only once the store holds 1 000 messages |
| F-12 | Medium | The plan tests passed that scan: their check rejected only a sort and the epoch index, and their statistics came from 300 messages a channel | Fixed in-PR: the check rejects any step that reads every message, on stores reopened young and at the floor |

### PR 2 — `feature/v040-rfc0029-society-tables`

**Scope**: `internal/channels/sqlite_society_migration.go` (new), its
`case` arm and `channelStoreSchemaVersion`; its test file.
**Implementation**: S-2, S-3, S-4 and S-5's table. Every society table in one migration,
named `society_*`, with their constraints and indexes. No store reads them
yet.
**Tests**: the migration on a populated v14 store; each constraint refuses
its bad row — a second open approval for one actor and hash, a reused nonce,
an unknown state or kind, an agent twice in one organization; a member in
two organizations is admitted, since that rule stays in validation (S-4); a
v15 store opened by a v14 binary is refused.
**Migration**: **channels store**, v14 → v15; read by PR 3.
**Acceptance**: the tables exist and nothing reads them.
**Estimate**: ~300 lines.

**Review findings** (PR 2):

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| F-1 | Medium | The held-turn check made every `action` row carry a protection level, cascade depth and origin, but a task agent's hold (RFC 0028 PR 14a) waits inside its step and has no held turn, so it would have had to write made-up values | Fixed in-PR: all three or none; RFC 0028 PR 14's release refuses a persona hold without them |
| F-2 | Medium | No plan says how an approved `tie` or `graph_change` row ends: no token spends it, and RFC 0009 PR 13's sweep expires every unspent approved row, so an accepted tie would later read as expired and its directive be refused | → deferred to RFC 0009 PR 13, which picks how such a row ends; the six states hold either way, so no migration follows |
| F-3 | Low | Deleting a role silently deleted its members and their edges | Fixed in-PR: a role a member holds cannot be deleted |
| F-4 | Low | A one-column TEXT primary key admitted NULL, and NULLs never collide, so two starts could each store a key with no purpose | Fixed in-PR: the three are NOT NULL |
| F-5 | Low | Times were DATETIME, which the driver writes as text in the writer's own time zone, so an expiry compared as text could be hours off | Fixed in-PR: unix seconds, as the accounts store keeps its expiry, and a check refuses anything else |
| F-6 | Low | `origin` is one text column, but E-13's release also needs the held turn's wallet cause, and RFC 0037's tripwire more of the dispatch origin than a channel id | → deferred to RFC 0028 PR 14, which defines what `origin` holds, the cause included |
| F-7 | Low | A `tie` row outside any tie group was admitted | Fixed in-PR: a check |
| F-8 | Low | The held-turn test emptied all three columns at once, so a check on one of them passed | Fixed in-PR: one case per column |
| F-9 | Low | No test pinned the five non-unique indexes | Fixed in-PR: the upgrade test pins each table's indexes |
| F-10 | Low | No test pinned that a step that cannot finish leaves the store at v14 | Fixed in-PR |
| F-11 | Low | A comment said the store keeps no decided action's text; an approved row keeps it until it is spent or expires | Fixed in-PR: no settled row's text |
| F-12 | Low | The dated note listed only some of the indexes beyond S-3, and not the role rule | Fixed in-PR |
| F-13 | Low | This section's Implementation line left out S-5, whose table it creates | Fixed in-PR |
| F-14 | Low | The schema history's v15 line read as though the amendment named that file | Fixed in-PR |
| F-15 | Info | Rows still admitted: a member managing themself, a payload that is not JSON, a deleted graph-version row, a tie and an action sharing a hash, and the 32-byte floor applies to every key purpose | Noted: validation refuses the first as a cycle; only PR 3's store calls write the rest |
| F-16 | Info | The open-row index serves a query only when it repeats `state IN ('pending', 'approved')`, and replacing a member with `INSERT OR REPLACE` drops their edges | → deferred to PR 3 |
| F-17 | Info | One `delivered_at` serves G-10's decision event only if each transition clears it and an acknowledgement names the state it was sent for | → deferred to RFC 0009 PR 14 |
| F-18 | Info | The file-size table gave `sqlite_migrations.go` as 476 lines | Fixed in-PR |
| F-19 | Info | The schema is a package-level slice, where v9 builds its statements inside the function | Noted: kept, as v14 keeps its index statement at package level |
| F-20 | Info | ~1 020 changed lines against the ~300 estimate and the 500-line target, ~710 of them in test files | Noted: the tables stay with their tests, split into a migration file and a constraints file |
| F-21 | Low | Only a `tie` row with no group was refused, so an `action` or `graph_change` row could carry a tie group and be withdrawn with that group's siblings | Fixed in-PR: a `tie` row, and no other, has a tie group |
| F-22 | Low | A `graph_change` or `tie` row could keep a held turn, which a release would read as a persona hold to resume | Fixed in-PR: only an `action` row keeps one |
| F-23 | Low | The cascade depth and the graph version took text, which passes `>= 0`, and the depth could be negative | Fixed in-PR: both are whole numbers, never below zero |
| F-24 | Low | The key's `created_at` took a `time.Time`, stored as text, F-5's hazard | Fixed in-PR: unix seconds, checked |
| F-25 | Low | A row could record who decided with no time, or a time with no decider | Fixed in-PR: both or neither |
| F-26 | Low | The upgrade test pinned the literal 15 after a full open, so v16 would break it as v15 broke v14's | Fixed in-PR: it expects `channelStoreSchemaVersion`; the idempotence test keeps the literal |
| F-27 | Low | The index pin checked names only, so an index rebuilt on other columns passed | Fixed in-PR: each index's columns and partial condition are pinned |
| F-28 | Low | The statistics floor counts messages only, so the society tables are analyzed while they hold a few rows, and the expiry sweep plans as a scan until they grow tenfold | → deferred to PR 5, with PR 1's F-3 |
| F-29 | Info | No index served the member's role foreign key, so each role delete scanned the organization's members | Fixed in-PR: members are indexed by role |
| F-30 | Info | The schema history's v15 line broke mid-sentence | Fixed in-PR |

### PR 3 — `feature/v040-rfc0029-society-store`

**Scope**: a new `internal/society/` — `store.go` (the interface and types),
`sqlite.go` (the implementation), and their tests; one exported call in
`internal/channels` that hands the store's connection to it; the benchmark's
stand-in society calls replaced by this store's; the glossary's *society
store*, beside its *Society State* entry.
**Implementation**: S-5 and S-6. Each move of an approval row names the
state it expects and fails with a typed error when the row is elsewhere. The
transaction call rolls back everything on any error from the function it
runs.
**Tests**: replace and read a graph, and its version; a replacement that fails
half-way leaves the old graph; an approval row moved from the wrong state
reports the race; the key is made once across two concurrent first calls and
survives a reopen; a transaction that writes a graph edge and approves a row
commits both or neither; publishes and society writes run together with no
error (the benchmark's second finding as a unit test).
**Migration**: none.
**Acceptance**: the benchmark's gate passes with the real society store,
recorded in the PR body.
**Estimate**: ~450 lines.
**From PR 2 review**: F-16 — a query that should use the open-row index
repeats its condition, `state IN ('pending', 'approved')`, as written; a
member's role changes by update, never `INSERT OR REPLACE`, which deletes
the row and with it their edges; a graph-version update checks that it
changed one row. The comments that say nothing reads the society tables
go with this PR.

**Review findings** (PR 3):

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| F-1 | Medium | A move never checked the expiry, so a row past it but not yet swept could be approved or spent; only the benchmark's own read guarded it | Fixed in-PR: a decision or a spend on a row at or past its expiry fails with `ErrExpired`, in the move's own statement |
| F-2 | Medium | The constructor took a connection without foreign keys, where replacing the graph leaves the old roles, members and edges behind, and the graph read then panicked or filed them under another organization | Fixed in-PR: such a connection is refused, and a row of an unknown organization fails the read |
| F-3 | Low | Every read began as a write transaction, so under `_txlock=immediate` each authority check and queue read waited for the write lock; the benchmark's immediate form measured that, not the form its doc described | Fixed in-PR: reads begin read-only; the gate re-ran on the fix |
| F-4 | Low | The key's comment promised one key across first calls on two connections, but a second call that read first failed its insert with `SQLITE_BUSY_SNAPSHOT` | Fixed in-PR: the insert comes first, so a call of its own takes the write lock before it reads; a two-connection test pins it |
| F-5 | Medium | A key in another value's unexported field printed its bytes, since fmt cannot call its methods there, and as a `[]byte` it passed to `zap.Binary` | Fixed in-PR: its bytes sit behind a pointer, which prints as an address, and only `Bytes()` hands them over |
| F-6 | Low | A move to approved or denied could record no one deciding | Fixed in-PR: a decision names who and when |
| F-7 | Low | A Store call inside the transaction call waited forever for the connection its own transaction held | Fixed in-PR: the function gets a marked context, and a Store call made with it fails at once with `ErrInvalid`; the channels store cannot see the mark, so its rule stays a rule |
| F-8 | Low | The expiry sweep and a tie group's withdrawal ran a list and then two statements a row, holding the shared connection | Fixed in-PR: `MoveApprovals` moves them in one statement |
| F-9 | Low | Shapes only the table's checks refused (a held turn off an action row, a tie group on the wrong kind, a negative depth) came back as driver errors, not `ErrInvalid` | Fixed in-PR: checked before the insert |
| F-10 | Low | The list ordered rows opened in one second by their ids as text, so `hold-10` came before `hold-9` | Fixed in-PR: by creation time, then rowid |
| F-11 | Low | The open-row conflict was told by the driver's message text | Fixed in-PR: the typed code, confirmed by a lookup of the open row; `internal/channels`' helper is not shared, since importing it here would make a cycle with its benchmark |
| F-12 | Low | Opening and moving a row each read it back with a second statement | Fixed in-PR: `RETURNING` |
| F-13 | Low | The benchmark's seed recorded each settled row's decision a day before the row was opened | Fixed in-PR: each row settles after it opens, a decision or a spend before its expiry |
| F-14 | Info | The ROADMAP's latest-changes note grew to a paragraph | Fixed in-PR: shortened |

### PR 4 — `feature/v040-rfc0029-society-wiring`

**Scope**: `cmd/orchestrator/channels.go` (491) builds the society store from
the channels store and hands it to the server and the components that need
it, through one new small file rather than `main.go` (499); the issue the
v0.4.0 plan's Known Gaps assign to A1 — the society backend is single-node
SQLite — filed and linked from that row; RFC 0029's Files Touched and Test
Strategy rows for Phase 3 noted as replaced by this form.
**Implementation**: with channels disabled there is no channels store and so
no society store; a component that needs one says so at startup, as the
channels routes do.
**Tests**: the orchestrator starts with a society store on the channels
connection; with channels disabled, startup logs that society features are
off.
**Estimate**: ~250 lines.

### PR 5 — `feature/v040-rfc0029-closeout`

**Purpose**: the review findings deferred from PRs 1–4, paraphrased by
component; RFC and ROADMAP status to ⚠️ Partially Implemented (Phases 1 and 3
shipped, Phase 3 in the SQLite form; Phases 2 and 4–6 unslotted); the storage
roadmap's SA-1 row updated; this plan's statuses closed. RFC 0029, near its
8 000-word cap, splits at a real seam first if the closeout would cross it.
**From PR 1 review**: F-3 — refresh the planner statistics while the store
stays open, with a periodic `PRAGMA optimize` as SQLite advises for
long-lived connections, so statistics taken on a young store do not keep a
plan the store has outgrown.
**From PR 2 review**: F-28 — the open gathers statistics once the store holds
1 000 messages, however few rows the society tables hold, so the refresh
covers them too.

---

## File-size constraints

| File | Lines now | Plan |
|------|-----------|------|
| `cmd/orchestrator/main.go` | 499 | PR 4 wires through its own file |
| `cmd/orchestrator/channels.go` | 491 | PR 4 adds one call; the logic lives in the new file |
| `internal/channels/sqlite_migrations.go` | 484 | PRs 1 and 2 add one `case` arm each; the bodies live in their own files, as since v9 |

## Risks

| Risk | Mitigation |
|------|------------|
| The society grows past what one SQLite file serves. | The benchmark's ramp gives the headroom; a deployment that needs more, or a second node, takes a Postgres form by amendment, as the lock says. |
| A later migration drops the channel-time index again. | PR 1's query-plan tests. |
| A component opens its own connection to `data/channels.db`. | S-2; PR 3's concurrent test; the store's constructor takes the channels store, not a path. |
| A society write blocks channel traffic. | Every society call is a few indexed rows, sub-millisecond in the benchmark; nothing holds the connection across a network call. |

## Notes

> 2026-10-21 — Plan written for row R2 after the benchmark. RFC 0029 moves to
> 🚧 Implementing with it, and its amendment gains a dated note with the
> benchmark's result and its two conditions.

> 2026-10-24 — PR 1 departs from its section three times. One
> session-migration test could not pass unchanged: it pinned v3's covering
> index as the one `GetHistory` walks, and the planner now takes the new
> index, so that assertion moves to it (P-1). The three reads' SQL moved,
> unchanged, into `historyQuery`, `pruneOldestSQL` and `scopedHistoryQuery`,
> so the plan tests explain the statements the store runs; the v3 test's copy
> of the history query had already drifted from it. And the open-time pragma
> is `PRAGMA optimize=0x10002`, the form SQLite advises when a long-lived
> connection opens, with no analysis limit, so the statistics are exact. It
> runs only once the store holds 1 000 messages, since statistics from a
> smaller store mislead the cap prune (F-11).

> 2026-10-24 — PR 2 settles what S-3 to S-5 leave open, each a new default
> for its review to confirm or overturn; the review's findings shaped six:
> - A persona's hold keeps the turn its release resumes (RFC 0028 E-13): its
>   protection level, cascade depth and origin. A task agent's hold waits
>   inside its step, a `graph_change` row's actor is an operator, and a
>   `tie` row releases nothing, so the three columns are nullable, all three
>   or none (F-1), and only an `action` row keeps them (F-22). A `tie` row,
>   and no other, belongs to a tie group (F-7, F-21).
> - The table itself keeps G-11: a row leaves `pending` and `approved` only
>   when the same write clears its payload. A decided row records who
>   decided and when, both or neither (F-25).
> - The nonce is made when a row opens, so every row has one, and the token
>   an approval mints carries it.
> - Times are unix seconds, as the accounts store keeps its expiry, so an
>   expiry is compared as a number in Go, never as text (F-5). The cascade
>   depth and the graph version are whole numbers too (F-23).
> - The key table refuses a key shorter than 32 bytes, or one stored as
>   text, and a time that is not unix seconds (F-24); no one-column key may
>   be NULL (F-4).
> - Topology and role authority have no check: the graph is written only
>   after the loader validates it, and the loader owns both vocabularies, as
>   `classification.go` owns v11's.
> - Beyond S-3's unique index, approvals are indexed by state and expiry for
>   the queue and the sweep, by actor for an agent's holds route, and by tie
>   group; members by agent, and by role for its foreign key (F-29); and
>   edges by report for the walk up a chain (F-12).
> - Deleting an organization deletes its graph, and removing a member
>   removes the edges to and from them; a role a member holds cannot be
>   deleted (F-3). The migration seeds the graph-version row at 0, meaning
>   no graph is loaded.
>
> One test reads its section differently: no test runs a v14 binary, so
> "a v15 store opened by a v14 binary is refused" is pinned as this binary
> refusing a store one version newer than it knows, the same rule.

> 2026-10-26 — PR 3 settles what S-5 and S-6 leave open, each a new default
> for its review to confirm or overturn:
> - The constructor takes a `ConnSource`, which the SQLite channel store
>   satisfies with `SocietyConn()`, the one exported call in
>   `internal/channels`. The interface lives in `internal/society`, so the
>   benchmark, a test in `internal/channels`, runs the real store with no
>   import cycle. The constructor fails on a connection without the society
>   tables.
> - The graph is replaced whole: deleting the organizations takes their
>   roles, members and edges (v15's cascade), and the new graph is inserted,
>   under a savepoint, so a graph the tables refuse leaves the old one even
>   inside a caller's transaction that goes on to commit. No row is replaced
>   in place (F-16). The version write must change the one meta row and
>   raise the version, so of two writers that read one version only one
>   wins (`ErrGraphVersion`). The graph reads back sorted by its keys, the
>   canonical form RFC 0012 PR 3 compares.
> - The store makes each row's nonce (16 random bytes) and creation time,
>   and refuses a payload that is not JSON (PR 2 F-15).
> - A move names the state it expects and follows the queue's lifecycle:
>   pending to approved, denied, expired or withdrawn; approved to spent,
>   expired or withdrawn. The four settled states are final. A move to one
>   clears the payload, and every move clears `delivered_at`, so each new
>   state is owed its own decision event: half of PR 2's F-17, whose
>   acknowledgement stays RFC 0009 PR 14's.
> - The open-row lookup is a list query that selects open rows, stated as
>   the index's own condition; a query-plan test pins that it takes the
>   open-row index (F-16).
> - A key formats, logs and marshals as `[redacted]`, so it reaches no log
>   or route by accident (S-5).
> - The benchmark's gate passes with the real society store
>   ([re-run](../storage-architecture-benchmark-reruns.md#the-gate-on-the-society-store)),
>   and so does S-2's second connection with both connections' write
>   transactions begun as `IMMEDIATE`; on a second connection as
>   transactions begin today, society writes fail as well as publishes. The
>   store keeps the shared connection: the immediate form passed one run on
>   one platform, and moving to it changes how every channel-store
>   transaction begins. Its review may give the store its own connection,
>   which would drop S-6's no-other-call rule.

> 2026-10-27 — PR 3's review kept those defaults and the shared connection,
> and added to them ([its findings table](#pr-3--featurev040-rfc0029-society-store)):
> a decision or a spend on a row at or past its expiry fails, even before the
> sweep; a decision names who made it; the sweep and a tie group's withdrawal
> move their rows in one statement; the store's own reads begin read-only,
> though under `_txlock=immediate` a transaction call that only reads still
> takes the write lock; the constructor also refuses a connection without
> foreign keys; a key is written before it is read, and its bytes sit behind
> a pointer; and a Store call made with the context the transaction call
> hands its function fails at once, though one made with any other context
> still waits forever.
> The gate [re-ran on the fixed store](../storage-architecture-benchmark-reruns.md#the-gate-after-the-society-stores-review)
> and passed on the shared and immediate forms. On the separate one, society
> writes, which now begin with their write, no longer fail; publishes still
> do, and more of them than before (7 and 8, against 2).

## Related documentation

- [Storage architecture benchmark](../storage-architecture-benchmark.md) — the result this plan rests on
- [RFC 0009 Phases 3–4 plan](0009-phases-3-4-pr-plan.md) — the approval queue on these tables
- [RFC 0028 PR plan](0028-pr-plan.md) and [RFC 0012 PR plan](0012-pr-plan.md) — the key, the approvals and the organization graph
- [v0.4.0 plan](../v0.4.0-plan.md) — row A1 and the society-backend lock
- [Release cycle §Phase 1](../methodology/release-cycle.md#phase-1--implementation-prs) · [review process](../methodology/review-process.md)
