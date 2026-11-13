---
id: ISSUE-0200
summary: "A chat reply is matched to its request by channel and sender only. After one chat request times out, the persona's late answer is returned to the next chat request on that DM, and every later answer on that DM is then one question behind for as long as each new question is sent before the previous answer lands."
status: resolved
severity: medium
area: internal/channels
created: 2026-11-10
closed: 2026-11-14
refs:
  - internal/channels/waiter.go
  - internal/channels/publish_and_await.go
  - internal/channels/floor_control.go
  - agents/dispatch_context.py
  - agents/chat_reply.py
  - schemas/channel.schema.json
  - internal/channels/waiter_reply_match_test.go
  - internal/channels/floor_reply_match_test.go
  - internal/server/chat_reply_match_test.go
  - tests/unit/python/test_reply_names_the_message_it_answers.py
  - tests/unit/python/test_cross_language_reply_echo_drift.py
  - docs/issues/ISSUE-0244-a-dms-error-reply-is-an-ordinary-message-in-the-channel-record.md
  - internal/channels/interaction_resolver.go
  - internal/server/chat_handler.go
  - docs/issues/ISSUE-0033-chat-as-dm-single-shot-waiter-multi-message-reply.md
  - docs/issues/ISSUE-0073-cpu-local-model-chat-exceeds-default-timeout.md
---

# ISSUE-0200: After a chat times out, the next chat gets the late answer to the previous question

## Summary

The chat endpoint publishes the person's message into the DM channel and
waits for the persona's reply. The wait is registered under the pair
(channel, sender) in `replyWaiter` (`internal/channels/waiter.go`).
Nothing ties a reply to the message it answers, so the first reply to
arrive satisfies whichever request is waiting at that moment.

## Context

A probe with a slow agent double:

1. Chat 1 asks "Q1" and times out.
2. Chat 2 asks "Q2: what is 2+2?".
3. The agent's late "A1: Paris" arrives. Chat 2 returns it, with no error.
4. The real answer to Q2 then finds no waiter.

A comment in `publish_and_await.go` says the waiter is removed on a
timeout "so a late-arriving reply does not leak into a future chat". It
does leak, into the next waiter registered under the same pair.

The default chat timeout is 30 seconds, and
[ISSUE-0073](ISSUE-0073-cpu-local-model-chat-exceeds-default-timeout.md)
already records local models that take longer. The floor-control round
uses the same waiter, so a speaker's late reply from one round can be
taken as its turn in the next.
[ISSUE-0033](ISSUE-0033-chat-as-dm-single-shot-waiter-multi-message-reply.md)
covers a different limit of the same waiter, replies that come as several
messages.

## Impact

A person gets a confident answer to a question they did not ask. The
conversation stays one answer behind for as long as each new question is
sent before the previous answer lands, and recovers when a late answer
lands while no chat is waiting. The history in the channel store is
correct; only the chat responses are shifted.

## Proposed fix / investigation path

Send the inbound message's id with the event, have the agent echo it on
its reply, and accept only a reply that echoes this request's id. A reply
carries no reference to the message it answers today, so this changes the
contract on both sides. The interaction id cannot do the job: one
interaction covers every message on a DM until the DM goes idle, and a
late answer is also stored after the next question. A test that times
out one request and then sends a second would pin it.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; reproduced with a throwaway Go test mapped in with
> `go test -overlay`.

> 2026-11-14 — fixed, following the proposed fix. Checked against the
> code at `dd006cba` first, with the probe as a test at the waiter, at
> the router and through the chat route: chat 2 returned `A1: Paris`
> where `A2: 4` was expected. The floor round was checked the same way:
> a speaker's late reply to round 1 ended its turn in round 2 at once,
> and the next speaker was dispatched before this speaker's answer to
> round 2 was in the channel. The Summary, Context and Impact hold as
> written.
>
> One point of the proposed fix needed less than it says. "Send the
> inbound message's id with the event": the id already travels, as
> `ChannelMessageEvent.message_id`, and the agent already keeps it on
> the event. So there is no proto change. What was missing is the way
> back:
>
> - **The agent** copies that id onto every message it publishes in
>   reply on the channel the message came on, as `in_reply_to` in the
>   publish metadata (`DispatchContext.same_channel_claim`, the one
>   rule every publish path already takes its metadata from). That
>   covers an answer, an end-of-interaction vote and the error reply a
>   DM gets when a turn fails or the wallet refuses the model call. A
>   message to another channel, and one the agent sends on its own from
>   a timer, answers nothing and carries no such key.
> - **The orchestrator** registers each wait under the id of the
>   message it is about to dispatch, and `replyWaiter.Notify` accepts
>   only a message whose `in_reply_to` is that id. A message that
>   echoes another id, or none, satisfies nothing and leaves the wait
>   in place. The floor round registers under the round's message id.
>
> The match is strict, as the proposed fix says ("accept only a reply
> that echoes this request's id"): a message with no echo is never
> matched. The other choice, to let a message with no echo answer
> whoever is waiting, would keep an agent from before this change
> working, and would keep the fault for it; it would also let a
> message the persona sends from a timer answer a request. The price
> of the strict rule is that the orchestrator and the agents must be
> upgraded together: with a new orchestrator and an old agent every
> chat request times out and every floor turn waits its full timeout.
> The CHANGELOG's Upgrade Notes say so.
>
> A cross-language test pins the four places the id passes through, in
> both languages, as text: a rename on one side alone would leave every
> reply unmatched with every other suite green.
>
> This also closes one half of
> [ISSUE-0244](ISSUE-0244-a-dms-error-reply-is-an-ordinary-message-in-the-channel-record.md):
> an error reply that arrives after its request gave up can no longer
> be returned to the next request. Its other half stays open there.
>
> Verified with a real orchestrator and a real agent process (the
> offline mock model): three chat requests in a row each returned HTTP
> 200, and each stored reply carried the id of its own question. The
> mixed case was run the same way. With the new orchestrator and an
> agent from before the change, each request ended in 504 and the
> reply, which names no message, was saved in the DM. With an
> orchestrator from before the change and the new agent, each request
> returned 200.
> [ISSUE-0033](ISSUE-0033-chat-as-dm-single-shot-waiter-multi-message-reply.md)
> is not changed by this: a wait still ends on the first message that
> answers it.
