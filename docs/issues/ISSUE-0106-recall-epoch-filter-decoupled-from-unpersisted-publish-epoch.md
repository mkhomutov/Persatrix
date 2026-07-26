---
id: ISSUE-0106
summary: "Recall's `epoch_id` override filters `messages.epoch_id`, but publish never persists a non-'live' epoch (ISSUE-0085 keeps the column default), so an explicit non-'live' epoch recalls nothing through the real path — and RFC 0036 §OQ-6 cross-run isolation is unenforced if separate runs share one channel-store DB"
status: resolved
severity: medium
area: channels
created: 2026-06-19
closed: 2026-07-26
refs:
  - docs/rfcs/0036-persona-message-recall.md
  - docs/rfcs/0036-pr-plan.md
  - docs/issues/ISSUE-0085-epoch-axis-run-isolation.md
---

## Summary

The RFC 0036 recall endpoint (`POST /api/v1/personas/{participant_id}/recall`,
PR #677) accepts an `epoch_id` body override and binds it to the store's strict
`m.epoch_id = ?` filter (the §OQ-6 "lock"). But the publish path deliberately
never stamps a non-`live` epoch on the persisted message — the override rides the
gRPC dispatch rail only (ISSUE-0085), and `messages.epoch_id` keeps its
`DEFAULT 'live'`. So recall's epoch axis and publish's epoch axis are
**decoupled**:

- a message "published into" any non-`live` epoch is stored as `live` and is
  **unreachable** when recalled under that epoch;
- conversely, if separate runs/epochs ever share one channel-store DB, every
  message is `live` and a persona in run A **can** recall run B's verbatim
  messages — the exact isolation breach §OQ-6 was written to prevent, currently
  **unenforced**.

## Context

Found during the deep review of PR #677 (RFC 0036 PR 3). The implementation
faithfully follows the RFC; the defect is in the RFC's *premise* that
`messages.epoch_id` is meaningfully populated per run.

Chain of evidence:

- Recall binds the body epoch into the filter —
  [`persona_recall_handlers.go`](../../internal/server/persona_recall_handlers.go)
  (`EpochID: channels.EpochOverrideFromContext(ctx)`), filtered by
  [`sqlite_search.go:76`](../../internal/channels/sqlite_search.go)
  (`m.epoch_id = ?`).
- The production message INSERT omits `epoch_id` —
  [`sqlite_messages.go:133`](../../internal/channels/sqlite_messages.go) — so the
  column takes its `TEXT NOT NULL DEFAULT 'live'`
  ([`sqlite_migrations.go:174`](../../internal/channels/sqlite_migrations.go)).
- Publish's epoch override is deliberately not persisted —
  [`channel_handlers.go:280`](../../internal/server/channel_handlers.go) ("The
  epoch is not stamped on the persisted row (unlike the session)") and
  [`channel_epoch_override.go`](../../internal/server/channel_epoch_override.go).
- `ChannelMessage` has no epoch field, and no non-test code writes a non-default
  `messages.epoch_id` (verified by grep).
- The RFC mandates the field + filter regardless: body `epoch_id?` in the PR 3
  row, the two-hop resolution and the claim "recall and publish agree on the
  epoch axis" in the PR 2 "Epoch binding" note, and the non-optional §OQ-6 lock —
  all in [`0036-pr-plan.md`](../../docs/rfcs/0036-pr-plan.md).

Pinned by `TestRecallEndpoint_RealPublishPath_ExplicitEpochUnreachable`
([`persona_recall_handlers_test.go`](../../internal/server/persona_recall_handlers_test.go)),
added in `9ce3a26`: publishes via REST with `epoch_id="ci-run-7"` and asserts the
row is unreachable under `ci-run-7`, reachable under `live`. That test is the
tripwire that flips red the day publish begins persisting a per-run epoch.

## Impact

Two faces, both gated on the deployment model (do separate runs share one
channel-store DB?):

- **Footgun (always):** the `epoch_id` override silently returns
  `{"messages": []}` for every real message whenever it is anything but `live`. A
  caller (or the PR 4 persona tool) that passes a run epoch gets empty results
  with no error.
- **Isolation gap (if runs share a store):** §OQ-6 frames the epoch filter as a
  correctness/security requirement against cross-run recall. Because epoch is not
  persisted, that guarantee is **not delivered** — co-resident runs' messages are
  all `live` and mutually recallable (subject only to the membership join).

No active production breach is confirmed — the default `live` path returns
correct results — so severity is medium pending the deployment-model answer
(escalate to high if runs are confirmed to share a store).

## Proposed fix / investigation path

Decide between two **opposite** directions; the choice hinges on whether separate
runs/epochs ever share a single channel-store DB:

- **(a) Make isolation real** — persist the resolved epoch onto the channel-store
  row at publish (and any other writer), so `m.epoch_id` actually partitions runs
  and §OQ-6 is enforced. Reverses part of ISSUE-0085's "channel store keeps its
  `epoch_id` default; isolation is persona-side via the gRPC rail" decision;
  larger blast radius (schema semantics, every message writer, migrating existing
  `live` rows). Right answer **if** runs share a store.
- **(b) Drop the moot axis** — accept ISSUE-0085's single-epoch channel store,
  remove the `epoch_id` body override from recall (the footgun), and amend RFC
  0036 §OQ-6 to record that the channel store is not epoch-partitioned and the
  filter is vestigial / forward-looking. Right answer **if** runs are physically
  isolated (separate DBs per run).

Either way, also correct the stale "recall and publish agree on the epoch axis"
claim still present in the PR #677 description and
[`0036-pr-plan.md`](../../docs/rfcs/0036-pr-plan.md). The inline
handler/types/test comments were already corrected in `9ce3a26`.

## Notes

> 2026-06-19 — captured during the deep review of PR #677 (RFC 0036 PR 3). The
> review's honest-docs + tripwire-test fixes landed in `9ce3a26` (no behavior
> change); this issue tracks the unresolved epoch decoupling itself, which needs
> the deployment-model decision above.

> 2026-07-25 — **Slotted into v0.3.12, direction (b)**, at the
> [v0.3.12 plan opening](../v0.3.12-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-07-25).
> Deployment model confirmed by the maintainer: separate runs/epochs never
> share a channel-store DB (isolation is physical), so the recall `epoch_id`
> body override is dropped, RFC 0036 §OQ-6 is amended, and the decoupling
> tripwire test retires with the axis. Rides
> [RFC 0037 PR 5](../rfcs/0037-pr-plan.md) — the same PR that reworks the
> recall endpoint's parameters for the §F classification filter, so the
> surface changes once.

> 2026-07-26 — **RESOLVED, direction (b), in RFC 0037 PR 5**
> (`feature/v0312-rfc0037-recall-filter`). The `epoch_id` body field was
> removed from `POST /api/v1/personas/{participant_id}/recall`; any presence
> — `"live"` and the empty string included — is a 400 naming this issue
> (silent acceptance would imply an isolation axis that does not exist),
> pinned by `TestRecallEndpoint_EpochOverrideRemoved_PointedRejection`. The
> handler no longer routes through `resolveEpochOverride` /
> `EpochOverrideFromContext` (publish still does — its dispatch-rail
> override is untouched); `RecallParams.EpochID` stays as the store-level
> strict-equality `"live"` filter, now documented as a vestigial guard.
> `TestRecallEndpoint_RealPublishPath_ExplicitEpochUnreachable` retired with
> the axis (a tombstone comment marks it); the stale "recall and publish
> agree on the epoch axis" claim in `0036-pr-plan.md` and RFC 0036 §OQ-6
> were both amended.
