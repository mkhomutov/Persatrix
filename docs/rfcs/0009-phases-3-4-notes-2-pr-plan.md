# RFC 0009 — PR Implementation Plan (Phases 3–4): notes on merged PRs, part 2

**Plan**: [RFC 0009 Phases 3–4 PR plan](0009-phases-3-4-pr-plan.md)
**RFC**: [0009-security-sandboxing.md](0009-security-sandboxing.md)
**Part 1**: [notes on merged PRs](0009-phases-3-4-notes-pr-plan.md), PRs 3 and 5 to 7
**Split out**: 2026-11-04, unchanged, from the plan, when PR 4's review
took the plan toward its 8 000-word cap and part 1 had no room for PR 8.
Each merged PR's implementation note and review findings from PR 8 on move
here; its scope, tests and estimate stay in the plan.

---

## PR 8 — `feature/v040-rfc0009-token-middleware`

Merged as [#1085](https://github.com/mkhomutov/Persatrix/pull/1085) on
2026-11-04.

**Implementation note (2026-11-03)**: how this PR meets
[the plan's scope](0009-phases-3-4-pr-plan.md#pr-8--featurev040-rfc0009-token-middleware).
Each point not settled there is a new default this PR's review confirms or
overturns.

- **Where.** `internal/server/agent_token_check.go` reads the token in the
  auth middleware before the route's policy, so a refused token gets its own
  code on any route; `internal/server/publish_sender.go` holds the sender
  check; `security.WithAgentID` hands the limiter the verified agent.
- **Refusals.** A token `Check` refuses gets a 401 with its code; a good one
  beside an `X-Agent-ID` naming another agent gets `AGENT_TOKEN_INVALID`; a
  token beside an account credential, bearer or cookie, gets a 400, which no
  agent reads as a token refusal. Each writes `agent.token_invalid` with a
  reason, the source, and the claimed ID if an agent could hold it, never the
  token. A Python drift test pins the codes to the agents' constants.
- **Refusals per source (new default).** A refused call never reaches the
  rate limiter, and each line is flushed at once, so one address is refused
  at most 120 times a minute, sized as registrations are; past that, 429 and
  no line.
- **The sender.** A refused publish gets 403 and `authz.denied` with reason
  `sender_not_caller`. A caller with no credential is refused too, but until
  PR 9 requires the enrollment secret anyone can register an agent's ID and
  post as it (review F-1), and anonymous reads stay. The CLI's `channel send` sends
  the OS username unless given `--as`, so a person whose participant ID
  differs passes `--as`; PR 11's guide says so.
- **Budgets.** One limiter still: an agent's REST budget is kept under
  `rest:<id>`, the same size as its wallet budget, and a REST refusal records
  no violation in either mode. Under `enabled` both go by the token's agent;
  a call naming an agent without its token spends the anonymous budget (new
  default), a registration its address's (review F-2), and a header naming
  a quarantined agent is still refused. Wallet calls still feed the
  breaker, so about 33 posts a minute into a channel can still quarantine
  its personas through their leased bids: ISSUE-0194.
- **F-9.** `IssuableAgentID` refuses `local`; a server test pins it to
  `channels.DefaultPrincipalID`.
- **F-15.** `AgentServer.stop` deregisters after the gRPC grace, the order
  RFC 0004's shutdown sequence gives, so an RPC finishing inside the grace
  still publishes. A persona's channel turn runs on its event loop, which
  the grace does not wait for: one still running after it loses its publish
  under `enabled`, as it would if the process were killed. `close_memory`'s
  wallet calls stay PR 10's.
- **F-4.** No code of its own for a replaced token: an agent told "replaced"
  would register again, so a rolling restart's old process would take the
  ID back from the new one, each retiring the other's token. The race F-4
  found stays: it needs a stalled registration overtaken by a later one, and
  the stopped agent logs why.

**Review findings** (PR 8): F-1 to F-12 are an independent review's, each
Medium one proved by running it, and F-13 the author's.

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| F-1 | Medium | Registration admits a caller that shows no secret and deregistration is open, so anyone can register a live agent's ID, publish with the token, and stop the real agent, whose token now reads revoked, until it restarts. The auth guide, the CHANGELOG and ISSUE-0187's note said nobody without a credential could post | Docs fixed in-PR: the residual is back in each. The code → deferred to PR 9 (noted there) |
| F-2 | Medium | Under `enabled` a registration shows no token, so it spent the budget every caller with no account shares: anyone spending that kept every agent from registering, and an agent that missed its registration had every publish refused | Fixed in-PR: under `enabled` a registration spends its address's budget, `register:<address>`; `TestRegistration_UnderEnabledSpendsItsAddressesBudget` |
| F-3 | Medium | The two new audit lines anyone can cause copied the request's path and method whole, so one refused call wrote a line of about 900 KB | Fixed in-PR: both cut to 256 bytes, with a test for each line |
| F-4 | Medium | ISSUE-0187 was resolved while its high outcome still happens through the personas' wallet bids, about 33 posts a minute, and no issue tracked that; its note also called step 5 fixed | Filed as [ISSUE-0194](../issues/ISSUE-0194-channel-posts-quarantine-personas-through-their-bids.md) (high), for PR 16's Known Gaps; ISSUE-0187's note corrected |
| F-5 | Low | PR 7's F-18 starts doing harm here: under `enabled` an agent with no token has every publish refused with 403, which its client never takes as a cue to register | → deferred to PR 9 (noted there) |
| F-6 | Low | An agent can register under a signed-in account's participant ID and publish as that person | → deferred to PR 9 (noted there) |
| F-7 | Low | The autonomous-channels guide still said a persona refused over its REST budget is quarantined | Fixed in-PR |
| F-8 | Low | No upgrade note said that an agent from before PR 7 shows no token, so under `enabled` its publishes are refused | Fixed in-PR: added to the upgrade note on publishes |
| F-9 | Info | Two mutants survived: dropping the audit line's filter on the claimed ID, and dropping the refusal of a bare `Authorization` header beside a token | Fixed in-PR: a test for each; both mutants are now caught |
| F-10 | Info | With deregistration after the grace, the orchestrator may still send work to a closing agent for up to the grace, 30 s by default. RFC 0004 gives that order | Noted |
| F-11 | Info | `channel_session_override.go` and `channels/principal_restamp.go` still call the sender unchecked, and say agent tokens have not shipped; partly true until PR 9 | → deferred to PR 9 (noted there) |
| F-12 | Info | +1,125 lines at review, 688 of them tests, past the ~450 estimate and the 500-line target | Noted |
| F-13 | Low | One limiter now holds two budgets per agent, so `SECURITY_RATE_LIMIT_MAX_AGENTS`, 1 000 by default, covers about 500 agents before the least recently used is dropped and starts afresh | Fixed in-PR: the setting's comment and the CHANGELOG say so |
