---
id: ISSUE-0048
summary: "Persona agent (ember-owl) processes inbound channel_message and calls the LLM, but never publishes the reply back to the orchestrator — chat-as-DM times out at 504"
status: resolved
severity: high
area: agents/persona
created: 2026-05-08
closed: 2026-05-08
refs:
  - agents/server_servicers.py
  - agents/dispatch.py
  - agents/action_executor.py
  - agents/response_gate.py
  - agents/persona_runtime/action_loop.py
  - agents/persona_runtime/channel_reply.py
  - docs/rfcs/0011-amendment-chat-as-dm.md
  - docs/issues/ISSUE-0033-chat-as-dm-single-shot-waiter-multi-message-reply.md
  - docs/issues/ISSUE-0046-pyproject-missing-temporal-package.md
  - docs/issues/ISSUE-0047-compose-orchestrator-channels-db-not-writable.md
---

## Summary

After ISSUE-0046 (missing `persatrix_agents.temporal` in the wheel) and
ISSUE-0047 (orchestrator `--channels-db` pointing at a read-only path)
are fixed and the compose stack is fully healthy, every
`POST /api/v1/agents/ember-owl/chat` request still returns:

```json
{"error":"agent did not respond in time","code":"DEADLINE_EXCEEDED"}   HTTP 504
```

Tracing the flow:

1. Orchestrator `handleChat` validates the request and creates the DM
   channel `dm:<user>:ember-owl` ✅ (verified via `GET /api/v1/channels`).
2. Inbound user message is persisted in the channel store ✅ (verified
   via `GET /api/v1/channels/dm:<user>:ember-owl/messages`).
3. `ChannelRouter.PublishAndAwait` fans out via `GRPCMessageDispatcher`
   to the agent. (No dispatcher error logged.)
4. `agent-ember-owl` makes **two consecutive HTTP 200 calls to
   `api.anthropic.com/v1/messages`** within 6–10 s of the chat
   landing — the LLM round-trip clearly fired (sharing one trace_id).
5. **No `SEND_CHANNEL_MESSAGE` action is executed**, **no
   `POST /api/v1/channels/.../messages` arrives at the orchestrator**,
   and the orchestrator's reply waiter eventually trips its 30 s
   (or higher, with `timeout_seconds` override) deadline and 504s.

The agent received the message and produced a reply — but the reply
never reaches the channel store, so `PublishAndAwait` never sees it.

## Context

Reproduced 2026-05-08 against tip-of-`main` + the ISSUE-0046 + ISSUE-0047
patches, on the standard `docker compose up -d` stack with all four
agent services healthy and `agent-ember-owl` registered.

ISSUE-0033 documents a related but distinct failure mode (the
single-shot reply waiter can drop multi-message tool-call/tool-result/
final-answer reply sequences). This issue's symptom is upstream of
that: the agent emits *zero* `SEND_CHANNEL_MESSAGE` actions, so the
waiter has nothing to drop.

Likely suspect surfaces (not yet root-caused):

- **Action executor wiring** for `SEND_CHANNEL_MESSAGE` against the
  HTTP channel publisher — possibly a v0.3.0 regression similar in
  shape to ISSUE-0026 (HTTPChannelPublisher unconditionally wired)
  or its mirror image.
- **Response gate** under DM with `respond_policy="always"` — should
  fire-through, but worth verifying the fanout `env.Recipient.RespondPolicy`
  is populated; the gate fails closed if absent.
- **Action loop output parsing** — the agent may be deciding
  `DO_NOTHING` instead of emitting `SEND_CHANNEL_MESSAGE` because the
  prompt wiring for "this is a DM, you must reply" is missing in the
  channel ingest path.
- **Persona-runtime action validation** — `SEND_CHANNEL_MESSAGE`
  may be silently dropped at the validation layer with no log line
  surfacing the rejection.

## Impact

- **MT-CHAT-001 cannot pass against the compose stack.** Steps 4
  (404 unknown agent), 5 (400 empty), 7 (400 invalid agent ID) work,
  but every step that exercises a real round-trip (1, 2, 3) 504s.
