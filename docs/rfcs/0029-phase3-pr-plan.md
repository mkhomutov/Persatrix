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
| 1 | A1 | Restore the channel-time index (channels-store v14; ISSUE-0188) | `feature/v040-rfc0029-channel-time-index` | 🔀 | — | — |
| 2 | A1 | The society tables (channels-store v15) | `feature/v040-rfc0029-society-tables` | ⬜ | — | — |
| 3 | A1 | `internal/society`: the store on the channels connection | `feature/v040-rfc0029-society-store` | ⬜ | — | — |
| 4 | A1 | Startup wiring; the single-node Known Gap issue | `feature/v040-rfc0029-society-wiring` | ⬜ | — | — |
| 5 | — | Review follow-ups and closeout | `feature/v040-rfc0029-closeout` | ⬜ | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ✂️ Cut

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
| F-7 | Low | The RFC 0028 plan's PR 1 row dated #1054 in UTC; `merged-prs.md` uses the author date, 2026-10-24 | Fixed in-PR |
| F-8 | Info | The shipped schema runs with planner statistics; the benchmark's 2026-10-21 indexed variant had the index without them | Noted in the benchmark's re-run section |
| F-9 | Info | The gate re-run shared the machine with unrelated load (load average ~8.6 on 8 cores), and its room step overlapped a disk-wide scan for two minutes | Noted: both shared steps passed; the record says so |
| F-10 | Info | ~880 changed lines against the ~250 estimate and the 500-line target, ~570 of them in test files | Noted: the migration stays with its tests |

### PR 2 — `feature/v040-rfc0029-society-tables`

**Scope**: `internal/channels/sqlite_society_migration.go` (new), its
`case` arm and `channelStoreSchemaVersion`; its test file.
**Implementation**: S-2, S-3 and S-4. Every society table in one migration,
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

---

## File-size constraints

| File | Lines now | Plan |
|------|-----------|------|
| `cmd/orchestrator/main.go` | 499 | PR 4 wires through its own file |
| `cmd/orchestrator/channels.go` | 491 | PR 4 adds one call; the logic lives in the new file |
| `internal/channels/sqlite_migrations.go` | 476 | PRs 1 and 2 add one `case` arm each; the bodies live in their own files, as since v9 |

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
> connection opens, with no analysis limit, so the statistics are exact.

## Related documentation

- [Storage architecture benchmark](../storage-architecture-benchmark.md) — the result this plan rests on
- [RFC 0009 Phases 3–4 plan](0009-phases-3-4-pr-plan.md) — the approval queue on these tables
- [RFC 0028 PR plan](0028-pr-plan.md) and [RFC 0012 PR plan](0012-pr-plan.md) — the key, the approvals and the organization graph
- [v0.4.0 plan](../v0.4.0-plan.md) — row A1 and the society-backend lock
- [Release cycle §Phase 1](../methodology/release-cycle.md#phase-1--implementation-prs) · [review process](../methodology/review-process.md)
