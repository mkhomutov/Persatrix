---
id: ISSUE-0155
summary: "REST chat is leased as CAUSE_CHANNEL_MESSAGE, never CAUSE_CHAT: the chat handler stamps `chat_session_id` on the DM message it publishes, but `ChannelMessageEvent` has no field or metadata map for it, so the key `cause_for_event` looks for never reaches the agent — only the unused gRPC `SendChatMessage` servicer builds that shape. No budget, limit, metric or dashboard reads the cause, so nothing is overspent or charged wrongly; but the wallet's logs file chat spend under channel traffic, and test docstrings say chat-as-DM events get CAUSE_CHAT"
status: open
severity: low
area: cost
created: 2026-09-11
refs:
  - docs/rfcs/0023-llm-call-leasing.md
  - docs/rfcs/0023-pr-plan.md
  - docs/rfcs/0011-amendment-chat-as-dm.md
  - docs/rfcs/0011-amendment-participant-type-wire-propagation.md
  - docs/rfcs/0032-channel-interaction-layer.md
  - docs/rfcs/0048-amendment-chat-panel-retirement.md
  - docs/issues/ISSUE-0035-chat-executor-dead-but-wired-cleanup.md
  - docs/issues/ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md
  - docs/issues/ISSUE-0068-chat-peer-recorded-as-agent-participant-type.md
  - internal/server/chat_handler.go
  - internal/server/chat_handler_test.go
  - internal/channels/grpc_dispatcher_proto.go
  - internal/wallet/wallet.go
  - internal/wallet/interaction_budget.go
  - proto/task.proto
  - proto/wallet.proto
  - agents/server_servicers.py
  - agents/channel_wire_metadata.py
  - agents/persona_runtime/wallet_cause.py
  - agents/tests/test_action_loop_channel_lease.py
  - agents/tests/test_action_loop_chat_lease.py
  - agents/tests/test_action_loop_tick_lease.py
  - tests/integration/test_channel_message_budget_denied.py
---

## Summary

Before a persona calls its model, it asks the orchestrator's wallet for a
lease, and the lease names the kind of traffic the call serves: its `cause`
([RFC 0023](../rfcs/0023-llm-call-leasing.md)). RFC 0023 meant a chat turn to
be leased as `CAUSE_CHAT`. But a chat sent through
`POST /api/v1/agents/{id}/chat`, which both the `persatrix chat` CLI and the
web console's DMs use, is leased as `CAUSE_CHANNEL_MESSAGE`, the same as a
reply in a group channel. The marker the agent checks for, `chat_session_id`,
is stamped by the chat handler and dropped before the message reaches the
agent.

On the wallet side, the cause is used for one thing: a field on four log
lines. No budget, limit, metric or dashboard reads it, so no money is spent
or refused differently. What is lost is the per-origin attribution RFC 0023
asked for, and several test docstrings and one changelog line say the
opposite of what the code does.

## Context

**How the agent picks the cause.** `cause_for_event` in
`agents/persona_runtime/wallet_cause.py` returns `CAUSE_CHAT` for a
channel-message event whose metadata holds a `chat_session_id` key, and
`CAUSE_CHANNEL_MESSAGE` for any other channel-message event. That key is its
only sign of chat.

