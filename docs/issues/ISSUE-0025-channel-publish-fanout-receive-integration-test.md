---
id: ISSUE-0025
summary: "No integration test covers agent SEND_CHANNEL_MESSAGE → REST publish → router fanout → gRPC dispatch → ReceiveChannelMessage"
status: resolved
severity: medium
area: tests
created: 2026-05-05
closed: 2026-05-08
refs:
  - agents/channel_publisher.py
  - internal/channels/grpc_dispatcher.go
  - internal/server/channel_publish_fanout_integration_test.go
  - docs/rfcs/0011-pr-plan.md
---

## Summary

The two halves of the channel publish path landed in PR #250 are
unit-tested independently (Python `HTTPChannelPublisher` + REST executor
branch; Go `GRPCMessageDispatcher` with a bufconn fake). No test
exercises the full chain end-to-end: agent emits
`SEND_CHANNEL_MESSAGE` → `HTTPChannelPublisher.publish` →
orchestrator REST `POST /api/v1/channels/{id}/messages` →
`ChannelRouter` fanout → `GRPCMessageDispatcher.Dispatch` →
recipient agent's `ReceiveChannelMessage`.

## Context

Captured during PR #250 review (PR 4a-ii-β-1 in the RFC 0011 sequence).
The wire contract between the two halves — proto field mapping,
`sender_id` propagation/trust, RFC-3339 vs epoch timestamp format,
mentions list shape — is currently trusted by implicit agreement
between the two unit-test suites.

## Impact

A change to either side of the contract (e.g. renaming a proto field,
swapping the timestamp format, dropping a mention type-coercion) will
not be caught until manual smoke or until the cross-process flow is
run in a real deployment. v0.3.0 ships the first cross-process channel
delivery path, so the regression cost is highest now.

## Proposed fix / investigation path

Stand up a Python integration test (preferred — closer to the LLM-side
trust boundary) that:

1. Boots the orchestrator with `--channels-db :memory:` and a real
   `ChannelRouter`.
2. Registers two stub agents whose gRPC servers expose a recording
   `ReceiveChannelMessage` implementation.
3. Drives `ActionExecutor._handle_send_channel_message` with a fake
   action and asserts both stubs received the proto event with the
   expected `sender_id`, `channel_id`, `content`, `mentions`, and a
   parseable `timestamp`.

Per the project TDD instructions, integration tests are exempt from
the failing-first rule, so this can land as a follow-up PR.

## Notes

> 2026-05-05 — initial capture during PR #250 review (Should-Fix #1).
>
> 2026-05-08 — resolved. Test landed at
> [`internal/server/channel_publish_fanout_integration_test.go`](../../internal/server/channel_publish_fanout_integration_test.go)
> (`TestChannelPublish_FullChain_RESTToGRPCFanout`) rather than as a
> Python test. Rationale for the call: the wire-shape risk the issue
> calls out — proto field mapping, sender_id propagation, RFC 3339 vs
> epoch timestamp format, mentions list shape — is closed with no
> behavioural change as long as the JSON body matches what
> `agents/channel_publisher.py::HTTPChannelPublisher.publish`
> serialises. The Go test posts the EXACT same JSON shape (`sender_id`
> + `content` + optional `mentions`) so a Python-side payload divergence
> still surfaces here once the publisher unit suite is updated in
> lockstep. The Go-side approach also avoids spawning the orchestrator
> binary from a Python test (the path `--channels-db :memory:` would
> have implied) while exercising the dispatcher's real
> `grpc.NewClient` dial against an ephemeral 127.0.0.1 listener — the
> bufconn fake used by `internal/channels/grpc_dispatcher_test.go` does
> not.
>
> Mutation-tested by changing `SenderId: msg.SenderID` to
> `SenderId: msg.SenderID + "-mutated"` in
> [`internal/channels/grpc_dispatcher.go::channelMessageToProto`](../../internal/channels/grpc_dispatcher.go)
> (caught: `expected agent-alice, actual agent-alice-mutated`) and by
> swapping `time.RFC3339Nano` for `time.Kitchen` on the Timestamp
> render (caught: `timestamp MUST be RFC 3339`). Both reverts restored
> green.