- **MT-CHAT-002 / MT-CHAT-003 / MT-CHAT-004 are also blocked** —
  they share the same chat round-trip primitive.
- **`persatrix chat` REPL non-functional in compose.** The CLI
  surfaces the 504 as an opaque "agent did not respond" message.

## Reproduction (against ISSUE-0046 + ISSUE-0047 patched stack)

```bash
docker compose up -d
# wait for ember-owl to report healthy
curl -s http://localhost:8080/api/v1/agents | jq '.[] | select(.id=="ember-owl") | .status'
# "healthy"

curl -s -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"say hi","user_id":"alice","timeout_seconds":60}'
# {"error":"agent did not respond in time","code":"DEADLINE_EXCEEDED"}

# Inbound message is persisted, but no agent reply ever appears:
curl -s "http://localhost:8080/api/v1/channels/dm:alice:ember-owl/messages" | jq '.messages | length'
# 1   (only the user's message)

# Yet the agent did call the LLM during the chat window:
docker compose logs agent-ember-owl --since 2m | grep "api.anthropic.com"
# Two HTTP 200 responses sharing one trace_id, ~6–10s after the chat POST.
```

## Investigation path

1. Add a DEBUG-level log inside `agents/persona_runtime/action_loop.py`
   at the action-emission boundary — log every action the LLM round
   produces and every action the validator drops. Re-run the chat
   reproduction; the missing `SEND_CHANNEL_MESSAGE` should surface
   as either "produced but dropped" or "never produced".
2. If the action is "produced but dropped": check
   `agents/action_executor.py::_handle_send_channel_message` and the
   wiring from `AgentServer.start()` that sets the channel publisher.
3. If the action is "never produced": inspect the LLM prompt assembly
   for channel-message handling (`agents/persona_runtime/channel_ingest.py`)
   — DM context may not be reaching the model.
4. Cross-reference ISSUE-0026 (HTTPChannelPublisher unconditional wiring):
   if the publisher is wired but the orchestrator publish path is
   misconfigured (e.g., wrong base URL inside the container), the
   publish would 404/503 silently.

## Notes

> 2026-05-08 — captured during the same MT-CHAT-001 rehearsal that
> surfaced ISSUE-0046 and ISSUE-0047. Those two are infrastructure
> bugs (wheel packaging + compose mounts) and ship together as
> docker-deployment-blockers. **This issue is the next layer down**:
> the docker stack is now healthy and reachable, but the chat
> protocol does not complete a round-trip. Filed separately so the
> infra fixes are not held up by the deeper persona-runtime
> investigation. Will be assigned to the next persona-runtime
> review pass.

## Resolution (2026-05-08)

Root cause was the third bullet on the investigation list: the LLM
emits `DO_NOTHING` / a conversational reply rather than a structured
`SEND_CHANNEL_MESSAGE`. No prompt snippet documents the JSON action
schema for personas, so on a normal CHANNEL_MESSAGE turn
`_parse_actions` falls back to a single `COMPLETE_TASK` carrying the
reply text. `ActionExecutor` records `status=completed` for
`COMPLETE_TASK` and never reaches the channel-publish branch — the
orchestrator-side `replyWaiter` never sees a publish on the inbound
channel and chat-as-DM 504s on its `chatDefaultTimeout`.

Fix: a new pure helper
[`agents.persona_runtime.channel_reply.synthesize_channel_reply`](../../agents/persona_runtime/channel_reply.py)
promotes a conversational `COMPLETE_TASK` reply into an explicit
`SEND_CHANNEL_MESSAGE` bound to the inbound `channel_id`. Hooked into
`_ActionLoopMixin._on_event_inner` immediately after `_parse_actions`,
before episode persistence. No-ops for non-channel events, the legacy
`SendChatMessage` path (no `channel_id`), action lists that already
publish to the inbound channel, and empty/whitespace replies. Pinned
by `tests/unit/python/test_channel_reply_synthesis.py` (11 cases —
pure helper + action-loop integration + replay/gate negative cases).
