---
id: ISSUE-0262
summary: "The history endpoint pages by timestamp alone, so the web console cannot page past a run of messages that share one timestamp once the run is as long as a page: a hole marker that can never be loaded (200 messages), or `Show older messages` going away with older messages unread (50). A message stored later with the timestamp of one already read can also be missed with no sign."
status: open
severity: low
area: web
created: 2026-11-15
refs:
  - https://github.com/mkhomutov/Persatrix/pull/1128
  - web/src/lib/backfill.svelte.js
  - web/src/lib/timeline.js
  - internal/channels/sqlite_messages_read.go
  - internal/server/channel_handlers.go
  - docs/issues/ISSUE-0222-console-skips-messages-past-one-page-between-polls.md
---

# ISSUE-0262: The console's paging cannot pass messages that share a timestamp

## Summary

`GET /api/v1/channels/{id}/messages` takes one cursor, `before`, and
returns the messages strictly older than it, newest first. The console
pages back by asking for the messages older than one nanosecond after
the oldest one it holds, and drops by id the ones it already shows.
That works while fewer messages than a page share a timestamp. Past
that, no cursor value moves on:

1. **A hole that cannot be loaded.** 200 or more messages with one
   timestamp inside a hole: the catch-up page (200) returns the same
   messages again. The marker stays, says "Could not load the missing
   messages: the server returned only messages already shown.", and
   every click ends the same way.
2. **Older messages cut off.** 50 or more messages with one timestamp
   at the oldest end of what is shown: the older-messages page (50)
   brings nothing new, the console takes **Show older messages** away
   and shows "Start of the conversation", with older messages unread.
   On `main` at `dd2cceb1` too.
3. **A late message with an old timestamp.** A message stored after
   the console read another one with the same timestamp is listed
   after it. If a page ends exactly between the two, or the catch-up
   meets the first one as the last row of a page, the later one is not
   read and nothing marks it. The same goes for the operator's own
   post when the catch-up takes it off the screen to read it back
   (the fix for
   [ISSUE-0222](ISSUE-0222-console-skips-messages-past-one-page-between-polls.md)):
   it comes back only if a page returns it before the page meets a
   message already read.

## Context

- The store's query orders by timestamp alone
  (`internal/channels/sqlite_messages_read.go`). Messages that share a
  timestamp come back in the order they were stored, the first stored
  listed first. Nothing in the query promises that order.
- The store stamps a message with the host's clock when it is stored
  and the caller gave no time (`internal/channels/sqlite_messages.go`),
  so two messages of one channel rarely share a timestamp and a run of
  50 is not expected. A caller that supplies its own timestamps, or a
  clock that is set back, can produce one.
- Cases 1 and 2 are pinned by tests as the behaviour today
  (`web/src/lib/backfill.test.js`,
  `web/src/panels/ChannelTimeline.history.test.js`) and were described
  in the note of ISSUE-0222. Case 3 was found with generated histories
  in the review of that fix; the console's tests do not cover it.
- Until the pull request in `refs`, the console's stand-in for the
  history endpoint listed tied messages the other way round from the
  store. It now lists them as the store does, and the tie cases run
  both ways.

## Impact

None expected on a channel written to through the orchestrator at its
normal pace. On a channel with long runs of equal timestamps, an
operator cannot read part of the history in the console (cases 1 and
2, both visible on screen), or misses single messages (case 3, with no
sign). A page reload shows the newest 50 again and does not help with
cases 1 and 2.

## Proposed fix / investigation path

Give the history endpoint a cursor that also carries the message id
(or the store's row order), and order by timestamp and then by that,
so that a page can start in the middle of a run. That is a change to
the REST route and the store query, with the console following. Until
then the limits above stand.

## Notes

> 2026-11-15 — recorded from the review of the fix for ISSUE-0222. The
> first two limits were accepted there and written only in that
> issue's note; a resolved issue is the wrong place to keep them.
> Severity `low` is proposed, not ruled: the trigger is not expected
> in normal use, and two of the three cases say so on screen.
