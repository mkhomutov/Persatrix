---
id: ISSUE-0192
summary: "In v0.4.0 the society store (RFC 0029 Phase 3) — the organization graph, the pending approvals and the key that signs them — is a set of tables in the orchestrator's channels database (the --channels-db file, data/channels.db by default), run on the channels store's one connection. One orchestrator process serves it: a second node cannot share it, so a deployment that needs one needs RFC 0029's Postgres form first, by amendment. A Known Gap of the v0.4.0 plan."
status: open
severity: low
area: society
created: 2026-10-27
refs:
  - docs/rfcs/0029-personal-society-storage-split.md
  - docs/rfcs/0029-phase3-pr-plan.md
  - docs/storage-architecture-benchmark.md
  - docs/v0.4.0-plan.md
  - internal/society/store.go
  - cmd/orchestrator/society.go
---

# ISSUE-0192: The society backend is single-node SQLite

## Summary

v0.4.0 keeps the state a society shares in SQLite tables inside the
orchestrator's channels store, served by that one process. A second
orchestrator cannot share them.

## Context

RFC 0029's
[amendment 2026-10-20](../rfcs/0029-personal-society-storage-split.md#amendment-2026-10-20--v040s-society-backend)
took this form for v0.4.0 in place of the Postgres society store, and only
while SQLite holds. The [storage benchmark](../storage-architecture-benchmark.md)
found that it holds a society's load on one node — ten agents writing to one
channel at a message a second each — once the channels store has its
channel-time index back and the society tables run on the channels store's
own connection. It does not cover a second node, which it says needs Postgres
whatever it shows. The
[Phase 3 plan](../rfcs/0029-phase3-pr-plan.md)'s S-2 puts the society store
on that one connection, and its PR 4 builds the store at startup
(`cmd/orchestrator/society.go`). The v0.4.0 plan lists this as a Known Gap
owned by row A1.

## Impact

- **One orchestrator.** Two orchestrators on one channels database would
  each hold a connection to it: the form S-2 rules out, where a society
  write can fail a publish at once with `SQLITE_BUSY`. SQLite's file
  locking is also not to be trusted on a network file system. The
  orchestrator is already single-replica — the chat reply waiter says so
  at startup — so this adds no new limit, but it ties the society to it.
- **One connection.** Every society call queues behind channel calls on the
  channels store's connection. The benchmark's ramp measured the headroom;
  a society past it slows its channels too.
- **One file.** The society state is backed up, and lost, with the
  channels database, as channel history already is: the `--channels-db`
  file, `data/channels.db` by default and `/var/lib/persatrix/channels.db`
  under Docker Compose.

None of this weakens a check: the limit is on scale and availability, not
on what the store refuses.

## Proposed fix / investigation path

RFC 0029 §D's Postgres society store, taken by amendment when a deployment
needs a second node or outgrows the benchmark's ramp. The society-store
interface, `internal/society.Store`, is the seam: callers never see the
connection, so a Postgres implementation replaces the SQLite one without
changing them. `InTx`'s rule that its function makes no other store call
comes from the one connection, and could relax there.

## Notes

> 2026-10-27 — filed by the RFC 0029 Phase 3 plan's PR 4, as the v0.4.0
> plan's Known Gaps table assigns to row A1.
