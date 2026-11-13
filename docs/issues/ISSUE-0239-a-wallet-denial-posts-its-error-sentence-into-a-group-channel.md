---
id: ISSUE-0239
summary: "When the wallet denies a persona's model call on a group channel, the agent posts the denial sentence into the channel as a message tagged `reply_status: error`, mentioning the sender. Only the chat handler reads that tag, so to the channel it is an ordinary message: stored, and able to end the speaker's turn in a floor round. A persona turn that fails for another reason is answered that way only on a DM."
status: resolved
severity: medium
area: agents/chat_reply
created: 2026-11-12
closed: 2026-11-13
closed_pr: 1109
refs:
  - agents/chat_reply.py
  - internal/server/chat_handler.go
  - internal/channels/floor_control.go
  - internal/channels/router_publish_async.go
  - docs/issues/ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md
  - docs/issues/ISSUE-0066-chat-rest-resource-exhausted-no-channel-reply.md
  - docs/issues/ISSUE-0199-persona-failures-travel-as-completed-work.md
  - agents/persona_runtime/llm_call_errors.py
  - tests/unit/python/test_wallet_denial_channel_reply.py
  - docs/observability.md
  - docs/issues/ISSUE-0161-silent-reply-on-a-handed-over-autonomous-turn-stalls.md
  - docs/experiments/EXP-001-preregistration.md
---

# ISSUE-0239: A wallet denial posts its error sentence into a group channel

## Summary

`dispatch_channel_event_with_chat_error_recovery` (`agents/chat_reply.py`)
answers a wallet denial by publishing an error reply on the channel the
message came from. It does so on every channel, not only a DM, and holds
back only for a synthesis turn. The fixes it belongs to
([ISSUE-0065](ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md),
[ISSUE-0066](ISSUE-0066-chat-rest-resource-exhausted-no-channel-reply.md))
were about the chat request on a DM.

## Context

Reproduced on `main` at `0f3eb3ac`: a persona whose model call raises
`BudgetExceededError`, sent one message on the channel `general`,
published `per_agent budget exceeded for ember-owl (spent=… limit=…)`
there with `metadata.reply_status = "error"` and the sender in
`mentions`. The same event on a DM published the same message.

In the orchestrator only the REST chat handler reads `reply_status`
(`internal/server/chat_handler.go`). Nothing under `internal/channels/`
does, so on a group channel the message is like any other: it is stored,
it counts against the sender's reply budget where one is set, and in a
floor-control round it is the speaker's reply (`internal/channels/floor_control.go`,
`router_publish_async.go`).

The fix for
[ISSUE-0199](ISSUE-0199-persona-failures-travel-as-completed-work.md)
answers a persona's other failures (a provider error, a cut-off reply, a
time-out) with this error reply on a DM only, and posts nothing on a
group channel. So the two kinds of failure now differ on a group channel.

## Impact

A budget figure and an internal sentence enter the channel record and
every member's history. Whether that is wanted is a product question:
the room does learn why a member went quiet, and in a floor round the
message ends the speaker's turn at once, where silence costs the round
its turn timeout (45 seconds by default).

Rated medium by likeness to ISSUE-0199's second effect; the severity is
the maintainer's to set.

Four more things bear on the ruling:

- The error reply is published at cascade depth 0 and mentions whoever
  sent the message. To the router that is a new message addressed to a
  member, not the end of a chain: the depth cap that stops a runaway
  exchange starts again from it (`agents/chat_reply.py`,
  `internal/channels/router_publish_async.go`).
- Silence has a known cost of its own on an autonomous channel:
  [ISSUE-0161](ISSUE-0161-silent-reply-on-a-handed-over-autonomous-turn-stalls.md)
  records a discussion that stopped after a silent turn.
- The wallet's error reply cannot be taken for a chair's synthesis: it
  is withheld on a synthesis turn and published without the synthesis
  claim. A sentence posted as an ordinary reply has no such guard. That
  was the case for the sentence a persona posted when it ran out of
  model calls, for as long as it was posted.
- EXP-001's pre-registration relies on a persona turn that ends in a
  provider error publishing nothing to a group channel
  (`docs/experiments/EXP-001-preregistration.md`, "Attempts and
  failures"). A ruling that posts a notice there changes what its
  meetings look like.

## Proposed fix / investigation path

Rule on one behaviour for both kinds of failure on a group channel:
either nothing is posted and the cause is logged, or a short neutral
notice is posted that carries no figures. Then make the wallet-denial
path and the failed-turn path agree.

## Notes

> 2026-11-12 — found while fixing
> [ISSUE-0199](ISSUE-0199-persona-failures-travel-as-completed-work.md),
> whose brief pointed at this path as the model for a group channel.

> 2026-11-13 — ruled by the maintainer: a group channel is posted
> nothing, and the cause is logged. ISSUE-0199's third step does that
> for a persona's own failed turn. This issue stays open for the
> wallet-denial path, which still posts its sentence on a group channel
> and is what remains to be brought in line. A DM keeps its error
> reply: the ruling is about group channels.

> 2026-11-13 — fixed as ruled. A wallet denial is answered with the error
> reply on a DM only; anywhere else nothing is posted and the denial is
> logged at WARNING, as it was before. That holds for each way the
> wallet refuses a call: a budget is spent, a discussion's cost ceiling
> is reached, the wallet cannot be reached, or the agent is at its lease
> or rate limit. One function in `agents/chat_reply.py` now decides it
> for a wallet denial and for a persona's own failed turn, with the same
> two facts: the channel, and whether the turn is the synthesis turn
> that closes a discussion, which is never answered. Reproduced first on
> the code as ISSUE-0199's third step left it: a spent budget, an
> unreachable wallet and a lease refusal each posted their sentence to
> `group:planning`, to an id starting `thread:` and to a channel id with
> no prefix, tagged `reply_status: error` and mentioning the sender. The
> cost ceiling was added to the tests after review; it takes the same
> path.
>
> The rule is "a DM, or nothing". Nothing in the orchestrator creates a
> channel whose id starts `thread:` today: a reply in a thread carries
> its parent channel's id, so in a thread under a DM it is still
> answered. The chat request, the one caller that waits for the reply,
> always travels on a DM (`internal/server/chat_handler.go`). The
> example line in Context above is a test's; the wallet writes
> `per_agent budget exceeded: spent=…, limit=…, estimated=…`
> (`internal/cost/cost.go`).
>
> The cost, accepted by the maintainer as a known gap, read in
> `internal/channels/` and not changed or run. In a floor-control round
> a denied speaker now holds the floor until its turn times out
> (`floor_turn_timeout_seconds`, 45 by default), where its posted line
> ended the turn at once. One agent over its own budget pays that on
> each of its turns. That is the smaller part. A wallet outage, a spent
> shared budget or a discussion at its cost ceiling denies every
> speaker, so a round of K speakers takes K × 45 seconds for each
> message with nothing posted, and the rest follows from a round in
> which nobody replies; it is set out in the note on
> [ISSUE-0161](ISSUE-0161-silent-reply-on-a-handed-over-autonomous-turn-stalls.md).
