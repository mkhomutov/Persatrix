---
id: ISSUE-0100
summary: "Display-name mention lift snapshots the whole agent registry per @-bearing publish; scope the lookup to channel membership before registries grow hot"
status: open
severity: low
area: internal/server
created: 2026-06-13
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
