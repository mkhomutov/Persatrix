---
id: ISSUE-0203
summary: "A message can be threaded under a message from another channel, and the thread route never checks that the message belongs to the channel in its path. A reply in a `secret` channel can be read through a `public` channel's thread route, labelled `public`, and deleting one channel can delete messages posted in another."
status: open
severity: medium
area: internal/server
created: 2026-11-10
refs:
  - internal/server/channel_handlers.go
  - internal/channels/sqlite_schema.go
  - internal/channels/sqlite_messages_read.go
  - docs/rfcs/0037-memory-confidentiality-channel-classification.md
---

# ISSUE-0203: The thread route ignores the channel in its path

## Summary

Two checks are missing on the same boundary:

- **Publish.** `handlePublishMessage` passes the request's `thread_id`
  through. The only check is the store's foreign key, which says the
  parent message exists, not that it is in the same channel.
- **Read.** `handleGetThread` (`internal/server/channel_handlers.go`)
  loads the channel in the path, then loads the message, and throws the
  message away without comparing its channel with the path. It then
  returns the thread labelled with the path channel's classification.

## Context

A probe against an in-process server with two channels, `group:lobby`
(`public`) and `group:board` (`secret`):

1. `GET /api/v1/channels/group:lobby/messages/<a board message>/thread`
   returned the board's reply with `"classification":"public"`.
2. A post into the lobby carrying a board message's id as `thread_id` was
   accepted (201) and then appeared in the board's thread, labelled
   `secret`.
3. `DELETE /api/v1/channels/group:board` removed that lobby message,
   because `messages.thread_id` is declared `ON DELETE CASCADE`
   (`internal/channels/sqlite_schema.go`).

The per-channel message cap deletes the same way. In a second probe with
a cap of 3, pruning a parent message in the board removed its reply in
the lobby.

## Impact

- The classification label on a thread response can be wrong in either
  direction. RFC 0037 says a thread is never more or less confidential
  than the channel it forks from.
- A message can be lost from one channel by an action on another.
- The read needs no access the caller lacks today, since any caller who
  may read one channel's thread may read another's. The harm is the wrong
  label and the cross-channel delete.

## Proposed fix / investigation path

Refuse a publish whose thread parent is in another channel, in the
store's `PublishMessage` so that every caller is covered. Answer 404 on
the thread read unless the message's `channel_id` equals the path's.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; reproduced with a throwaway Go test mapped in with
> `go test -overlay`.
