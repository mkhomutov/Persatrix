# Storage Architecture Benchmark — the re-runs

**Status**: ✅ Gate re-run 2026-10-24 on channels-store v14; 2026-10-26 on the society store
**Parent**: [storage-architecture-benchmark.md](storage-architecture-benchmark.md) — the
2026-10-21 run, its method, its pass criteria and its findings
**Why**: RFC 0029's Phase 3 plan re-runs the benchmark's gate whenever a
change touches what it measured ([S-7](rfcs/0029-phase3-pr-plan.md#phase-0--open-questions-and-design-points)):
PR 1 on the real migration, PR 3 on the real society store, and any later
change that adds a society table. Each re-run is recorded here.

---

## The gate on channels-store v14

Re-run on macOS on 2026-10-24 for RFC 0029's Phase 3 PR 1, on the shipped
schema: the index, and planner statistics the store now gathers when it
opens, which the first run's indexed variant did not have.

| Layout | Society connection | Result |
|--------|--------------------|--------|
| Room | shared | ✅ 3 000 of 3 000; wait p99 37.5 ms, max 421 ms |
| Room | separate | ❌ 1 failed publish |
| Society | shared | ✅ 3 000 of 3 000; wait p99 11.5 ms, max 71 ms |
| Society | separate | ❌ 2 failed publishes |

In both shared steps every kind of call answered within 18 ms at the 99th
percentile, and no race was lost. Unrelated work kept the machine busy
throughout, at a load average of about 8.6 on its 8 cores, and a disk-wide
scan overlapped two minutes of the room step: its one stall, 421 ms, shows
in every kind of call at once.

## The gate on the society store

Re-run on Linux on 2026-10-26 for RFC 0029's Phase 3 PR 3, in a cloud
container with 4 cores (SQLite 3.53.4, Go 1.26). The society calls now run
on the real society store (`internal/society`) and its v15 tables, where
every earlier run used a stand-in. Each call does what the approval queue
and the organization read will do: a decide or a spend reads its row,
checks the expiry and moves the row from the state it read; the authority
read loads the whole graph and finds the agent's role and managers in Go;
an organization change replaces the whole graph and raises its version.

A third connection form joins the two the first run measured. **Immediate**
puts the society store on a second connection, as **separate** does, but
both connections begin every write transaction as `IMMEDIATE` (the driver's
`_txlock=immediate`), so a writer takes the write lock before its first read
and waits for it under `busy_timeout`. The first run's
[finding 2](storage-architecture-benchmark.md#2-a-second-connection-breaks-publishes)
left this form unmeasured.

| Layout | Society connection | Result |
|--------|--------------------|--------|
| Room | shared | ✅ 3 000 of 3 000; wait p99 18.2 ms, max 472 ms |
| Room | immediate | ✅ 3 000 of 3 000; wait p99 10.2 ms, max 35 ms |
| Room | separate | ❌ 2 failed publishes; 5 failed society writes |
| Society | shared | ✅ 3 000 of 3 000; wait p99 11.2 ms, max 101 ms |
| Society | immediate | ✅ 3 000 of 3 000; wait p99 10.5 ms, max 113 ms |
| Society | separate | ❌ 2 failed publishes; 5 failed society writes |

In the four passing steps no call failed and no race was lost; at the 99th
percentile every kind of call answered within 14 ms and the publish wait
within 19 ms, and the write-ahead log never passed 5 MB. Each call alone, with nothing
else running, took about what it took on the stand-in: a held action opened
in 0.5–0.6 ms and the authority read in 0.06 ms (room) and 0.13 ms
(society), where it now reads the whole graph of fifty members. The room's
shared step ran while the same machine built and tested the change; its one
stall, 472 ms, shows in every kind of call at once.

**What the immediate form shows.** On the separate connection, as
transactions begin today, the society store's own writes now fail as well as
publishes: a decide reads its row before it moves it, so it too starts as a
reader that cannot wait for the write lock. With both connections immediate,
nothing failed in either layout. So a second connection works once both
sides begin their writes that way. The society store keeps the shared
connection for now: the immediate form passed in one run on one platform,
where the shared one passed every run on both, and moving to it means
changing how every channel-store transaction begins, a channels-store
change outside this PR. RFC 0029's Phase 3 plan records the choice for
review (S-2).

## Reproduce

The commands are the parent's
([Reproduce](storage-architecture-benchmark.md#reproduce)). The society-store
re-run named all three forms:

```bash
go test ./internal/channels -run '^TestSocietyStoreBenchmark$' -count=1 -timeout 0 -v -args -society-bench=gate -society-bench-forms=shared,immediate,separate -society-bench-out=/tmp
```

It took 31 minutes, seeding included.

## Related documentation

- [Storage architecture benchmark](storage-architecture-benchmark.md) — the first run, its method and its findings
- [RFC 0029 Phase 3 plan](rfcs/0029-phase3-pr-plan.md) — S-2 and S-7, which these re-runs answer
