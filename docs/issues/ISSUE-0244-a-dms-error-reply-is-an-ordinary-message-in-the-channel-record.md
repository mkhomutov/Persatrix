---
id: ISSUE-0244
summary: "The error reply a persona publishes on a DM, for a wallet denial or for its own failed turn, is an ordinary message in the channel record. It is replayed to the model as the persona's own earlier words. (One published after the chat request had given up could also be returned as the answer to the person's next request; the fix for ISSUE-0200 closed that half.)"
status: open
severity: low
area: agents/chat_reply
created: 2026-11-13
refs:
  - agents/chat_reply.py
  - agents/persona_runtime/conversation_window.py
  - agents/persona_runtime/__init__.py
  - internal/server/chat_handler.go
  - docs/issues/ISSUE-0199-persona-failures-travel-as-completed-work.md
  - docs/issues/ISSUE-0200-late-chat-reply-answers-the-next-question.md
  - docs/issues/ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md
  - https://github.com/mkhomutov/Persatrix/pull/1107
---

# ISSUE-0244: A DM's error reply is an ordinary message in the channel record

## Summary

When a persona cannot answer on a DM it publishes an error reply there,
tagged `reply_status: error` (`agents/chat_reply.py`). The tag is read
by one consumer, the REST chat handler, which uses it to answer HTTP 200
with an error status. To everything else the reply is a message from the
persona like any other. Two things follow.

## Context

**It is replayed as the persona's own words.** The persona builds each
prompt from the DM's recent messages and gives its own messages the role
`assistant` (`agents/persona_runtime/conversation_window.py`). Nothing
there looks at `reply_status`. So after a failed turn the model's next
prompt holds, as something it said, `LLM provider error`, or a wallet's
`per_agent budget exceeded for … (spent=… limit=…)`. Read from the
code; no prompt was captured.

**A late one can answer the wrong request.** The chat request waits 30
seconds by default and 300 at most (`internal/server/chat_handler.go`).
A persona's event time-out (`event_timeout`) is 300 seconds unless its
config sets another, and no shipped config does. So when a turn times
out, the request has already ended in a 504 by the time the error reply
`Event processing timed out after 300s` is published. A reply is matched
to a request by channel and sender only
([ISSUE-0200](ISSUE-0200-late-chat-reply-answers-the-next-question.md)),
so that reply is what the person's next request on the DM gets back.
The constants and the absence of `event_timeout` from the shipped
configs were checked; the late reply was not run against an
orchestrator.

The wallet-denial reply has had the first property since it was added
([ISSUE-0065](ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md)).
The fix for
[ISSUE-0199](ISSUE-0199-persona-failures-travel-as-completed-work.md)
gave the same reply to a persona's own failures, the time-out among
them, which is where the second comes from.

## Impact

Small each time. A model that reads an error line as its own last turn
may apologise for it or repeat it. A person who retries after a 504 can
get the previous turn's time-out notice as the answer to the retry, and
from then on is exposed to the off-by-one that ISSUE-0200 describes.

Proposed low: nothing is lost, and the mis-answered request is
ISSUE-0200's fault to fix. The severity is proposed here, not ruled.

## Proposed fix / investigation path

One question decides both: is an error reply part of the conversation,
or a notice about it? If a notice, keep it out of the window the prompt
is built from (the tag is already on the message), and do not publish
one for a turn that ended after its request could still be waiting. The
second half needs a ruling of its own: whether a late error reply
should be published at all. ISSUE-0200's fix, matching a reply to the
message it answers, would stop a late reply from answering the next
request whichever way that goes.

## Notes

> 2026-11-13 — found by an independent review of the fix for
> [ISSUE-0199](ISSUE-0199-persona-failures-travel-as-completed-work.md),
> whose texts said every cause now answers HTTP 200. Filed apart from
> that issue because its third step resolves it.

> 2026-11-14 — the fix for
> [ISSUE-0200](ISSUE-0200-late-chat-reply-answers-the-next-question.md)
> closes the second half of the Context, "a late one can answer the
> wrong request". A reply is now matched to the request it answers by
> the id of the request's message, which the error reply echoes like
> any other reply (`in_reply_to`). An error reply that is published
> after its request gave up is stored in the DM and returned to no
> later request; one that arrives in time still answers its own
> request with HTTP 200. Tested at the router with a late reply marked
> `reply_status: error`, and through the chat route with a late
> `event_timeout` error reply; the waiter's own table has no error
> reply, because the waiter does not look at that mark.
>
> Still open here: the first half, the error reply replayed to the
> model as the persona's own earlier words; and the ruling the
> proposed fix asks for, whether an error reply for a turn that ended
> after its request stopped waiting should be published at all. It is
> still published, and stays in the channel record. The summary in the
> front matter was first left naming both halves; the review of that
> fix changed it, because the index shows the summary and it read as
> if the late answer could still happen.
