---
id: ISSUE-0026
summary: "HTTPChannelPublisher wired unconditionally on agents — every channel-routed action hits 503 when orchestrator has channels disabled"
status: open
severity: medium
area: agents
created: 2026-05-05
refs:
  - agents/server.py
  - agents/channel_publisher.py
  - cmd/orchestrator/channels.go
---

## Summary

`AgentServer.start()` constructs `HTTPChannelPublisher` and attaches it
to the dispatcher unconditionally. If the orchestrator is started
without `channels.yaml` (or with the channels store disabled), every
agent-emitted `SEND_CHANNEL_MESSAGE` carrying a `channel_id` POSTs to
the orchestrator, gets a 503, returns `status: failed`, and emits a
WARN log — every time, on every action.

## Context

Observed during PR #250 review. Same agent build is intended to run
against orchestrators with and without channels enabled (per the
deferred-by-default phase model in `cmd/orchestrator/channels.go`'s
`selectChannelDispatcher`).

## Impact

- Operator log noise scales with action volume, drowning legitimate
  warnings.
- Wasted RTT on every publish.
- Misleading `status: failed` reporting back to the LLM, which may
  retry an action that has no chance of succeeding in the current
  deployment.

## Proposed fix / investigation path

Two viable approaches:

1. **Capability probe at agent startup.** `AgentServer.start()` issues
   a `GET /api/v1/health` (or a dedicated `/capabilities`) and only
   wires `HTTPChannelPublisher` if `channels` is reported as enabled.
2. **Sticky negative cache in `HTTPChannelPublisher`.** First 503/404
   response sets a `_disabled` flag (or a TTL'd one) that short-circuits
   subsequent `publish()` calls to `ValueError("channels disabled")`,
   which the executor already maps to a single WARN.

Approach 1 is cleaner; approach 2 is cheaper to ship and self-healing
once channels are turned on at runtime.

## Notes

> 2026-05-05 — initial capture during PR #250 review (Should-Fix #3).
