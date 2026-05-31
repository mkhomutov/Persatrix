---
# Allowed values are documented in README.md. Comments above fields
# (not inline) so that the front-matter parser does not pick them up.
id: ISSUE-0068
# summary: one-line description, surfaced as the Summary column in INDEX.md
summary: "Human chat peer is recorded as other_participant_type='agent' (not 'user') in the relationship tier — REST chat participant_type is dropped at the ChannelMessageEvent proto boundary"
# status: open | in_progress | resolved
status: resolved
# severity: low | medium | high | critical
severity: medium
# area: internal/ package or agent subsystem
area: memory
# created: YYYY-MM-DD when the finding was first captured (validated)
created: 2026-05-22
closed: 2026-05-31
# refs: documentary only — not surfaced in INDEX, useful for grep
refs:
  - docs/rfcs/0011-channels-bridges.md
  - docs/rfcs/0011-amendment-participant-type-wire-propagation.md
  - docs/rfcs/0020-interaction-lifecycle.md
  - proto/task.proto
  - docs/manual-tests/MT-CHAT-004.md
---

## Summary

When a human chats with a persona over the REST chat endpoint, the relationship-memory row
recorded at interaction close has **`other_participant_type = "agent"`**, not `"user"`. The
peer-type the caller supplies (`participant_type: "user"`) never reaches the agent, so the
agent-side default (`"agent"`) is used for every human chat peer.

## Context

REST chat is delivered to the agent as a **channel message**, not via the gRPC `SendChatMessage`
servicer:

1. [`internal/server/chat_handler.go:281-300`](../../internal/server/chat_handler.go) builds a
   `channels.ChannelMessage` and sets `Metadata["participant_type"] = req.ParticipantType`, then
   publishes it via `channelRouter.PublishAndAwait`.
2. The orchestrator dispatches it to the agent through `AgentService.ReceiveChannelMessage`. The
   wire message is **`ChannelMessageEvent`** ([`proto/task.proto`](../../proto/task.proto)), whose
   fields are `message_id, channel_id, channel_type, sender_id, content, timestamp, thread_id,
   mentions, respond_policy, thread_parent_sender_id, cascade_depth` — **there is no
   `participant_type` field and no metadata map.** So `Metadata["participant_type"]` is dropped at
   the proto boundary.
3. On the agent, [`agents/persona_runtime/episode_routing.py:366-371`](../../agents/persona_runtime/episode_routing.py)
   stashes the sender type from `event.metadata.get("sender_participant_type", "agent")` — which is
   absent for a channel-delivered chat — so it **defaults to `"agent"`**. That value flows to
   [`record_close.py:72`](../../agents/persona_runtime/record_close.py) as `other_participant_type`.

Note the gRPC `SendChatMessage` servicer ([`agents/server_servicers.py:189-242`](../../agents/server_servicers.py))
*does* default `participant_type` to `"user"` and set `sender_participant_type` — but that path is
**not** the one REST chat uses (REST chat goes through the channel publish above), so its correct
typing is inert for the REST surface.

There is also a **key-name mismatch**: the publish side sets `participant_type`, while the agent
read side expects `sender_participant_type`. Even if `ChannelMessageEvent` carried metadata, the
keys would need reconciling.

## Reproduction

On the v0.3.3 RC stack (lower `memory.interaction_idle_timeout_sec` to ~10s for a fast close):

```
# user_id only (no participant_type)
POST /api/v1/agents/ember-owl/chat {"message":"...","user_id":"u1"}
# explicit participant_type=user
POST /api/v1/agents/ember-owl/chat {"message":"...","user_id":"u2","participant_type":"user"}
# wait > idle timeout, send a nudge turn to materialise the close, then:
sqlite3 /app/data/memory.db
  SELECT other_participant_id, other_participant_type FROM relationships WHERE other_participant_id IN ('u1','u2');
```

Observed (both, regardless of `participant_type`): `other_participant_type = 'agent'`. Expected:
`'user'`. Interaction-close mechanics are otherwise correct (`interaction_count == 1` per closed
conversation, `interaction_type == 'conversation'`, `trust_score == 0.5`).

## Impact

- The relationship tier cannot distinguish human-user peers from agent peers for any
  channel-delivered conversation, including all REST chat. Trust/relationship modelling and any
  user-vs-agent logic keyed on `other_participant_type` operate on incorrect peer typing for human
  chat.
- `MT-CHAT-004` cannot reach its intended `other_participant_type == "user"` assertion; it carries
  ⚠️ Accepted-with-known-gap pending this fix.

Severity **medium**: chat and interaction recording function correctly; this is a peer-type
data-quality defect with downstream relationship-modelling implications, and the fix touches the
cross-language proto contract.

## Proposed fix / investigation path

The clean fix requires carrying peer type to the agent. Options:

1. **Add `participant_type` (or a `metadata` map) to `ChannelMessageEvent`** in
   [`proto/task.proto`](../../proto/task.proto) — a cross-language proto change, so it requires RFC
   review (per the proto-change policy). The orchestrator populates it from
   `ChannelMessage.Metadata["participant_type"]`; the agent maps it onto `sender_participant_type`
   before episode routing (reconciling the key-name mismatch).
2. **Derive peer type on the agent without a proto change**: a `sender_id` that is not a registered
   agent ID is a `user`. This avoids the proto change but couples the relationship tier to registry
   lookups and may misclassify external/bridge senders.

Whichever path is chosen, reconcile the `participant_type` vs `sender_participant_type` key naming
between `chat_handler.go` and `episode_routing.py`.

## Resolution

> 2026-05-31 — resolved via **option 1** (the proto change), under the
> [RFC 0011 Participant-Type Wire Propagation amendment](../rfcs/0011-amendment-participant-type-wire-propagation.md),
> mirroring the cascade-depth wire-propagation precedent:
>
> - `proto/task.proto`: `ChannelMessageEvent` gains `string sender_participant_type = 12` — the
>   peer type is now a first-class wire field rather than dropped at the metadata-less boundary.
> - `internal/server/chat_handler.go`: an omitted REST `participant_type` defaults to `"user"`
>   (REST chat is always a human→persona DM), matching the gRPC servicer's OQ-3 default. The
>   reproduction's "user_id only (no participant_type)" case now records `"user"`.
> - `internal/channels/grpc_dispatcher.go` (+ `participant_type.go`): `channelMessageToProto`
>   lifts `Metadata["participant_type"]` onto the typed field; empty for agent-to-agent fanout.
> - `agents/server_servicers.py`: `ReceiveChannelMessage` seeds
>   `event.metadata["sender_participant_type"]` from the typed field — **reconciling the
>   `participant_type` vs `sender_participant_type` key-name mismatch** at the single ingress seam.
>   The existing episode-routing → `record_close` path consumes it unchanged.
>
> `MT-CHAT-004`'s `other_participant_type == "user"` assertion is now reachable; its
> ⚠️ Accepted-with-known-gap note is cleared.

## Notes

> 2026-05-22 — found during the v0.3.3 release-prep dry-run of the rewritten `MT-CHAT-004`. The
> recipe's interaction-close corrections (count/type/trust) validated live; the
> `other_participant_type` assertion surfaced this gap. Reproduced with and without an explicit
> `participant_type` request field — both yield `'agent'`.
