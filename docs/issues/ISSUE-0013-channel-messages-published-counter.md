---
id: ISSUE-0013
summary: channel.messages.delivered counter has no companion published counter; delivered/published ratio dashboard not computable
status: resolved
severity: low
area: internal/observability
created: 2026-05-04
closed: 2026-05-07
closed_pr: 269
refs:
  - docs/rfcs/0011-channels.md
  - docs/observability.md
---

## Summary

PR #245 added `channel.messages.delivered{channel_type, status}` in
`internal/observability/metrics/metrics.go`. There is no companion
`channel.messages.published` counter, so publishes that succeed the store
commit but find zero eligible recipients (RespondNever short-circuit,
sender-only channel, all members filtered) are invisible. The delivered/
published ratio dashboard suggested in the CHANGELOG observability blurb
cannot be computed.

## Context

Captured during PR #245 deep review (Nice-to-have #2). The router
currently increments delivered after the per-recipient dispatch loop;
adding a publish-side counter is a one-line addition at the top of
`Publish()` after the type/prefix validation passes.

## Impact

- Observability gap: ratio metrics not available.
- Operators cannot distinguish "no traffic" from "lots of traffic, all
  filtered" without log-scraping.

## Proposed fix / investigation path

1. Add `ChannelMessagesPublished` counter alongside
   `ChannelMessagesDelivered` with the same label set
   (`channel_type, status`).
2. Increment in `internal/channels/router.go` `Publish()` immediately
   after the store commit succeeds — i.e. once per accepted publish,
   regardless of fanout outcome.
3. Update `docs/observability.md` with the new metric and a sample
   `delivered / published` PromQL query.
4. Add a test asserting both counters tick on a successful publish, and
   only the published counter ticks on a RespondNever short-circuit.

## Notes

> 2026-05-04 — initial capture during PR #245 review (Nice-to-have #2).
>
> 2026-05-07 — resolved by adding the `channel.messages.published`
> Int64 counter (labelled by `channel_type` only — no `status`, since
> publishes that fail validation never reach this point) to the
> orchestrator instrument inventory; threading it through
> `RouterMetrics` and `ChannelRouter.Publish` so it ticks once per
> accepted publish, after the store commit and before fanout. Docs
> updated in `docs/observability.md` §11.3 with the
> delivered/published ratio PromQL. Test coverage in
> `TestChannelRouter_Publish_PublishedCounterTicks`
> (RespondNever-only fanout: published fires, delivered does not) and
> `TestChannelRouter_Publish_PublishedCounter_PerType` (label-set
> stability across repeated publishes).
