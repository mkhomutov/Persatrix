---
id: ISSUE-0194
summary: "Every open-floor message in a salience-gated channel costs each member persona a leased bid, two wallet calls counted under the persona's ID, and a wallet call refused for going over its budget counts toward its quarantine. So about 33 open-floor messages in a minute quarantine every persona in the channel until an operator releases each one. Under the default auth.mode: disabled anyone can post them; under enabled, any signed-in member can, and until registration requires the enrollment secret anyone can. Before ISSUE-0187's fix the personas' REST history reads counted too and about 22 messages were enough; that fix took REST out, and this wallet path, which is older, is what is left."
status: open
severity: high
area: security
created: 2026-11-03
refs:
  - docs/issues/ISSUE-0187-agent-rest-bursts-count-toward-its-own-quarantine.md
  - docs/issues/ISSUE-0152-one-quarantine-refuses-every-anonymous-caller.md
  - docs/rfcs/0009-phases-3-4-pr-plan.md
  - agents/persona_runtime/salience_gate.py
  - internal/security/middleware.go
  - cmd/orchestrator/ratelimit.go
  - docs/guides/auth.md
---

# ISSUE-0194: Posts into a channel can quarantine its personas through their bids

## Summary

Anyone who can post into a salience-gated channel can quarantine every
persona in it with about 33 open-floor messages in a minute. Each message
costs each member persona a leased bid, which is two wallet calls under its
own ID, and a wallet call refused for going over that ID's budget counts
toward its quarantine. A quarantine lasts until an operator releases each
persona.

## Context

Read at the PR that fixed ISSUE-0187 (RFC 0009 Phases 3–4 plan PR 8):

- **A bid is two wallet calls.** For every open-floor message, each member
  persona reads the channel's history over REST and leases a bid from its
  wallet over gRPC, a lease and a settle, even when it then stays silent
  (`agents/persona_runtime/salience_gate.py`).
- **The wallet budget is the persona's.** The gRPC interceptor counts the
  call under the `x-agent-id` it carries, against `SECURITY_RATE_LIMIT_CALLS`
  (60 a minute by default), and records a violation for each refusal; the
  breaker quarantines after five in ten minutes (`cmd/orchestrator/ratelimit.go`).
  So about 30 messages spend the budget, and the next few refusals
  quarantine the persona.
- **REST no longer adds to it.** Since ISSUE-0187's fix an agent's REST calls
  spend a budget of their own, and a REST refusal counts toward no
  quarantine (`internal/security/middleware.go`). Before that fix the history
  read counted too, and about 22 messages were enough.
- **Who can post.** Under `auth.mode: disabled`, the default, anyone who can
  reach the orchestrator. Under `enabled` a publish must name its caller, so
  a signed-in member can, and until registration requires the enrollment
  secret (plan PR 9) anyone can, by registering an agent's ID.

## Impact

- **Every persona in the channel goes silent until a person steps in.** A
  quarantined ID's wallet calls are refused, so it cannot call its model,
  and a quarantine has no end of its own (RFC 0009 §H).
- **High.** The same grounds as ISSUE-0187 and ISSUE-0152: in the default
  mode, someone with no account stops every persona in a channel with a few
  dozen messages, and it lasts until an operator releases each one. A busy
  channel reaches it without anyone meaning to.
- A bigger `SECURITY_RATE_LIMIT_CALLS` only raises the count, and the
  poster's own budget grows with it.

## Options

1. **Count a refused bid toward no quarantine**, as REST refusals now are:
   the traffic that refuses it is the channel's, not the persona's. The
   breaker would then catch only a persona's own calls, its turns.
2. **Let a persona skip a bid when its budget is low**, so it falls silent
   for the rest of the minute instead of being refused.
3. **Bound what one poster can cost a channel**: a per-channel or
   per-poster limit on open-floor messages, sized below what spends a
   persona's budget.
4. **An end to a quarantine** (ISSUE-0152 step 4) bounds the outage but
   changes RFC 0009 §H.

**Slot: the owner's call.** Until then it belongs in v0.4.0's Known Gaps.

## Notes

> 2026-11-03 — filed from the review of RFC 0009 Phases 3–4 plan PR 8
> ([#1085](https://github.com/mkhomutov/Persatrix/pull/1085), finding F-4),
> which resolved ISSUE-0187: that issue's high outcome still happens through
> this path, and no issue tracked it. Read from the code; not measured.
