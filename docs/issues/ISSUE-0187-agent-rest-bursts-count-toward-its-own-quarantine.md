---
id: ISSUE-0187
summary: "Since the agents' REST calls carry the hosted agent's ID (ISSUE-0152 step 3), they spend the same budget as its wallet calls, and a refused REST call counts toward its quarantine. Every open-floor message costs each member persona three calls even when it stays silent, so someone with no account, in either auth mode, can post about 22 messages in a minute into a channel and quarantine every persona in it until an operator releases each one; raising the budget does not stop that. Short of a quarantine, a burst of an agent's own REST calls leaves its wallet calls nothing, so its bids and turns fail closed. The start-up catch-up, which read every channel back to back past a 429, now stops at the first one."
status: open
severity: high
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
  - agents/wallet_client.py
  - docs/guides/auth.md
  - internal/security/middleware.go
  - cmd/orchestrator/ratelimit.go
---

# ISSUE-0187: An agent's own REST bursts count toward its quarantine

## Summary

Each agent now has its own rate-limit budget on REST, which was the point of
ISSUE-0152's step 3. The same change means a REST call refused for going over
that budget is a violation against the agent, and five violations in ten
minutes quarantine it. A busy channel can do that, and anyone who can post
to it can make it busy; before a quarantine, a burst of REST calls already
leaves the agent's wallet calls nothing. The start-up catch-up made a long
run of REST calls with nothing to slow it down; it now stops at its first
refusal.

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
  calls run one after another with no pause. As filed, a refused call was
  logged and skipped, so the loop carried on into further refusals.
- **Anyone can post to a channel.** Channel publishes stay open to callers
  with no account in both auth modes (`docs/guides/auth.md`), the publish
  body's `sender_id` is taken on trust, and the channel list names each
  channel's members.

So a persona's start-up costs about 2 + C + M calls: registration, the list,
one read per channel in the deployment (C) and one per channel it belongs to
(M). With the defaults, 40 channels with the persona in 30 of them comes to
72 calls; as filed, that was about 12 refusals and a quarantine. Direct
messages are channels, so C grows with every person and persona pair that
has talked.

## Impact

- **The persona goes silent until a person steps in.** A quarantined ID's
  wallet calls are refused, so it cannot call its model, and its publishes
  are refused. A quarantine has no end of its own (RFC 0009 §H); the operator
  releases the ID through `POST /api/v1/agents/{id}/unquarantine`.
- **An outsider can do it on purpose.** Someone with no account posts about
  22 open-floor messages in a minute into a salience-gated channel, as one of
  its members, inside the anonymous budget. Each member persona spends three
  calls on each (below), is refused five times, and is quarantined: the
  lockout ISSUE-0152 describes, one channel at a time. Wallet calls alone
  allowed it before, at about 33 messages; the agents' REST ID brings that
  down to about 22. A bigger `SECURITY_RATE_LIMIT_CALLS` only raises the
  count, and the poster's budget grows with it.
- **Before any quarantine, REST bursts starve the agent's wallet calls.** A
  start-up catch-up that comes to 60 calls (30 channels, a member of 28)
  spends the whole budget, so for the rest of that minute a bid's lease is
  refused and the bid fails closed to silence, a turn's model call fails
  closed (RFC 0023 §F), a close-summary lease becomes the
  `[interaction summary unavailable]` placeholder, and a refused
  `SettleLease` is reaped at its granted estimate: ISSUE-0111's failure, one
  agent at a time.
- **The catch-up half is new with ISSUE-0152's fix.** Before, the catch-up's
  reads were anonymous: once the shared budget ran out they were refused
  too, and the catch-up silently lost the channels left, but nothing was
  quarantined and the agent's wallet budget was untouched.
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
- **High severity.** Someone with no account, in either auth mode, can
  quarantine every persona in a channel with a few dozen messages, and it
  lasts until an operator releases each one: ISSUE-0152's own grounds for
  High. The organic paths, a large deployment or a busy channel, are slower
  to reach.

## Proposed fix / investigation path

1. **Stop the catch-up at the first 429** instead of skipping to the next
   channel, so a pass adds at most one violation. Fixed: see Notes.
2. **Cut the per-channel membership read.** `GET /api/v1/channels` already
   returns each channel's members with their respond policy
   (`handleListChannels`), so the catch-up needs 2 + M calls, not 2 + C + M.
3. **Size the budget**, or document raising `SECURITY_RATE_LIMIT_CALLS` for
   deployments with many channels. The CHANGELOG's upgrade note for the fix
   says so for now. It does nothing against an outsider.
4. **Keep wallet calls out of REST bursts**: count an agent's REST and
   wallet calls against separate budgets, so a burst of reads cannot refuse
   its leases.
5. **Verify the sender.** Only a publish the orchestrator can tie to its
   sender closes the outsider path: RFC 0009 Phase 4's agent tokens on the
   REST routes (row A4), and ISSUE-0117's `auth.agent_ingress` close knob.
6. ISSUE-0152's step 4, an end to a quarantine, would bound the outage but
   changes RFC 0009 §H, and stays the owner's call.

**Slot: the owner's call.** Step 1 is fixed; step 5 is row A4.

## Notes

> 2026-10-21 — filed from a code reading by the PR that fixed ISSUE-0152's
> steps 1–3, which made each agent's REST calls carry its ID. The call counts
> above are read from the code. One measurement: the offline roundtable
> (three personas on the mock provider, the orchestrator's default limits,
> run natively rather than in Docker) convened twice 25 seconds apart, and
> its 14 messages drew no `rate_limit.violated` and no `agent.quarantined`
> event; the orchestrator logged no 429. No catch-up over many channels was
> run.

> 2026-10-22 — the review of
> [#1050](https://github.com/mkhomutov/Persatrix/pull/1050) found the
> outsider path and the wallet starvation, added above, and raised the
> severity from medium to high. Step 1 is fixed in the same PR: a 429 on a
> membership or history read stops the pass (`OrchestratorRateLimitedError`,
> `agents/channel_history_fetcher.py`), so a pass adds at most one
> violation; `tests/unit/python/test_channel_catchup_gaps.py` runs a
> catch-up over more channels than the budget and sees one refusal. The
> re-registration path, the live traffic, the wallet starvation and the
> outsider path stay open.