**What REST chat sends.** Since [chat-as-DM](../ai-glossary.md#chat-as-dm)
([#251](https://github.com/mkhomutov/Persatrix/pull/251), v0.3.0), `handleChat`
in `internal/server/chat_handler.go` posts each chat turn into the caller's DM
channel with the agent, through `ChannelRouter.PublishAndAwait`, and stamps
`chat_session_id` (and `participant_type`) on the message's metadata;
`internal/server/chat_handler_test.go` pins the stamp. The message is stored
with its metadata, and the channel history endpoint
(`GET /api/v1/channels/{id}/messages`) returns it.

**Where the key stops.** The orchestrator hands the message to the agent as a
gRPC `ChannelMessageEvent` (`proto/task.proto`). It has 31 typed fields, no
metadata map, and no field for a chat session. `channelMessageToProto` in
`internal/channels/grpc_dispatcher_proto.go` reads four things from the
message's metadata into typed fields: cascade depth, participant type, the
interaction id and the previous-interaction pair. Nothing else in the
metadata crosses. The proto's own comments name the pattern: `cascade_depth`,
`sender_participant_type` and `interaction_id` each became a typed field
because the event "has no metadata map".

**What the agent builds.** `ReceiveChannelMessage` in
`agents/server_servicers.py` fills the event's metadata from three sources:
`cascade_depth`; `seed_wire_metadata` in `agents/channel_wire_metadata.py`
(sender participant type, interaction id, the previous-interaction pair,
channel classification); and the session, principal and epoch read from gRPC
headers, stored as `persatrix_session`, `persatrix_principal` and
`persatrix_epoch`. None of them writes `chat_session_id`, so every REST chat
turn takes the `CAUSE_CHANNEL_MESSAGE` arm. Catch-up replay
(`seed_replay_metadata`) lifts only the interaction keys, so it does not
restore the key either.

**Who builds the `CAUSE_CHAT` shape.** Only the gRPC `SendChatMessage`
servicer in `agents/server_servicers.py`. The orchestrator no longer calls
it: `handleChat` never uses the chat executor that `cmd/orchestrator/main.go`
still builds, and
[ISSUE-0035](ISSUE-0035-chat-executor-dead-but-wired-cleanup.md) tracks
deleting the servicer, the executor and the proto entry. RFC 0023 PR 4
([#387](https://github.com/mkhomutov/Persatrix/pull/387), v0.3.2) wired the
`CAUSE_CHAT` lease onto that servicer on 2026-05-20, fifteen days after #251
had stopped sending chat through it. No route the project ships reaches the
`CAUSE_CHAT` arm; only a direct call to an agent's `SendChatMessage` RPC
does.

**Seen before, never tracked.**
[ISSUE-0065](ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md)
(resolved) hit this in v0.3.2 release testing. Its write-up records that the
orchestrator's hand-off drops the key, so `cause_for_event` "derives
`CAUSE_CHANNEL_MESSAGE` rather than `CAUSE_CHAT`", and its notes log a live
REST chat lease as `CAUSE_CHANNEL_MESSAGE`. Its Path B proposed a
`chat_session_id` field. Path A fixed that issue's real bug, the missing
error reply; Path B was never taken, and no issue has tracked the cause
since. The design never carried the token either: the mapping table in the
[chat-as-DM amendment](../rfcs/0011-amendment-chat-as-dm.md#mapping) keeps
`participant_type` for the DM path and does not mention the chat session.

## Impact

**Spend attribution.** RFC 0023 tags each lease with a cause "so spend can
be attributed and policy can differ per origin" (Goal 7). In the code,
attribution is the `cause` field on four wallet log lines in
`internal/wallet/wallet.go` and `internal/wallet/interaction_budget.go`:
lease granted (`Debug`), and denied over budget, reaped on TTL expiry, and
denied at the interaction ceiling (`Warn`). `docs/observability.md` lists the
first three. REST chat spend shows up there as `CAUSE_CHANNEL_MESSAGE`, and a
filter for `CAUSE_CHAT` finds nothing. None of those lines names the channel,
so the wallet's logs cannot tell a chat turn from a group-channel reply.

**Metrics and dashboards.** None are split by cause. No Go or Python metric
or span carries it, and the repo ships no dashboards, only a dev Prometheus
scrape config (`config/observability/prometheus.yaml`). So no chart is wrong,
but two test docstrings promise "per-cause dashboards" that do not exist
(`agents/tests/test_action_loop_tick_lease.py`,
`agents/tests/test_action_loop_channel_lease.py`).

**Budgets and limits.** There is no per-cause budget or limit, so chat
neither escapes one nor is charged against the wrong one. The wallet's budget
check (`BudgetEnforcer.CheckBudget`) takes the workflow, agent, model and
token estimate, never the cause, and the interaction ceiling keys on the
interaction id. Per-cause caps are RFC 0023's
[open question 4](../rfcs/0023-llm-call-leasing.md#open-questions), deferred
to RFC 0006 territory (v0.6+, per `docs/v0.3.x-sequencing.md`). The agent
side does not branch on it either: `LLMClient.create_message` skips the
wallet only for `CAUSE_UNSPECIFIED`, and a budget denial is handled by event
type, so REST chat's error reply (ISSUE-0065) does not depend on the cause.
The v0.3.2 changelog entry does say leases are issued "against per-agent /
per-cause / global budgets"; there is no per-cause budget. If per-cause caps
are ever built on this enum, the gap becomes a charging bug: a chat cap would
never see REST chat, and a channel-message cap would count it.

**Claims that say the opposite.** The tests pass and are right about
`cause_for_event` itself; each builds its "chat" event the way
`SendChatMessage` does. The prose is what misleads:

- `agents/tests/test_action_loop_channel_lease.py`: the module docstring says
  `chat_session_id` "is the chat-as-DM shape from PR 4", and
  `test_chat_event_still_maps_to_chat` fails with "chat-as-DM events must
  stay on CAUSE_CHAT".
- `tests/integration/test_channel_message_budget_denied.py`: the
  `_channel_event` docstring calls the key "the chat-as-DM discriminator from
  PR 4", which "would route to `CAUSE_CHAT` instead".
- `agents/tests/test_action_loop_chat_lease.py`: the `_chat_event` docstring
  says the key "is set unconditionally on the chat path" and asks readers to
  "keep the chat handler's metadata in sync", but the chat handler's metadata
  never reaches this event; `test_channel_event_tagged_channel_message_post_pr6`
  calls the key "PR 4's chat discriminator".
- The "per-cause dashboards" docstrings and the changelog's per-cause
  budgets, above.

No test follows a REST chat turn to the cause on its lease.

**Severity: low.** No money moves differently and nothing is refused
wrongly. The cost is lost attribution, a metadata key the chat handler stamps
that never reaches the agent, and prose that contradicts the code, until
someone builds a per-cause budget or metric on the enum.

## Proposed fix / investigation path

Three options; the owner picks at slotting. One plan bears on the choice:
[RFC 0032](../rfcs/0032-channel-interaction-layer.md), a draft aimed at
v0.4.0 or later. Its first goal says `chat_session_id` "either becomes an
alias for the DM channel's `interaction_id` or is retired entirely", and its
[open question 1](../rfcs/0032-channel-interaction-layer.md#open-questions)
was resolved on 2026-06-03: deprecate the chat-specific REST route and the
`SendChatMessage` RPC, keep a generic wait-for-reply primitive, and rebuild
`persatrix chat` on that primitive and channel publish.

1. **Carry the token.** Add a typed `chat_session_id` field to
   `ChannelMessageEvent`, fill it in `channelMessageToProto` from the publish
   metadata, and lift it onto `event.metadata["chat_session_id"]` in
   `seed_wire_metadata` when it is set. `cause_for_event` then works
   unchanged, and so does the salience bid, which takes its cause from
   `cause_for_event`. This is the
   [ISSUE-0068](ISSUE-0068-chat-peer-recorded-as-agent-participant-type.md)
   participant-type fix again, and the first half of ISSUE-0065's Path B; by
   precedent a new field on this message comes with an RFC amendment, as
   cascade depth and participant type each got one on RFC 0011. Two details:
   the REST handler puts no length limit on the token (the gRPC servicer caps
   it at 128 characters), so the lift needs a byte bound like the interaction
   id's; and the channel publish route passes caller metadata through, so it
   should drop a caller-supplied token, the way the router already replaces a
   caller's interaction id. Then only `/chat` turns count as chat. The cost is
   a new wire field for a token RFC 0032 plans to alias or retire, fed by a
   route it has resolved to deprecate.
2. **Infer chat from the room.** Treat a channel-message event as chat when
   its channel type is `dm` and its sender's participant type is `user`. Both
   already reach the agent, so the wire does not change. It does change what
   "chat" means: every human message in a DM counts, whichever route posted
   it (the channel publish route stamps `user` for any sender the agent
   registry does not know), and a bridge that labels its sender `agent` does
   not. It also outlives the chat endpoint, since it never looks at the
   route. `chat_session_id` stays unused on the agent side, so the handler's
   stamp should say it is kept for history only, or go.
3. **Retire `CAUSE_CHAT`.** A chat is a DM channel, the web console already
   dropped its separate chat panel
   ([RFC 0048 amendment](../rfcs/0048-amendment-chat-panel-retirement.md)),
   and RFC 0032 has resolved to deprecate the chat endpoint too. If nobody
   needs chat spend split from channel spend, drop the `CAUSE_CHAT` arm when
   ISSUE-0035 deletes `SendChatMessage`, its only producer, keep the enum
   value for wire compatibility, and say in RFC 0023 that chat is leased as
   channel traffic.

**Recommendation:** option 3 if chat spend does not need its own line: it
follows RFC 0032's resolved direction and removes code instead of adding a
wire field. Option 2 if it does, because it keeps working once the chat
endpoint is gone. Option 1 fits only if RFC 0032's resolution is reversed.

Whichever is chosen: fix the prose listed under Impact, and add one test,
written first, that drives a REST chat turn to the cause on its lease.
ISSUE-0035 deletes the only code that builds the `CAUSE_CHAT` shape, so the
two should land together or agree on what happens to `CAUSE_CHAT`.

**Slot: the owner's call.** v0.3.16's scope was ratified by the
[sequencing amendment of 2026-08-19](../v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train)
and does not include this, so taking it into v0.3.16 needs a new dated
amendment. The natural home is v0.4.0, beside ISSUE-0035 and RFC 0032, which
both aim at v0.4.0 or later. Under the
[version-train gate](../methodology/process-glossary.md#version-train-gate), a
v0.4.0 PR can be written and reviewed now but does not merge until v0.3.16 is
tagged.

## Notes

> 2026-09-11 — filed from a code reading at `5088697a`; every file, symbol
> and test named above was checked there. Docs only: no code changes. Found
> while correcting chat comments in
> [#921](https://github.com/mkhomutov/Persatrix/pull/921), which rewrites the
> `cause_for_event` docstring to say REST chat takes the channel-message arm
> and points it at ISSUE-0065; that pointer can move here.
> [ISSUE-0065](ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md) and
> [ISSUE-0035](ISSUE-0035-chat-executor-dead-but-wired-cleanup.md) now link
> here.
