---
id: ISSUE-0187
summary: "Since the agents' REST calls carry the hosted agent's ID (ISSUE-0152 step 3), a refused REST call counts toward that agent's own quarantine. A persona's start-up catch-up reads every channel in the deployment one after another, with no pause on a 429, so in a deployment where that comes to more than about 60 reads in a minute the persona is refused five times and quarantined; a busy channel can do the same, since every open-floor message costs each member persona three calls even when it stays silent. It then cannot call its model or publish until an operator releases it. Before, its REST refusals fell into the shared anonymous budget, which quarantines nothing."
status: open
severity: medium
area: security
created: 2026-10-21
refs:
  - docs/issues/ISSUE-0152-one-quarantine-refuses-every-anonymous-caller.md
  - docs/issues/ISSUE-0111-anonymous-wallet-rpcs-share-rate-limit-bucket.md
  - agents/orchestrator_session.py
  - agents/channel_catchup.py
  - agents/channel_catchup_discovery.py
  - agents/channel_history_fetcher.py
  - agents/persona_runtime/salience_gate.py
  - agents/server_reregister.py
  - internal/security/middleware.go
  - cmd/orchestrator/ratelimit.go
---

# ISSUE-0187: An agent's own REST bursts count toward its quarantine

## Summary

Each agent now has its own rate-limit budget on REST, which was the point of
ISSUE-0152's step 3. The same change means a REST call refused for going over
that budget is a violation against the agent, and five violations in ten
minutes quarantine it. The start-up catch-up makes a long run of REST calls
with nothing to slow it down, so a persona in a deployment with many channels
can quarantine itself as it starts; a busy channel can do it more slowly.

## Context

Read at the PR that made the change (ISSUE-0152's steps 1–3):

- **One budget per ID, for both transports.** `cmd/orchestrator` builds one
  `RateLimiter` and hands it to the REST middleware and the gRPC interceptor
  (`internal/security/middleware.go`). Each ID may make 60 calls a minute by
  default (`SECURITY_RATE_LIMIT_CALLS`). An agent's wallet calls carry its ID
  since ISSUE-0111, and its REST calls now do too
  (`agents/orchestrator_session.py`), so both count against one budget.
- **Refusals under a named ID are violations.** The middleware records a
  `ViolationRateLimit` for every refused call that sends an ID, and the
  breaker's rule (`cmd/orchestrator/ratelimit.go`) quarantines after five in
  ten minutes. Calls that send no ID never count.
- **The catch-up makes one call per channel, back to back.** At start-up,
  `replay_channel_history` (`agents/channel_catchup.py`) lists the channels,
  then for every channel in the list reads it to check membership
  (`agents/channel_catchup_discovery.py`), and for each channel the persona
  belongs to reads its history (`agents/channel_history_fetcher.py`). The
  calls run one after another with no pause, and a refused call is logged and
  skipped, so the loop carries on into further refusals.

So a persona's start-up costs about 2 + C + M calls: registration, the list,
one read per channel in the deployment (C) and one per channel it belongs to
(M). Once that passes the budget, every further read is refused and counts.
With the defaults, 40 channels with the persona in 30 of them comes to 72
calls, about 12 refusals, and a quarantine. Direct messages are channels, so
C grows with every person and persona pair that has talked.

## Impact

- **The persona goes silent until a person steps in.** A quarantined ID's
  wallet calls are refused, so it cannot call its model, and its publishes
  are refused. A quarantine has no end of its own (RFC 0009 §H); the operator
  releases the ID through `POST /api/v1/agents/{id}/unquarantine`.
- **It is new with ISSUE-0152's fix.** Before, the catch-up's reads were
  anonymous: once the shared budget ran out they were refused too, and the
  catch-up silently lost the channels left, but nothing was quarantined.
- **Busy live traffic can do the same.** Every open-floor message in a
  salience-gated channel costs each member persona a history read and a
  leased bid, which is two wallet calls, even when it stays silent
  (`agents/persona_runtime/salience_gate.py`); one that speaks adds roster
  reads, its turn's lease and the publish. So about 20 open-floor messages a
  minute spend a silent persona's whole budget. Before, only the two wallet
  calls counted against it.
- **A re-registration can too.** After the orchestrator's gRPC channel
  reconnects, an agent tries to re-register up to five times
  (`REREGISTER_ATTEMPTS` in `agents/server_reregister.py`), the same number
  as the quarantine threshold. After an orchestrator restart the budget is
  fresh, but if the channel only blipped while the budget was spent, five
  refused tries quarantine the agent.
- **Medium severity.** It needs a large deployment or a burst, and the
  release endpoint recovers it; but the agent is out until then.

## Proposed fix / investigation path

1. **Stop the catch-up at the first 429** (or wait out its `Retry-After`)
   instead of skipping to the next channel, so a pass adds at most one
   violation. Test first: a catch-up over more channels than the budget stops
   after one refusal.
2. **Cut the per-channel membership read.** `GET /api/v1/channels` already
   returns each channel's members with their respond policy
   (`handleListChannels`), so the catch-up needs 2 + M calls, not 2 + C + M.
3. **Size the budget**, or document raising `SECURITY_RATE_LIMIT_CALLS` for
   deployments with many channels. The CHANGELOG's upgrade note for the fix
   says so for now.
4. ISSUE-0152's step 4, an end to a quarantine, would bound the outage but
   changes RFC 0009 §H, and stays the owner's call.

**Slot: the owner's call.** Step 1 is a small Python change.

## Notes

> 2026-10-21 — filed from a code reading by the PR that fixed ISSUE-0152's
> steps 1–3, which made each agent's REST calls carry its ID. The call counts
> above are read from the code. One measurement: the offline roundtable
> (three personas on the mock provider, the orchestrator's default limits,
> run natively rather than in Docker) convened twice 25 seconds apart, and
> its 14 messages drew no `rate_limit.violated` and no `agent.quarantined`
> event; the orchestrator logged no 429. No catch-up over many channels was
> run.
