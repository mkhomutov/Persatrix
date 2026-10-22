# Storage Architecture Benchmark — does SQLite hold a society?

**Status**: ✅ Run 2026-10-21
**Question**: [storage roadmap open question 1](storage-architecture-roadmap.md#open-questions)
— at what scale does SQLite break for channels, ten agents writing to one
channel at one message a second each?
**Decides**: whether the [v0.4.0 plan](v0.4.0-plan.md#scope-locks)'s
society-backend lock stands. RFC 0029's
[2026-10-20 amendment](rfcs/0029-personal-society-storage-split.md#amendment-2026-10-20--v040s-society-backend)
puts the society tables in `data/channels.db` only while SQLite holds.
**Harness**: `internal/channels/society_bench*_test.go`, skipped unless asked
for ([Reproduce](#reproduce)).

---

## Answer

**SQLite holds a society on two conditions. As shipped, the channels store
does not.**

- **As shipped, it fails the roadmap's own case**, ten agents posting a
  message a second each into one channel. In five minutes it published about
  a third of what was offered, and posts waited minutes. The cause is an
  index the store lost in v0.3.1 ([finding 1](#1-the-channels-store-lacks-its-channel-time-index)),
  not SQLite: a Postgres store without it would sort the same way.
- **With that one index restored and the society tables on the channels
  store's own connection**, every gate step passed on both platforms: no
  call failed, nothing was lost, and posts committed within 14.2 ms (macOS)
  and 24.2 ms (Linux) of their due time at the 99th percentile. The ramp held
  100 posts a second on Linux and 200 on macOS, ten and twenty times the case
  asked about, with nothing lost even where it failed the latency criterion.
- **With the society tables on a second connection to the same file**, some
  publishes fail outright at every rate ([finding 2](#2-a-second-connection-breaks-publishes)),
  with transactions begun as the store begins them today.

So the society-backend lock can stand, on two conditions that
[RFC 0029's Phase 3 plan](rfcs/0029-phase3-pr-plan.md) makes its first
design points: the index comes back before any society table lands
([ISSUE-0188](issues/ISSUE-0188-channels-store-lost-its-channel-time-index.md)),
and the society store shares the channels store's connection.

---

## What was measured

The benchmark drives the real channels store, `NewSQLiteStore` with its
shipped schema, migrations, full-text index and 10 000-message cap, at the
load a society puts on it. It adds a stand-in for the society tables in the
same file. The roadmap asked about writes alone. A write is the cheap part, so
the benchmark also makes every read a message causes.

### The load one message makes

Each agent posts on a fixed schedule whether or not the store keeps up, so a
slow store shows as waiting time, measured from the moment the post was due,
not as fewer posts; only when 20 000 calls are waiting at once do the
schedules stop, and the step fails as saturated. One post costs the store what
the orchestrator spends on it today:

| Step | Calls | Where it comes from |
|------|-------|---------------------|
| Router reads | `GetChannel`, `GetChannelConfig`, `GetMembers` three times | the publish path, the fan-out and the cascade checks |
| The publish | `PublishMessage`: membership check, insert, full-text index, the cap's count and its prune | `router_publish_async.go` |
| An authority read | the organization graph's version, the sender's role, its managers | what RFC 0012's accept / adapt / refuse step reads |
| Each recipient's window | `GetHistoryScoped(50)` for every other member | the persona's conversation window refetches when a new message arrives |
| One post in ten | `RecallMessages` with two topic words | a persona's `channels:recall` tool |
| One post in twenty | a held action: opened, and 1–10 s later approved and then spent (six in ten), denied (two in ten) or left to expire (two in ten); one hold in five opens a tie pair, so approving it withdraws its twin | RFC 0009's queue and RFC 0028's approval tokens |

Three loops run beside the posts: an expiry sweep every second over pending
and approved rows, an operator
page of the approval queue every two seconds, and an approved organization
change every minute, which writes the approval, a graph edge and the graph
version in one transaction.

A recipient reading its window on every delivery is an upper bound: a persona
that does not take the turn may never build its prompt. One held action every
two seconds is far above any real approval rate. Both make the load heavier
than a real society's.

### The store a long-lived society leaves

Every run starts from a copy of one seeded file in which every channel holds
its full 10 000 messages, so each new post also prunes the oldest. Messages
are 200 to 1 200 bytes of made-up words whose frequencies follow word rank as
in real text, led by English function words. So a recall of two topic words
matches about as many messages as it would on real conversation. The society
tables hold one organization over every agent, the signing key, a graph
version and 2 000 settled approvals.

### What varies

| Axis | Values |
|------|--------|
| Layout | **room**: ten agents in one channel, the roadmap's case. **society**: fifty agents in ten channels of ten, each agent in two and posting in them in turn |
| Schema | **as-is**: the channels store as shipped. **indexed**: plus one index, messages by channel in time order (see [the first finding](#1-the-channels-store-lacks-its-channel-time-index)) |
| Society connection | **shared**: the society calls use the channels store's own single connection. **separate**: they open a second connection to the same file |
| Rate | the gate: 10 messages a second for five minutes. The ramp: 20, 50, 100, 200 and 400 a second for a minute each, indexed only, stopping a series at its first failure |
| Platform | macOS on the development Mac (APFS); Linux in Docker on the same Mac (arm64, ext4 inside the Docker VM) |

### The pass criteria, fixed before the first run

A step passes when all of these hold:

- no call fails;
- at least 99 % of the posts offered are published, and all in-flight work
  finishes within 30 s of the step's end;
- every kind of call answers within 100 ms at the 99th percentile, and every
  kind is called at least once;
- posts commit within 100 ms of their due time at the 99th percentile, and
  none waits more than one second.

A persona's turn takes seconds of model time. A store that adds at most a
tenth of a second at the 99th percentile, loses nothing and never fails a call
is not what limits a society.

---

## Results

Raw numbers are in each run's JSON (see [Reproduce](#reproduce)); the tables
give what the verdict rests on. "Wait p99" is a post's time from its due
moment to its commit, at the 99th percentile.

### Each call alone

The median of 25 calls with nothing else running, in milliseconds, on the
seeded store (macOS / Linux):

| Call | Room, as shipped | Room, indexed | Society, as shipped | Society, indexed |
|------|------------------|---------------|---------------------|------------------|
| Scoped window read, 50 rows | 46.8 / 43.1 | 0.21 / 0.25 | 96.3 / 84.1 | 0.23 / 0.28 |
| Publish at the cap | 12.2 / 11.5 | 1.21 / 1.88 | 21.9 / 19.9 | 1.22 / 1.61 |
| Recall, two topic words | 0.32 / 0.25 | 0.23 / 0.27 | 0.44 / 0.40 | 0.37 / 0.38 |
| Members read | 0.03 / 0.03 | 0.03 / 0.03 | 0.03 / 0.03 | 0.03 / 0.03 |
| Authority read (society) | 0.05 / 0.04 | 0.04 / 0.05 | 0.05 / 0.05 | 0.04 / 0.05 |
| Held action opened (society) | 0.11 / 0.53 | 0.13 / 0.52 | 0.09 / 0.60 | 0.12 / 0.52 |

### The gate: 10 posts a second for five minutes

| Layout | Schema | Society connection | macOS | Linux |
|--------|--------|--------------------|-------|-------|
| Room | as shipped | shared | ❌ 1 014 of 3 000 published; wait p99 274 s | ❌ 1 094 of 3 000; 265 s |
| Room | as shipped | separate | ❌ 925 of 3 000; 280 s; 2 failed publishes | ❌ 887 of 3 000; 282 s; 1 failed |
| Room | indexed | shared | ✅ 3 000 of 3 000; wait p99 13.5 ms, max 86 ms | ✅ 3 000; 23.3 ms, max 50 ms |
| Room | indexed | separate | ❌ 9 failed publishes | ❌ 28 failed publishes |
| Society | as shipped | shared | ❌ 652 of 3 000; 295 s | ❌ 633 of 3 000; 297 s |
| Society | as shipped | separate | ❌ 538 of 3 000; 305 s; 5 failed | ❌ 505 of 3 000; 303 s; 3 failed |
| Society | indexed | shared | ✅ 3 000 of 3 000; wait p99 14.2 ms, max 61 ms | ✅ 3 000; 24.2 ms, max 63 ms |
| Society | indexed | separate | ❌ 5 failed publishes | ❌ 19 failed publishes |

In every indexed, shared step the window reads answered within 7 ms, the
society calls within 9 ms and recalls within 12 ms at the 99th percentile,
and the write-ahead log never passed 4.4 MB.

### The ramp: one minute a step, indexed schema

| Posts a second | Room, shared (macOS / Linux) | Society, shared (macOS / Linux) |
|----------------|------------------------------|---------------------------------|
| 20 | ✅ 13 ms / ✅ 21 ms | ✅ 14 ms / ✅ 24 ms |
| 50 | ✅ 12 ms / ✅ 22 ms | ✅ 13 ms / ✅ 25 ms |
| 100 | ✅ 15 ms / ✅ 27 ms | ✅ 14 ms / ✅ 29 ms |
| 200 | ✅ 46 ms / ❌ 2.1 s | ✅ 54 ms / ❌ 6.4 s |
| 400 | ❌ saturated / — | ❌ saturated / — |

Each cell is the wait p99. The separate-connection series failed at their
first step, 20 a second, on both platforms, with 1 to 26 failed publishes.
Linux at 200 a second lost no post; it failed on latency alone.

---

## Findings

### 1. The channels store lacks its channel-time index

Channels-store v3 (RFC 0031's session migration) replaced
`idx_messages_channel_ts` with `(channel_id, session_id, timestamp DESC)`.
With `session_id` in the middle, that index does not order a channel's rows
by time, so three hot reads lost their index:

- a channel's newest messages (`GetHistory`) sort the whole channel;
- the cap's prune, on every publish once a channel is full, sorts the whole
  channel to find its oldest rows;
- the scoped window every persona reads on each delivery
  (`GetHistoryScoped`) takes the epoch index the v6 migration added, which
  matches every message in the store, and scans them all.

A full channel makes each window read about 45 ms and each publish about
12 ms; ten full channels double both. Every call shares the store's one
connection, so a post's nine window reads queue behind each other and the
store falls behind at ten posts a second. Restoring the index takes the
window read to 0.2 ms and the publish to 1.2–1.9 ms with no query change.
Filed as [ISSUE-0188](issues/ISSUE-0188-channels-store-lost-its-channel-time-index.md).

### 2. A second connection breaks publishes

A publish reads (is the sender a member?) and then writes, in one
transaction that starts as a reader. If a society write on another
connection commits in between, or is still writing, SQLite refuses the
publish's write at once (`SQLITE_BUSY`, and once in a while 517,
`SQLITE_BUSY_SNAPSHOT`): a transaction that already holds a read snapshot
cannot wait for a writer without risking deadlock, so SQLite never calls the
busy handler for it, and `busy_timeout` does not apply.
It happened in every separate-connection step, at every rate, on both
platforms. On the shared connection the same calls queue in Go and none
fails. So, with transactions begun as they are today, the society store
must use the channels store's connection, and any other component that
writes to `data/channels.db` must too.

One form was not measured. If both connections begin their write
transactions as `IMMEDIATE` (the SQLite driver's `_txlock=immediate`
setting, which leaves read-only transactions as they are), a publish takes
the write lock before its membership read and waits under `busy_timeout`
like any other writer. That could let a second connection work, without the
shared connection's rule that a society transaction must make no other
store call. RFC 0029's Phase 3 plan measures it before the society store
settles its connection.

### 3. With both conditions, the one connection is the limit

Each post makes about sixteen store calls, so 100 posts a second is about
1 600 calls a second through one connection. That held on both platforms;
macOS also held 200. Past that the queue grows faster than it drains.
Commits cost more on Linux, where each one reaches the disk through the
Docker VM (the held-action call is 0.5 ms there against 0.1 ms on macOS),
which is why its headroom is half. The write-ahead log stayed under 6 MB in
every step, so checkpoints kept up.

### Harness fixes after the run

The review of this change found two harness bugs: a call class that made no
calls would still have passed, and a scheduler that stopped at the in-flight
cap disabled the drain deadline. Both are fixed in the committed harness.
Neither changed a verdict: every class was called in every step, and no
step stopped at the cap.

A second review found four more, also fixed. The expiry sweep ended only
pending rows, where A4's also ends approved rows never spent; a tie pair
opened in two transactions, where A4 opens a group in one; the society
classes include the three loops, so a step whose posts made no society call
would still have passed, and so would one whose decides and spends all lost
their race. The harness now counts the posts' own authority reads and holds
and fails a step that has none, or any lost race. The recorded runs predate
these fixes. No step that passed lost a race, and in each the posts made
their authority reads and opened their holds. The wider sweep and the
one-transaction tie pair touch a few rows a second and were not re-run;
A1's re-runs of the gate (RFC 0029 plan S-7) measure them.

---

## What the benchmark does not cover

- **One process, no network.** The calls go straight to the store, without
  the REST server, the router's in-memory work or gRPC fan-out. Those cost
  CPU, not store time, and none of them holds the store's connection.
- **One node.** A deployment that needs a second orchestrator needs Postgres
  whatever this shows; the lock already says so.
- **Text.** The corpus is synthetic. Its word frequencies are realistic, but
  a real persona's recall queries are not two random topic words.
- **Durability settings.** The store runs as shipped: write-ahead logging,
  `synchronous=FULL`. On macOS SQLite's `fsync` does not force the drive's
  cache to the platter (`fullfsync` is off), so commits there are cheaper than
  on Linux; that is why both platforms ran.
- **The candidate tables.** The society tables are a stand-in with the rows
  and indexes the RFC 0012 and RFC 0028 PR plans ask of A1. A1 writes the real
  ones and re-runs the gate on them.

## Reproduce

```bash
go test ./internal/channels -run '^TestSocietyStoreBenchmark$' -count=1 -timeout 0 -v -args -society-bench=gate -society-bench-out=/tmp
```

```bash
go test ./internal/channels -run '^TestSocietyStoreBenchmark$' -count=1 -timeout 0 -v -args -society-bench=ramp -society-bench-schemas=indexed -society-bench-out=/tmp
```

`-society-bench=smoke` runs every combination for ten seconds.
`-society-bench-schemas` and `-society-bench-forms` narrow the runs; the
`indexed` schema stops existing as a variant once A1 restores the index, as
the shipped schema then has it. An unknown value fails before seeding. The
JSON written to `-society-bench-out`, a file named for the plan, schemas,
forms and platform, holds each call's median time with nothing else running,
and for every step the counts, the percentiles, the errors and the verdict.
Seeding takes about two minutes; the gate about 45.

## Related documentation

- [Storage architecture roadmap](storage-architecture-roadmap.md) — the question and the SQLite / Postgres split
- [RFC 0029](rfcs/0029-personal-society-storage-split.md) — the society store; its Phase 3 PR plan acts on this result
- [v0.4.0 plan](v0.4.0-plan.md) — row R2 and the society-backend lock
