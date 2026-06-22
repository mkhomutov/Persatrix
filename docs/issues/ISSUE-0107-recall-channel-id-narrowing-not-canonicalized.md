---
id: ISSUE-0107
summary: "The persona recall endpoint forwards the body `channel_id` narrowing parameter to the store UNMODIFIED (persona_recall_handlers.go:90), with no `group:`/`dm:` canonicalization — unlike the channel REST paths, which canonicalize their id. The store matches `messages.channel_id` against the canonical (`group:`-prefixed) id, so a caller that narrows by the bare, human-facing channel name (e.g. `mt-recall-001` rather than `group:mt-recall-001`) gets ZERO results. Observed live in MT-PERSONA-RECALL-001 Step 5: ember-owl's own recall tool call narrowed by the bare name `mt-recall-001` and got count=0, so it fell back to its conversation transcript. Non-exposure (narrowing can only REDUCE the result set, never widen scope past the membership filter) but it silently breaks channel-narrowed recall for the natural id form."
status: resolved
severity: medium
area: internal/server
created: 2026-06-22
closed: 2026-06-22
closed_pr: 685
refs:
  - docs/rfcs/0036-persona-message-recall.md
  - docs/manual-tests/MT-PERSONA-RECALL-001.md
  - docs/manual-tests/v0.3.9-execution-report.md
---

## Summary

The `POST /api/v1/personas/{participant_id}/recall` endpoint accepts an optional
`channel_id` **narrowing** parameter in the request body and forwards it to the
store **verbatim** — there is no `group:` / `dm:` canonicalization. The channel
store matches `messages.channel_id` against the canonical, prefixed id, so a
caller that narrows by the **bare, human-facing channel name** gets an **empty**
result set even when in-scope messages exist.

This is **not** a data-exposure defect: narrowing can only *reduce* the result
set, never widen it past the RFC 0035 membership `EXISTS` filter. It is a
**functional degradation** — channel-narrowed recall silently returns nothing
for the id form a persona (or a human) naturally uses.

## Context

Found live during the first execution of
[MT-PERSONA-RECALL-001](../manual-tests/MT-PERSONA-RECALL-001.md) (2026-06-22,
tip `2bd72a8`, recorded in the
[v0.3.9 execution report](../manual-tests/v0.3.9-execution-report.md)). In
Step 5 the persona `ember-owl` reached for `recall_channel_messages` to quote
the agreed deploy window. The server-side `channel.recall` audit event records
the fields the tool call resolved to (the actual `emitRecallAudit` schema —
`channel_id` / `result_count` / `epoch_id`, never `channel` / `count`):

```
{"query":"deploy window decision","epoch_id":"live","channel_id":"mt-recall-001","result_count":0,"limit":10}
```

It narrowed by the **bare** `mt-recall-001`, got `result_count=0`, and (honestly) fell
back to its conversation transcript: *"The channel search came back empty …
That said, it's in our current conversation transcript."* The deterministic
endpoint, queried with the canonical id, returns the full in-scope set:

```
channel_id=mt-recall-001        → 0      # bare name (persona's form)
channel_id=group:mt-recall-001  → 7      # canonical
channel_id omitted (un-narrowed)→ 7      # all accessible channels
```

