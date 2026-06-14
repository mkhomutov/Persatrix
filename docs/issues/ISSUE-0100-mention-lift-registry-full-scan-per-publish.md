---
id: ISSUE-0100
summary: "RESOLVED 2026-06-14 — display-name mention lift no longer snapshots the whole agent registry per @-bearing publish; the name lookup is scoped to channel membership via a new Registry.NamesFor(ids) batch read (Option 3). Microbench on a 5000-agent directory / 6-member channel: 1.72 ms → 261 ns, 1.33 MB → 336 B, 5021 → 2 allocs."
status: resolved
severity: low
area: internal/server
created: 2026-06-13
closed: 2026-06-14
refs:
  - docs/rfcs/0011-amendment-display-name-mention-lifting.md
---

## Summary

[`Server.liftContentMentions`](../../internal/server/channel_mention_lift.go)
(the RFC 0011 display-name-mention-lifting wiring, #619) calls
`s.registry.List(ctx)` — a snapshot of the **whole** agent directory — on
every publish whose content contains an `@`, even though it then consults
only the names of the *channel's* members. The lift builds an id→name map
from the full directory and discards every entry that is not a member of
the target channel.

## Context

For the in-memory registry that ships today this is a cheap map copy under
a read lock, so the cost is negligible and shipping the full scan kept the
PR-3 wiring simple (the resolver itself is membership-scoped — only the
*name source* over-reads). The concern is forward-looking: once the
registry is backed by something with real per-`List` cost (a remote store,
a large multi-tenant directory) and a channel is on a hot publish path,
this becomes an O(registry) read per `@`-bearing message where an
O(members) read would do. The `@`-prefix short-circuit at the top of
`liftContentMentions` already skips the lookups entirely for mention-free
prose (the dominant case), so the waste is bounded to messages that
actually address someone — but on an active channel that is most of them.

A code comment at the `List` call site flags this as a deliberate,
tracked deferral (see `channel_mention_lift.go`).

## Impact

None today (in-memory registry, small fleets). Latent: a per-publish
O(registry) directory scan on the channel hot path the moment the registry
gains non-trivial `List` cost.

## Proposed fix / investigation path

Scope the name lookup to the channel's members rather than the whole
directory. Options, cheapest-first:

- **Per-member `registry.Get`** — N small lookups for an N-member channel
  instead of one whole-directory `List`. Simplest; trades one big read for
  a few small ones (a win once `List` ≫ `Get × members`).
- **Cached directory snapshot** — memoize the id→name map with a short TTL
  / registry-version invalidation, shared across publishes. Best when many
  publishes hit the same membership.
- **Membership-join helper on the registry** — a `NamesFor(ids)` batch
  that the store/registry can satisfy in one scoped query.

Pick per the registry backing that actually lands; the lift's call site is
the only consumer, so the change is local. Pair with a microbenchmark on a
synthetic large directory to confirm the chosen path before enforcing.

## Resolution

**2026-06-14 — fixed via Option 3 (membership-join batch on the registry).**

Options 1 and 2 were rejected:

- **Per-member `Get` (Option 1)** optimizes the wrong axis for the very
  scenario the issue hedges against: on a remote backing, N `Get`s are N
  round-trips, which can be *worse* than one `List`. In-memory it also
  deep-copies each agent it touches.
- **Cached snapshot (Option 2)** still reads the whole directory (just less
  often) and adds TTL/invalidation plus rename staleness — a speculative
  mitigation of a publish pattern that does not exist yet, and the one option
  with a correctness cost. Deliberately not built.

Option 3 is the only choice that is a strict improvement across *both*
backings — single scoped read, membership-bounded by construction — and it
is a real win even on today's in-memory registry, where `List` deep-copies
*and sorts* the entire directory (including every agent's `Capabilities`
slice) just to read a handful of `Name` strings. The issue's "cheap map
copy" framing understated that cost.

Change (one PR, local to the call site as predicted):

- Added `Registry.NamesFor(ctx, ids) (map[string]string, error)` to the
  interface and `InMemoryRegistry` — one `RLock`, N map reads, copies only
  the name strings (no `AgentInfo` deep-copy, no sort). Missing ids are
  omitted, preserving the lift's existing "id-only on a registry miss"
  fail-open.
- [`Server.liftContentMentions`](../../internal/server/channel_mention_lift.go)
  now builds `memberIDs` from the channel membership and calls
  `NamesFor(memberIDs)` instead of `List()`.

TDD: registry-layer `TestNamesFor` (scoping, missing-id omission, non-nil
empty) written red-first; the lift's `countingRegistry` spy was flipped from
counting `List` to counting `NamesFor` + asserting it receives *exactly* the
member ids and that `List` is never called; a new
`TestLiftContentMentions_NamesForErrorDegradesToIDOnly` pins the fail-open
leg (in-text id still lifts, display name no-lifts on a `NamesFor` error).

Microbenchmark (`registry_names_for_bench_test.go`, 5000-agent directory,
6-member channel) confirms before enforcing:

| path | ns/op | B/op | allocs/op |
|------|-------|------|-----------|
| whole-directory `List` + extract | 1,721,813 | 1,332,035 | 5021 |
| scoped `NamesFor` | 261 | 336 | 2 |

~6,600× faster, ~4,000× less allocated. `List`'s figure is its real cost,
not an estimate: the baseline calls the production `List`, so its ~5000
allocs/op *are* the per-agent `Capabilities` deep-copies — exactly the work
`NamesFor` skips.
