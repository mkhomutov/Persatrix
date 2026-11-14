---
id: ISSUE-0247
summary: "In a channel that is at its message cap, a reply to the channel's oldest message is accepted and then deleted by its own publish: the cap removes the oldest message, and the reply goes with it. The caller gets `201` for a message that is not stored, and the members are sent a message the channel does not hold."
status: open
severity: low
area: internal/channels
created: 2026-11-14
refs:
  - internal/channels/sqlite_messages.go
  - internal/channels/sqlite_schema.go
  - internal/server/channel_handlers.go
  - docs/issues/ISSUE-0203-thread-route-ignores-the-channel-in-its-path.md
  - https://github.com/mkhomutov/Persatrix/pull/1110
---

# ISSUE-0247: A reply to a full channel's oldest message is deleted by its own publish

## Summary

`PublishMessage` stores the new message first and enforces the
per-channel message cap second, in one transaction
(`internal/channels/sqlite_messages.go`). When the channel is already at
the cap, the new message makes it one too many and the oldest message is
deleted. If the new message is a reply to that oldest message, the
delete takes the reply as well, because `messages.thread_id` is declared
`ON DELETE CASCADE`. The transaction commits and the publish reports
success.

## Context

A throwaway store test with a cap of 3: three messages are published,
then a reply to the first.

- The publish returned no error.
- `GetMessage` for the reply's id returned `message not found`.
- The channel held two messages, not three.

Read from the code and not run: over REST the caller gets `201`. The
handler then looks the stored message up to echo it, fails, and logs the
ERROR `channels: post-publish lookup failed`
(`internal/server/channel_handlers.go`). The router still delivers the
message to the channel's members.

## Impact

- A caller is told a message was stored when it was not.
- The members receive a message that the channel's history does not
  hold, so a persona can answer something no later read will show.
- It needs a channel at its cap (10,000 messages by default) and a reply
  to that channel's oldest message, so it is rare. Nothing is lost from
  another channel.

## Proposed fix / investigation path

Make room before storing: delete the oldest messages down to one below
the cap, then run the thread-parent check and the insert. A reply whose
parent was just removed is then refused like any reply to a message that
does not exist (`400`, `invalid thread_id`), and nothing is stored or
delivered. Check the query plan of the delete stays the one
`pruneOldestSQL` has today.

## Notes

> 2026-11-14 — found while reviewing the fix for ISSUE-0203. The code is
> the same on `main` at `dd006cba`; that fix did not cause it and does
> not change it.
