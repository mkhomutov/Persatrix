---
id: ISSUE-0025
summary: "No integration test covers agent SEND_CHANNEL_MESSAGE → REST publish → router fanout → gRPC dispatch → ReceiveChannelMessage"
status: open
severity: medium
area: tests
created: 2026-05-05
refs:
  - agents/channel_publisher.py
  - internal/channels/grpc_dispatcher.go
  - tests/integration/
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
