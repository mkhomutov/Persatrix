---
id: ISSUE-0203
summary: "A message can be threaded under a message from another channel, and the thread route never checks that the message belongs to the channel in its path. A reply in a `secret` channel can be read through a `public` channel's thread route, labelled `public`, and deleting one channel can delete messages posted in another."
status: resolved
severity: medium
area: internal/server
created: 2026-11-10
closed: 2026-11-14
closed_pr: 1110
refs:
  - internal/server/channel_handlers.go
  - internal/server/channel_thread_handler.go
  - internal/server/channel_errors.go
  - internal/channels/sqlite_messages.go
  - internal/channels/sqlite_thread_channel_test.go
  - internal/server/channel_thread_route_test.go
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

> 2026-11-13 — fixed. Checked against the code at `dd006cba` first: all
> three steps of the probe above reproduce, and one thing is wider than
> written. A publish whose `thread_id` names no message at all answered
> `500` with `channel store error`, because the store's foreign-key
> error was not one the REST layer maps.
>
> - **Publish.** `PublishMessage` looks the parent up inside its
>   transaction and refuses the message unless the parent is in the same
>   channel (`ErrThreadParentNotInChannel`, a `400` over REST). A parent
>   that does not exist gets the same error, so a refusal does not say
>   whether an id exists in another channel. The check is in the store,
>   so the router and the REST fallback with no router are both covered.
> - **Read.** `handleGetThread` answers `404 message not found` unless
>   the message's channel is the path's. It moved to
>   `internal/server/channel_thread_handler.go`, because
>   `channel_handlers.go` was at the 500-line warning.
> - **Rows from before.** `GetThread` returns only the replies in the
>   parent's own channel, so a reply an older version stored under
>   another channel's message is not listed under that channel's label.
>   Its query keeps reading by the thread index
>   (`TestThreadQuery_ReadsOnlyTheThread`).
>
> Not repaired: such an older reply still has the other channel's
> message as its parent, so it is still deleted with it. Nothing in the
> agent runtime sends a `thread_id` on a publish, so only a REST or CLI
> caller that named another channel's message can have made one.

> 2026-11-14 — review of the fix. One gap was found and closed, and one
> thing the note above says is wider than written.
>
> - **A new reply could still be lost.** The check looked at the
>   parent's channel only. An older reply that sits in this channel under
>   another channel's message passed it, so a new reply under that one
>   was accepted and then deleted with the other channel's message: the
>   cascade runs down the whole thread. The check now walks from the
>   parent to the top of its thread and refuses the reply if any message
>   on the way is in another channel
>   (`TestSQLiteStore_PublishMessage_RefusesAReplyUnderAThreadThatLeavesTheChannel`).
>   No message published since the fix can be removed by an action on
>   another channel.
> - **Still not repaired.** An older crossing reply, and every reply
>   stored below it before the fix, is deleted with the other channel's
>   message. `SELECT r.id FROM messages r JOIN messages p ON p.id =
>   r.thread_id WHERE p.channel_id <> r.channel_id` lists the crossing
>   replies.
> - The membership rule runs before the thread check, so a sender
>   outside the channel gets the same refusal whatever the thread id
>   names. That order is now pinned by a test.
> - The store's old foreign-key branch for a `thread_id` that names no
>   message can no longer be reached and was removed with its test; the
>   new table covers the case.
> - Found on the way, in code this fix did not change: a reply to a full
>   channel's oldest message is deleted by its own publish
>   ([ISSUE-0247](ISSUE-0247-reply-to-a-full-channels-oldest-message-is-deleted-by-its-own-publish.md)).