(The later un-narrowed Step 5b call — `channel=null`, `query="emergency rollback
key"` — returned the in-scope hit and correctly **excluded** the out-of-scope
removal-gap message, so the membership filter and the data-exposure gate are
intact; this issue is strictly about the narrowing param's id form.)

## Mechanism

[`persona_recall_handlers.go:90`](../../internal/server/persona_recall_handlers.go)
binds the body field straight into the store params:

```go
params := channels.RecallParams{
    ParticipantID: participantID,
    Query:         req.Query,
    EpochID:       channels.EpochOverrideFromContext(ctx),
    ChannelID:     req.ChannelID, // ← forwarded unmodified; no canonicalization
    ...
}
```

By contrast the channel REST surface canonicalizes the channel id taken from the
URL path (the `canonicalID := "group:" + name` convention noted in the MT
preconditions and applied by the channel handlers). The recall endpoint takes
`channel_id` from the **body**, not the path, so it never passes through that
canonicalization. The `participant_id` (the access-scope key) comes from the
path and is closure-bound — unaffected; only the narrowing `channel_id` is raw.

## Impact

- A persona that narrows recall by the channel name it sees in its context gets
  **silent empty results** — the channel-narrowed recall path is effectively
  unusable for the natural id form. The persona degrades gracefully (transcript
  fallback, no fabrication), so it is not user-visibly broken, but the recall
  tool's `channel_id` argument does not do what it appears to.
- **No data-exposure / scope-widening risk.** The membership `EXISTS` filter is
  applied regardless; a malformed/bare `channel_id` can only return a subset
  (here, the empty set). The v0.3.9 data-exposure gate (the removal-gap message
  is unreachable) holds — proven both deterministically and live.

## Proposed fix

Canonicalize `req.ChannelID` in the recall handler before binding it into
`RecallParams`, so a bare `mt-recall-001` and a canonical `group:mt-recall-001`
narrow identically. The channel path handlers do not expose a reusable helper —
they inline `canonicalID := "group:" + req.Name` (channel_handlers.go) — so the
recall handler grows a small sibling helper applying the same `group:` convention.
Mirror it on the `sender` field only if senders are similarly namespaced (they
are not — leave as-is). Add an endpoint-level test
(`persona_recall_handlers_test.go`) asserting a bare-name narrow returns the
same set as the canonical narrow.

Implemented as described below (see **Resolution**) — folded into the same
release-prep PR 1 ([#685](https://github.com/mkhomutov/Persatrix/pull/685)) that
surfaced it rather than deferred, since it is a one-line canonicalization with a
single pinning test.

## Severity

**Medium** — a shipped feature path (channel-narrowed recall) silently returns
empty for the natural id form, observed live; no correctness/exposure risk, and
the un-narrowed path and the deterministic canonical path both work.

## Resolution

Fixed by canonicalizing the body `channel_id` narrower in the recall handler
before it is bound into `RecallParams`. The new `canonicalNarrowChannelID` helper
([persona_recall_handlers.go](../../internal/server/persona_recall_handlers.go))
prepends the `group:` prefix to a bare name (`mt-recall-001` →
`group:mt-recall-001`), mirroring the `canonicalID := "group:" + req.Name`
convention the channel path handlers use (channel_handlers.go), while leaving an
already-prefixed id (`group:`/`dm:`/`thread:`) and the empty/un-narrowed case
untouched. `sender` is left raw — senders are not namespaced. A bare and a
canonical narrow now match the same rows; the change is narrowing-only and cannot
widen scope past the RFC 0035 membership `EXISTS` filter. Pinned by
`TestRecallEndpoint_BareChannelNarrowMatchesCanonical`
([persona_recall_handlers_test.go](../../internal/server/persona_recall_handlers_test.go)).

A bare (un-prefixed) id is deliberately treated as a **group** name: `group:` is
the only id the system mints from a bare, human-facing channel name (the
`channel_handlers.go` create path), and DM/thread channels carry composite ids
(`dm:<a>:<b>`, `thread:<…>`) a caller passes through already-prefixed. So the
bare→`group:` mapping is the complete fix for every channel a persona can name by
its bare form; there is no bare id that should resolve to a DM/thread.

**Landed in this PR (release-prep PR 1), not deferred.** F-1 was found live while
executing `MT-PERSONA-RECALL-001` at tip `2bd72a8`; rather than carry it as a
fast follow, the one-line canonicalization fix + its pinning test were folded
into the same PR (commit `dbe4d69`). The execution report records it as
found-and-fixed here.
