---
id: ISSUE-0050
summary: "PublishMessage accepts arbitrarily large msg.Content; add soft byte cap at the SQLite store boundary as defense-in-depth"
status: resolved
severity: low
area: internal/channels
created: 2026-05-08
closed: 2026-05-08
refs:
  - internal/channels/sqlite_messages.go
  - internal/server/channel_handlers.go
  - docs/rfcs/0011-pr-plan.md
---

## Summary

[`channels.PublishMessage`](../../internal/channels/sqlite_messages.go) and the
[REST `POST /api/v1/channels/{id}/messages`](../../internal/server/channel_handlers.go)
handler both accept `Content` of unbounded length. Agent-side validation
(`agents/channel_validation.py::_CHANNEL_CONTENT_MAX_CHARS = 4000`) and the
chat handler (`chatMaxMessageLength = 4000`) cap inputs upstream, but the
unauthenticated REST publish surface is reachable directly. A single
multi-megabyte body would commit straight into SQLite, bypassing every
upstream cap.

## Context

Captured during PR #231 review (Nice-to-Have list, deferred to RFC 0011 PR 8
close-out). Tracked in
[docs/rfcs/0011-pr-plan.md PR 8 checklist line](../rfcs/0011-pr-plan.md#L450)
("soft byte cap on `msg.Content`"). [ISSUE-0049](ISSUE-0049-builddsn-drops-pragmas-on-file-uri-paths.md)
established the precedent of resolving PR 8 close-out NTH items ahead of
PR 8 when the fix is self-contained.

## Impact

- Defense-in-depth gap: REST publish trusts upstream sanitization that does
  not exist on the unauthenticated path.
- DoS amplification: per-channel 10 000-message cap (`DefaultMaxMessagesPerChannel`)
  caps row count but not byte count; a single oversized publish can balloon
  the channel database and the per-recipient gRPC fanout payload.
- Inconsistency with the rest of the surface: the proto comment on
  `ChatRequest.message` documents "max 4000 chars enforced server-side",
  the agent-side enforces it, but the channel publish path does not.

## Proposed fix / investigation path

1. Introduce `MaxMessageContentBytes = 16_384` in [`channels.go`](../../internal/channels/channels.go)
   (4× the 4000-codepoint upstream cap to leave UTF-8 worst-case headroom
   without blocking legitimate agent traffic).
2. Add `ErrMessageContentTooLarge` sentinel.
3. In [`PublishMessage`](../../internal/channels/sqlite_messages.go), reject
   `len(msg.Content) > MaxMessageContentBytes` before opening the transaction.
4. Map the sentinel to HTTP 413 in [`writeChannelError`](../../internal/server/channel_handlers.go).
5. Tests:
   - Unit: store rejects oversized content with the sentinel (no row inserted).
   - Unit: REST handler returns 413 for oversized content.
   - Boundary: `len == cap` accepted; `len == cap + 1` rejected.

## Notes

> 2026-05-08 — captured ahead of RFC 0011 PR 8 close-out. The cap is a soft
> byte limit at the store boundary, not a hard codepoint count — the
> upstream `_CHANNEL_CONTENT_MAX_CHARS` codepoint cap stays the canonical
> user-facing contract. The byte cap exists so an unauthenticated REST
> caller cannot bypass it.
