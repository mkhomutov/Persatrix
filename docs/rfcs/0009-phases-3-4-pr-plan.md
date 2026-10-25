# RFC 0009 — Agent Identity, Security & Sandboxing — PR Implementation Plan (Phases 3–4)

**RFC**: [0009-security-sandboxing.md](0009-security-sandboxing.md)
**Status**: ⬜ Not started
**Created**: 2026-10-21
**Branch prefix**: `feature/v040-rfc0009-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Spawned from**: row R2 of the [v0.4.0 plan](../v0.4.0-plan.md#progress); its rows A3 and A4 are this plan's two phases. Phases 1–2 are in the [v0.3.0 plan](0009-pr-plan.md).

> Sizes are calibrated at 3.5× naive, the factor the
> [Phases 1–2 plan](0009-pr-plan.md) recommends after every security PR there
> landed above twice its estimate. Every PR stays under 500 changed lines and
> writes its failing test first.

---

## Overview

Today any process can claim any agent id. The orchestrator believes the
`X-Agent-ID` header, the agent-facing REST routes are public under
`auth.mode: enabled` because agents hold no accounts, a signed-in account can
publish under any sender id, and nothing can hold an action until a person
approves it. This plan gives every agent an identity the orchestrator issued,
checks it on every call an agent makes, binds every publish to the caller
that made it, and builds the queue where held actions wait. It delivers RFC
0009's two v0.4.0 phases in 16 PRs, the first two already merged as one:

- **A3, Phase 3** — tool arguments checked against each tool's schema before
  the tool runs, a CPU limit for `shell_exec`, an output cap any tool can
  take, and a `resource_limits:` block per agent.
- **A4, Phase 4** — agent tokens issued at registration and checked on the
  agents' REST and wallet calls; a fleet enrollment secret at registration;
  the pending-approval queue on A1's society tables, with its decision event
  to the agent; tools tagged `irreversible`; and the `hitl` switch. It closes
  [ISSUE-0152](../issues/ISSUE-0152-one-quarantine-refuses-every-anonymous-caller.md),
  [ISSUE-0140](../issues/ISSUE-0140-agent-fleet-401-on-roster-fetch-under-auth.md),
  [ISSUE-0117](../issues/ISSUE-0117-agent-ingress-close-knob.md)
  and [ISSUE-0187](../issues/ISSUE-0187-agent-rest-bursts-count-toward-its-own-quarantine.md).

The hold itself is not here. RFC 0028 Phase 2a's decision hold replaces §F's
agent-side pause ([RFC 0028 plan](0028-pr-plan.md) E-9, recorded in §F's
dated note): this plan builds the queue the hold opens rows on, and the
release, the tokens a person's approval mints and the `decision.hitl_*`
audit events are RFC 0028's PRs 11–14a.

**The no-change contract.** Under `auth.mode: disabled`, with `hitl.enabled`
left at its default and no organization, an agent behaves as in v0.3.16
except that its REST calls carry its id, as the v0.4.0 plan's acceptance
allows. Every limit defaults to today's behaviour (G-3), the argument check
refuses only what fails today (G-2), and `make eval-replay` replays every
golden unchanged. Each PR on the agent's request path proves it.

### What is already in place (do not rebuild)

- `agents/tools/registry.py` — the `@tool` decorator builds a JSON schema
  from each tool's signature (`{"type": "object", "properties": …,
  "required": …}`); nothing checks a call against it.
  `ToolDefinition` has no `irreversible` field.
- `agents/persona_runtime/tool_round.py` (`_execute_tools`) and
  `agents/base.py` (`_execute_tools`) — the two places a tool runs, each as
  `await tool_def.func(**call.input)`; a wrong argument surfaces as a
  `TypeError` the wrapper turns into a failed `ToolResult`.
- `agents/tools/builtin.py` — `MAX_OUTPUT_BYTES = 102_400` and `_truncate()`
  for `file_read`, `shell_exec` and `http_request`; `shell_exec` clamps its
  timeout to `permissions.shell.max_execution_seconds`
  ([ISSUE-0150](../issues/ISSUE-0150-schema-valid-config-cannot-enable-shell-exec-or-http-request.md)).
  `agents/tools/sandbox.py` has `PathValidator` and only TODOs for
  `ResourceLimiter` and `OutputSizeLimiter`. `http_request` opens its own
  HTTP session per call.
- `internal/security/audit_event.go` — `tool.arg_invalid`,
  `agent.token_issued` and `agent.token_invalid` are declared with no
  emitter; the `hitl.*` constants are reserved (RFC 0028 PR 13 retires them).
  There is no Python-to-Go audit path; Python audit records are structured
  log lines (`agents/memory/_facts_audit.py`).
- `internal/server/auth_policy.go` — the route-to-policy table; the agent
  ingress (register, deregister, channels list and get, history, publish,
  convene) is `public`, the directory `authenticated`, persona recall
  `operator`. `auth_middleware.go` reads an account session from
  `Authorization: Bearer` or the session cookie, and `principal.go` gives a
  signed-in request its verified participant id (RFC 0039 §F).
- `internal/server/channel_handlers.go` — `handlePublishMessage` takes
  `sender_id` from the request body and checks it against nothing.
- `internal/server/agent_handlers.go` — registration upserts into the
  in-memory registry and answers with no token; `SECURITY_UNQUARANTINE_TOKEN`
  is the env-var-and-bearer pattern a fleet secret copies.
- `agents/server.py` and `server_reregister.py` — the shared session for
  every orchestrator call, `_self_register`, and the re-registration watcher
  (ISSUE-0125). `agents/wallet_client.py` sends `x-agent-id` on wallet calls.
- [#1050](https://github.com/mkhomutov/Persatrix/pull/1050), merged — this
  plan's PRs 1–2: a quarantine no longer refuses every anonymous caller, and
  `agents/orchestrator_session.py` sends the agent's id on every
  orchestrator call. It files ISSUE-0187, high: an agent's REST and wallet
  calls now share one budget, and a REST call refused by it counts toward
  the agent's quarantine, so anyone who can post about 22 messages a minute
  into a channel quarantines every persona in it, and a burst of REST calls
  starves the agent's wallet calls.

### What this plan provides to the RFC 0012 and RFC 0028 plans

Each request in the [RFC 0012 plan](0012-pr-plan.md#what-this-plan-needs-from-the-prerequisite-plans)
and the [RFC 0028 plan](0028-pr-plan.md#what-this-plan-needs-from-the-prerequisite-plans)
made of row A4, and where this plan answers it:

| Requested by | Request | Provided | Revision |
|--------------|---------|----------|----------|
| RFC 0012 C1 PR 4 | The organization read among the token-checked agent routes | PR 9: one helper registers every agent route, and moves any route already waiting on A4 onto it (G-6) | — |
| RFC 0012 PR 8 | Verified sender ids under `auth.mode: enabled` | PR 8: a publish's sender is the token's agent, or the signed-in account's own participant (G-7) | Covers accounts as well as agents |
| RFC 0012 C3 | The queue, its release path, a call that withdraws an open approval, an approve hook inside A1's transaction | PRs 12–13 | The release path is the queue's approve; the spend that runs a held action stays RFC 0028 PR 13's |
| RFC 0012 PR 10a | A tie group, so approving one row withdraws the group's other open rows in the same transaction | PR 13, with a `tie` kind (G-9) | A group is opened whole, in one call; approving withdraws its pending rows and is refused once a sibling is approved |
| RFC 0012 C3 | A decision event to the persona when one of its approvals is approved, denied, expired or withdrawn | PR 14 | At least once; the agent confirms it against the queue before acting (G-10) |
| RFC 0028 PRs 13–14a | The queue for a persona's holds and a workflow step's; approve, deny, expiry, withdraw | PRs 12–13 | Expiry also ends approved rows never spent; under `disabled`, approve and deny fail closed until RFC 0028 PR 13 adds the approval secret (G-8) |
| RFC 0028 PRs 13–14a | The agent's open-hold call and PR 13's spend route among the token-checked routes | PR 12 (open-hold); PR 9's helper for the spend route | — |
| RFC 0028 PRs 13–14a | Agent tokens, so an agent token cannot approve | PRs 5–9; PR 12's approve route admits accounts only | — |
| RFC 0028 PRs 13–14a | No agent-side pause and no `hitl.*` event | The queue writes no audit event of its own; RFC 0028 PR 13's `decision.hitl_*` events are the one family (G-9) | PR 13 needs a `withdrawn` event as well, which ties and the withdraw call produce |
| RFC 0028 PR 11 | Tools tagged `irreversible`; where `hitl_enabled` lives and its default | PR 15: `hitl.enabled` in `config/security.yaml`, default `false` | Not `config/agents.yaml`, where §F put it: one switch per deployment, which RFC 0028 PR 11's `hitl:` block extends |
| RFC 0028 PR 10 (E-11) | A held call keyed by its checked arguments | PR 3 runs the check before the pre-tool seat (G-2) | RFC 0028 PR 10 tests that order |

---

## Phase 0 — open questions and design points

RFC 0009's open questions that belong to these phases, settled by the
2026-10-18 default where one exists (questions 9 and 10) and otherwise by a
new default. Accepting this plan confirms those defaults; each *new default*
is confirmed or overturned in the review of the PR named.

| # | Question | Answer | Settled in |
|---|----------|--------|-----------|
| OQ 1 | Token delivery in proto | No proto field: the token comes back in the REST registration response, which question 9 makes the route. | Resolved by OQ 9; PR 6 |
| OQ 4 | Token revocation | A per-process list, since v0.4.0 runs one orchestrator: deregistration revokes the agent's tokens, and a restart revokes all of them by changing the key. | New default; PR 5 |
| OQ 9 | Which calls carry the token | **The 2026-10-18 plan-opening default is confirmed** by the agent-identity lock: every REST route an agent calls, with a fleet enrollment secret at registration, plus the wallet's gRPC calls, which Phase 4's deliverable 3 already named. RFC 0040 Phases 2–3 stay unslotted. | 2026-10-18 default; PRs 6–10 |
| OQ 10 | Where pending approvals are kept | **The 2026-10-18 plan-opening default is confirmed** by the society-backend lock: rows in A1's society tables in `data/channels.db`, which survive a restart. | 2026-10-18 default; PR 12 |

Points the RFC leaves open or states two ways, each settled by a new default
in the PR named:

- **G-1 Phase 4 needs Phase 3's argument check, so A3 stays core whole.**
  RFC 0028's hold is keyed by a hash over a call's argument values (its
  E-11). Without a check that reads each value in one form, `"30"` and `30`
  are two calls and a retried call opens a second approval. Ruling (n) cuts
  Phase 3 only if Phase 4 needs none of it, so Phase 3, limits included,
  stays core. RFC 0009's amendment records why.
- **G-2 The check reads arguments in one form and refuses only what fails
  today.** Calls are checked with `jsonschema`, already a dependency, against
  the schema `@tool` generates. A missing required argument or an unknown
  one, each a `TypeError` today, is refused before the tool runs with a
  structured error naming the argument. A value given in a lossless other
  form, the string `"30"` for an integer, is converted, so the tool and the
  hold's hash see one form. Where a tool clamps a value today, as
  `shell_exec` clamps its timeout, it still clamps. No Pydantic models: no
  second source of truth and no new dependency. The check runs before
  RFC 0028's pre-tool seat, so a held call's hash covers checked arguments.
  PR 3.
- **G-3 Limits default to today's behaviour.** The 100 KB output cap stays
  on the three tools that have it; any other tool takes a cap only when
  `resource_limits.tool_output_bytes` sets one. Shell wall time keeps
  `permissions.shell.max_execution_seconds`, adopted rather than duplicated.
  The CPU limit for `shell_exec` is off unless
  `resource_limits.shell_cpu_seconds` sets it, and is set in the child
  process on POSIX. §D's numbers (10 s of CPU, 64 KB) are the guide's
  recommendation for production. §D's 60 tool calls a minute is not built:
  tool calls never reach the orchestrator, and the wallet's budget already
  bounds a runaway. PR 4.
- **G-4 Python audit events stay log records.** `tool.arg_invalid` and
  `tool.resource_limited` are written as structured audit log lines, the
  pattern `_facts_audit.py` uses, which reach the orchestrator's log stream
  but not its checksummed audit chain. A Python-to-Go audit call is a new
  contract nothing else needs; RFC 0009's amendment records the gap. The log
  stream (`LogService.StreamLogs`) stays outside the token check (PR 10), so
  anyone who reaches the gRPC port can write log lines under any agent's
  name, these among them; PR 16 files that as a Known Gap. PRs 3–4.
- **G-5 The token.** HMAC-SHA256 over the agent id, issue time, expiry and a
  nonce, with a key made at startup and never stored (§B rule 1). It carries
  no capability claims: nothing in v0.4.0 reads them (RFC 0029 Phase 2 is
  unslotted). It lives 24 hours; a call refused for an expired or unknown
  token re-registers and retries once. It travels in an `X-Agent-Token`
  header on REST and `x-agent-token` metadata on gRPC, never in
  `Authorization`, which carries account sessions, and only on calls to the
  orchestrator. A request carrying both credentials is refused. PRs 5–7.
- **G-6 What each agent route admits under `enabled`.** The issues'
  2026-10-18 defaults, and the routes later rows add:

  | Route | Admits |
  |-------|--------|
  | `POST /api/v1/agents/register` | the enrollment secret, or an operator session |
  | `DELETE /api/v1/agents/{id}` | the token of the agent the path names, or an operator session |
  | `POST /api/v1/channels/{id}/convene` | an agent token, or an operator session |
  | `GET /api/v1/channels`, `GET /api/v1/channels/{id}`, `GET /api/v1/channels/{id}/messages`, `POST /api/v1/channels/{id}/messages` | an agent token, or any signed-in account |
  | `GET /api/v1/agents` | an agent token, or any signed-in account |
  | `POST /api/v1/personas/{participant_id}/recall` | the token of the persona the path names, or an operator session |
  | `/api/v1/agents/{id}/holds` — open, list, withdraw (PR 12) | the token of the agent the path names |
  | RFC 0028 PR 13's spend route; RFC 0012 C1 PR 4's organization read | the token of the agent the call is for |

  Under `disabled` nothing changes: every caller is `local`, ids stay
  self-reported, and tokens are issued but not checked. PR 9.
- **G-7 Every publish is bound to its caller.** Under `enabled`, an
  `X-Agent-ID` that differs from a valid token's agent is refused with 401
  and an `agent.token_invalid` audit line. A publish's `sender_id` must be
  the token's agent, or, for a signed-in account, the account's own verified
  participant id, with `participant_type` `user`; anything else is refused
  with 403 and an audit line. So only an agent can speak as that agent, and
  no account can relay a manager's directive under the manager's name. The
  CLI's `channel send` works as the signed-in person only. The rate limiter
  and the circuit breaker key on the token's agent, and a refused token does
  not count toward that agent's quarantine. Binding the sender is ISSUE-0187's
  step 5, but a member can still drive a persona's calls by posting, so,
  as a new default, the plan also takes its step 4 in both modes: an agent's
  REST calls take a budget of their own, apart from its wallet calls, and a
  REST call refused by that budget is not a violation toward its quarantine.
  So nobody's posts can quarantine a persona or starve its wallet calls.
  PR 8.
- **G-8 Approvals fail closed until there is someone to approve.** The
  approve and deny routes admit operator sessions only. Under `disabled`
  every caller is the anonymous `local`, so they refuse until RFC 0028 PR 13
  adds its approval secret. Nothing opens a hold before RFC 0028 PR 14, so
  nothing waits on them meanwhile. PR 12.
- **G-9 The queue's rows, routes and events.** A row has a kind:
  - **`action`** — a held action or tool call. Its actor is the agent that
    holds it; its hash is the one RFC 0028 computes (E-11), which for a held
    publish covers the class and target channel only, so the unique index
    keeps one open hold per publish target. Only this kind is spent with a
    token.
  - **`graph_change`** — an organization change proposed over REST (RFC 0012
    PR 11). Its actor is the proposing operator, kept so the rule that
    another operator approves can be checked; its hash covers the change in
    canonical form.
  - **`tie`** — one of a directive tie's rows (RFC 0012 PR 10a). Its actor is
    the persona whose directives tie; its hash covers the directive's id.

  Operator routes live under `/api/v1/approvals` (list, get, approve, deny);
  an agent's own under `/api/v1/agents/{id}/holds` (open, list its rows,
  withdraw). Nothing sits under `/workflows`: the queue is not a workflow
  feature, and the one workflow change ruling (x) allows, a step waiting on
  its hold, is RFC 0028 PR 14a's. The queue writes no audit event of its
  own: RFC 0028 PR 13's `decision.hitl_*` events, written on these
  transitions, are the one family (its E-9), and they carry ids, kind and
  class, never the held text. PRs 12–13.
- **G-10 The decision event.** A new `AgentService` call carries an
  approval's id, kind and state to the agent that opened the row, whenever
  the row leaves `pending` or `approved`, sent after the transaction
  commits. Delivery is at least once: a row stays undelivered until the call
  succeeds and is resent when the agent re-registers. Only `action` and
  `tie` rows, whose actor is an agent, are delivered; a `graph_change` row's
  actor is an operator, so it gets no event and never waits as undelivered.
  The orchestrator's calls into an agent are not authenticated, so the agent
  acts on no event alone: it reads the row back on its token-checked holds
  route and acts on what the queue says. PR 14's handler does that and acts on nothing else;
  RFC 0028 PR 14 releases on `approved`, RFC 0012 PR 10a applies tie
  decisions. PR 14.
- **G-11 A held action's text is kept only while it waits.** The row holds
  what an operator needs to decide and what the release needs to run — the
  class, the tool or action, its target, its arguments or text — readable on
  the operator routes and by the actor's own token. It is cleared when the
  row settles, that is, leaves `pending` and `approved`, so
  `data/channels.db` does not keep a held message's text after the decision.
  A1's table provides the column. PR 12.
- **G-12 What `irreversible` covers.** `file_write`, and `http_request` for
  its writing methods: a tool may declare the argument values that make a
  call irreversible, so a `GET` is never held. `shell_exec` and the memory
  tools stay out, as §F says. RFC 0028 PR 11's irreversible class reads the
  tag. PR 15.
- **G-13 `hitl.enabled` defaults to `false`.** §F calls the gate opt-in per
  deployment. With the default, v0.3.16 deployments hold nothing new; the
  two classes RFC 0028 makes required in every environment hold regardless
  (its OQ 7). PR 15.
- **G-14 The secret stays with the agent.** The enrollment secret and the
  agent token are removed from the environment of every process a tool
  starts, so a prompt-injected `shell_exec` cannot read them. A holder of
  the fleet secret can still register under any id; a re-registration of a
  live id from another address is audited, and the guide states the limit.
  PRs 4 and 6.

---

## Progress Overview

| PR | Row | Title | Branch | Status | GitHub PR | Merged |
|----|-----|-------|--------|--------|-----------|--------|
| 1 | A4 | ISSUE-0152 steps 1–2: one quarantine no longer refuses every anonymous caller | `fix/quarantine-spares-anonymous-callers` | ✅ | [#1050](https://github.com/mkhomutov/Persatrix/pull/1050) | 2026-10-22 |
| 2 | A4 | ISSUE-0152 step 3, agent half: agents send their id on REST | (in PR 1) | ✅ | [#1050](https://github.com/mkhomutov/Persatrix/pull/1050) | 2026-10-22 |
| 3 | A3 | Tool arguments checked against the tool's schema | `feature/v040-rfc0009-arg-schemas` | ⬜ | — | — |
| 4 | A3 | Shell CPU limit, output caps, `resource_limits:` | `feature/v040-rfc0009-resource-limits` | ⬜ | — | — |
| 5 | A4 | Agent tokens: issue, check, revoke (Go) | `feature/v040-rfc0009-agent-token` | ⬜ | — | — |
| 6 | A4 | Registration returns a token and accepts the enrollment secret | `feature/v040-rfc0009-enrollment` | ⬜ | — | — |
| 7 | A4 | Agents send the secret, keep their token and refresh it | `feature/v040-rfc0009-agent-token-client` | ⬜ | — | — |
| 8 | A4 | Token check on REST; every publish bound to its caller | `feature/v040-rfc0009-token-middleware` | ⬜ | — | — |
| 9 | A4 | The agent routes' policies under `enabled` | `feature/v040-rfc0009-agent-route-policy` | ⬜ | — | — |
| 10 | A4 | Token check on the wallet's gRPC calls | `feature/v040-rfc0009-grpc-token` | ⬜ | — | — |
| 11 | A4 | MT-AUTH-001 v1.1; the auth guide's agent section | `feature/v040-rfc0009-auth-mt` | ⬜ | — | — |
| 12 | A4 | Pending-approval queue: open, list, approve, deny | `feature/v040-rfc0009-approval-queue` | ⬜ | — | — |
| 13 | A4 | Expiry, withdraw, tie groups, the approve hook | `feature/v040-rfc0009-approval-lifecycle` | ⬜ | — | — |
| 14 | A4 | The approval decision event to the agent | `feature/v040-rfc0009-approval-event` | ⬜ | — | — |
| 15 | A4 | `irreversible` tags and the `hitl` switch | `feature/v040-rfc0009-irreversible` | ⬜ | — | — |
| 16 | A4 | The Known Gap issues; Upgrade Notes lines | `feature/v040-rfc0009-known-gaps` | ⬜ | — | — |
| 17 | — | Review follow-ups and closeout | `feature/v040-rfc0009-closeout` | ⬜ | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ✂️ Cut

## Dependency graph

```
R2 (this plan accepted)
PR 1 (#1050, with PR 2)                            (merged; waited on no row)
PR 3 ──► PR 4                                      ═══ A3 done
PR 3 ──► PR 5 ──► PR 6 ──► PR 7 ──► PR 8 ──► PR 9 ──► PR 10 ──► PR 11
A1 done ──► PR 12 ──► PR 13 ──► PR 14
PR 9 ──► PR 12
PR 3 ──► PR 15
PR 11, PR 14, PR 15 ──► PR 16 ──► PR 17           ═══ A4 done
A4 done ──► RFC 0028 B3 ──► RFC 0012 C3
```

The hard edges, each an RFC phase's own dependency; two narrow the
v0.4.0 plan's [order](../v0.4.0-plan.md#implementation-prs) and are marked:

- **PR 3 before A4's PR 5 and PR 15** — Phase 4 lists Phase 3 as its
  dependency, and G-1 is why. *Revision:* the v0.4.0 plan orders all of A3
  before A4; only the argument check gates A4, so PR 4 may land beside it.
- **A1 before PR 12** — the queue's rows live in A1's society tables, behind
  its interface. *Revision:* the v0.4.0 plan orders A1 before all of A4;
  PRs 5–11 touch no society table and need not wait.
- **PRs 5 → 9** — the token exists before registration returns it; agents
  send the secret and the token before any route requires them; the check
  runs before a route's policy depends on it. So no step leaves the fleet
  refused.
- **PR 9 before PR 12** — the holds route is an agent route.
- **PR 1 waits on no row**, as the v0.4.0 plan says; step 3's CLI half
  stays out under the Rust CLI freeze.

---

## PR Sequence

Every PR: the failing test first and confirmed red; `make lint` and the
language's suites green; `make validate` after any config change; docs,
glossary and status moved in the same PR; review findings recorded under the
PR, numbered `F-n`. Every PR on the request path also replays
`make eval-replay` unchanged.

### PRs 1–2 — [#1050](https://github.com/mkhomutov/Persatrix/pull/1050)

ISSUE-0152 steps 1 and 2, and step 3's agent half, in one PR: the release
call is exempt from the anonymous refusal, a quarantine refuses only the id
it names, and `agents/orchestrator_session.py` sends the hosted agent's id on
every orchestrator call. It files ISSUE-0187, which PR 8 closes (G-7).
ISSUE-0152 stays open for step 5, which PR 11 closes.

### PR 3 — `feature/v040-rfc0009-arg-schemas`

**Scope**: `agents/tools/registry.py` (301) — a `validate_arguments` step and
the per-parameter narrowing a tool may declare; `tool_round.py` and
`base.py` (505, which RFC 0028 PR 6 also splits — whichever merges second
rebases) call it before the tool runs and before RFC 0028's pre-tool seat.
**Implementation**: G-2. A refused call does not run; the model gets a
structured `tool_argument_validation_error` naming the argument, and a
`tool.arg_invalid` audit line is written (G-4). The converted arguments are
the ones the tool receives.
**Tests**: a missing or unknown argument is refused before the tool runs, on
both the persona and the task path; `"30"` for an integer reaches the tool
as `30`; a value the tool clamps today is still clamped; a path-traversal
string still reaches `PathValidator`, whose job it stays; every golden
replays unchanged.
**Acceptance**: Phase 3 items 1, 2 and 6 for arguments.
**Estimate**: ~400 lines.

### PR 4 — `feature/v040-rfc0009-resource-limits`

**Scope**: `ResourceLimiter` and `OutputSizeLimiter` in
`agents/tools/sandbox.py`; `shell_exec` in `builtin.py`; the output cap
applied in the registry's wrapper; a `resource_limits:` block in
`schemas/agent.schema.json` and `config/agents.yaml`, read beside
`permissions.shell`; the scrubbed environment for tool processes (G-14).
**Implementation**: G-3 and G-14. A set CPU limit is applied in the child
before `exec`, so a CPU-bound command is killed by the kernel; on Windows
only the wall time applies, as §Security Considerations 4 says. A cut
output keeps the truncation notice `_truncate` writes today. A limit hit
writes a `tool.resource_limited` audit line.
**Tests**: with a CPU limit set, a CPU-bound command ends at it; with none
set, the same command runs as today; a tool given an output cap cuts with
the notice; the schema refuses a negative limit; a tool process's
environment holds neither the secret nor the token; no golden moves.
**Acceptance**: Phase 3 complete.
**Estimate**: ~400 lines.

### PR 5 — `feature/v040-rfc0009-agent-token`

**Scope**: a new `internal/security/agent_token.go`.
**Implementation**: G-5 and OQ 4 — issue, check (signature, expiry,
revocation) and revoke, with the key from `crypto/rand` at construction.
Constant-time comparison. The key is never logged or served.
**Tests**: issue and check; each field changed is refused; expired and
revoked are refused; a token from another process's key is refused; the key
appears in no log line.
**Migration**: none.
**Estimate**: ~350 lines.

### PR 6 — `feature/v040-rfc0009-enrollment`

**Scope**: `handleRegisterAgent` and `handleDeleteAgent`; the
`SECURITY_ENROLLMENT_SECRET` setting read in `cmd/orchestrator/` beside
`SECURITY_UNQUARANTINE_TOKEN`; `agent.token_issued` and
`agent.token_invalid` given their emitters; the glossary's *agent token* and
*fleet enrollment secret*.
**Implementation**: registration answers with `agent_token` and its expiry
in both modes (OQ 1). A wrong secret is refused with 401 and an audit line;
a missing one is still admitted, so the fleet keeps registering until PR 7
sends it; PR 9 makes it required under `enabled`. Deregistration revokes the
agent's tokens. A re-registration of a live id from another address is
audited (G-14).
**Tests**: registration returns a token in both modes; a wrong secret is
refused and audited; a deregistered agent's token is refused; a
re-registration from a new address writes its audit line.
**Estimate**: ~350 lines.

### PR 7 — `feature/v040-rfc0009-agent-token-client`

**Scope**: `agents/server.py` (490) and `server_reregister.py`; the
enrollment secret read from the environment and sent at registration; the
token kept and sent in `X-Agent-Token`, per request, on the orchestrator
session only, and in `x-agent-token` by `wallet_client.py` (503 — split at a
real seam first, then one line).
**Implementation**: a 401 that names an invalid or expired token triggers
one re-registration and one retry, through the ISSUE-0125 path, so an
orchestrator restart costs the fleet one round trip; a second refusal is not
retried. No route requires the token yet.
**Tests**: the token from registration is sent on every orchestrator call
and never on an `http_request` tool call; an orchestrator restart, seen as a
refused token, re-registers once and the retried call succeeds; a second
refusal is not retried in a loop.
**Estimate**: ~350 lines.

### PR 8 — `feature/v040-rfc0009-token-middleware`

**Scope**: `internal/server/auth_middleware.go` (330) — an agent principal
read from `X-Agent-Token`; `internal/security/middleware.go` — the limiter
and breaker key on it, with an agent's REST calls on a budget apart from its
wallet calls; the publish handler's sender check.
**Implementation**: G-7. Under `enabled`, a valid token makes the request's
principal the agent; an invalid one is refused with 401 and an audit line; a
request carrying both an agent token and an account credential is refused. A
publish whose sender is neither the token's agent nor the account's own
participant is refused. In both modes, a REST call refused by the agent's
REST budget records no violation. No route's policy changes yet.
**Tests**: a forged `X-Agent-ID` beside a valid token is refused and
audited; a token-carrying publish naming another sender is refused; a
signed-in `user` account publishing as an agent is refused, and as itself
admitted; a refused token adds nothing toward the agent's quarantine; a
burst of posts that spends an agent's REST budget leaves its wallet calls
admitted and quarantines nothing, in both modes; under `disabled` nothing
else changes.
**Acceptance**: ISSUE-0187 closed, with its regression test.
**Estimate**: ~450 lines.

### PR 9 — `feature/v040-rfc0009-agent-route-policy`

**Scope**: `internal/server/auth_policy.go` (130) — an agent-route helper
that registers a route with what it admits beside its account policy; the
G-6 table, including any route already merged that waits on A4, such as
RFC 0012 C1 PR 4's organization read; registration requires the enrollment
secret under `enabled`, which with the secret unset refuses every enrollment
and names the setting at startup; the startup warning about the open agent
ingress removed; ISSUE-0117 and ISSUE-0140 closed.
**Implementation**: G-6. Under `enabled` the agent ingress stops being
public. A route bound to the agent the path names refuses another agent's
token. The helper is the one RFC 0012 C1 PR 4 and RFC 0028 PR 13 use.
**Tests**: the v0.4.0 plan's — under `enabled`, each route refuses a forged
id and a caller with no credential it accepts, and admits each one it does;
registration without the secret, with a wrong one or with it unset is
refused; the fleet fetches its roster; a persona recalls its own memory and
not another's.
**Acceptance**: ISSUE-0140 and ISSUE-0117 closed, each with its regression
test.
**Estimate**: ~450 lines.

### PR 10 — `feature/v040-rfc0009-grpc-token`

**Scope**: `internal/security/middleware.go`'s gRPC interceptor and
`cmd/orchestrator/grpcserver.go` (180).
**Implementation**: under `enabled`, the wallet's calls need a valid token
whose agent matches `x-agent-id`, the same rules as PR 8.
**Tests**: a wallet lease with no token, an invalid one or another agent's
is refused under `enabled`; under `disabled` nothing changes; the log stream
stays outside, as its TODO records.
**Estimate**: ~300 lines.

### PR 11 — `feature/v040-rfc0009-auth-mt`

**Scope**: `docs/manual-tests/MT-AUTH-001.md` to v1.1 — its carve-out
probes now expect a credential, and it gains a forged-id probe for an agent
token and for a `user` account; `docs/guides/auth.md`'s "agent ingress"
section rewritten for enrollment and tokens, with the Upgrade Notes 2 text,
the CLI's `channel send` as the signed-in person, and G-14's limit.
**Acceptance**: ISSUE-0152 closed, citing #1050 and PR 8. Under `disabled`,
where no id is verified, anyone who reaches the REST port can still get a
named agent quarantined; the closing note says so.
**Estimate**: ~250 lines.

### PR 12 — `feature/v040-rfc0009-approval-queue`

**Scope**: a new `internal/approvals/` package on A1's society-store
interface; its routes registered from their own file, as
`channel_routes.go` does (`server.go`, at 500 lines, gains one call); the
glossary's *pending approval*.
**Implementation**: G-8, G-9 and G-11. An agent opens an `action` hold on its
own holds route with its token; a hold whose `(actor_id, action_hash)`
already has an open row returns that row (RFC 0028 E-11). Operators list,
read, approve and deny; an agent token never approves. The row records who
decided and when. RFC 0028 E-8's spend is not here.
**Tests**: open, list, approve and deny; a second open for the same hash
returns the first row; an agent token cannot approve or deny; under
`disabled` approve and deny refuse; a row survives an orchestrator restart;
a settled row keeps no text; no transition writes an audit event of the
queue's own.
**Migration**: none — A1 owns the tables.
**Estimate**: ~450 lines.

### PR 13 — `feature/v040-rfc0009-approval-lifecycle`

**Scope**: in `internal/approvals/`: expiry, withdraw, tie groups and the
approve hook.
**Implementation**: an expiry sweep moves every `pending` or `approved` row
past its deadline (24 h by default, §F) to `expired`, so an approval never
spent ends and its retry can open a new hold. The agent withdraws its own
open rows. A tie group's rows are opened together in one call and one
transaction, and nothing joins a group later; approving one row withdraws
the group's `pending` rows in the same transaction, and an approve is
refused once a sibling is approved. A kind may register a hook that runs
inside A1's transaction, with the transaction's own handle, so the change
an approval releases commits with it or not at all; a hook makes no
channel-store call and no network call.
**Tests**: RFC 0012 PR 10a's and PR 11's needs — a tie of three approves one
and withdraws two; two approvals of one group sent at once approve exactly
one; a sibling approved earlier is never withdrawn; an approved row past
its deadline expires and its hash can open again; a failing hook leaves the
row pending and its change unwritten.
**Estimate**: ~450 lines.
**From RFC 0029 Phase 3 PR 2 review**: F-2 — no token spends an approved
`tie` or `graph_change` row, so a sweep that expires every unspent approved
row would later report an accepted tie as expired, and RFC 0012 PR 10a would
refuse its directive. This PR settles how such a row ends: the approve
transaction moves it to `spent`, as RFC 0012 PR 11 calls the approve route
the spender, or the sweep leaves approved rows of those kinds alone. The
table's six states hold either way. A test pins that an approved tie never
expires.

### PR 14 — `feature/v040-rfc0009-approval-event`

**Scope**: `proto/task.proto` — an `ApprovalDecision` message and an
`AgentService` call for it, stubs regenerated with the CI-pinned toolchain;
the Go sender beside the channel dispatcher; a Python handler in
`agents/server_servicers.py` (502 — split at a real seam first) and an
`APPROVAL_DECIDED` event type in `agents/persona_types.py`.
**Implementation**: G-10. The handler reads the row back on its holds route
and records what the queue says; RFC 0028 PR 14 and RFC 0012 PR 10a give it
behaviour.
**Tests**: each decision reaches the agent that opened the row, after the
commit; an agent offline at the decision gets it on re-registration; a
repeat is ignored; a decision the queue does not confirm is ignored; a row
another agent opened is never sent to this one; a `graph_change` row is
never sent and never left waiting as undelivered.
**Estimate**: ~450 lines.
**From RFC 0029 Phase 3 PR 2 review**: F-17 — a row has one `delivered_at`,
so it serves a row that leaves `pending` and later `approved` only if each
transition clears it and an acknowledgement names the state it was sent
for; otherwise an approved row's later expiry is never sent.

### PR 15 — `feature/v040-rfc0009-irreversible`

**Scope**: `irreversible` on `ToolDefinition` and in the `@tool` decorator,
with the argument values that make a call irreversible (G-12); the tags on
`file_write` and `http_request`; `hitl.enabled` in `config/security.yaml`
and `schemas/security.schema.json`, read by `LoadSecurityConfig`.
**Implementation**: G-12 and G-13. No hold acts on the tag here; RFC 0028
PR 11's registry reads it.
**Tests**: the tags and their argument rule; `hitl.enabled` validates and
defaults to `false`; nothing is held.
**Estimate**: ~250 lines.

### PR 16 — `feature/v040-rfc0009-known-gaps`

**Scope**: the issue the v0.4.0 plan's Known Gaps assign to A4 — the
agent-token key changes on every restart — one for the orchestrator's
unauthenticated calls into agents (G-10), and one for the log stream, which
no token guards, so Python audit lines can be forged under any agent's name
(G-4), each filed and named in the Known Gaps; their Upgrade Notes 2 and 3
lines drafted in `[Unreleased]`.
**Estimate**: ~150 lines.

### PR 17 — `feature/v040-rfc0009-closeout`

**Purpose**: the review findings deferred from PRs 1–16, paraphrased by
component; RFC and ROADMAP status to ✅ Implemented, all four phases
shipped; this plan's statuses closed. The RFC splits at a real seam first if
the closeout would take it past its cap.

---

## File-size constraints

| File | Lines now | Plan |
|------|-----------|------|
| `agents/base.py` | 505 | PR 3 adds one call; RFC 0028 PR 6 moves `_execute_tools` out first, and whichever merges second rebases |
| `internal/server/server.go` | 500 | PR 12 registers its routes from their own file |
| `agents/server_servicers.py` | 502 | PR 14 splits at a real seam first |
| `cmd/orchestrator/main.go` | 499 | PR 6 reads its setting beside `cmd/orchestrator/ratelimit.go`, not here |
| `agents/wallet_client.py` | 503 | PR 7 splits it at a real seam first, then adds one metadata entry |
| `internal/server/auth_middleware.go` | 330 | PR 8 adds the agent principal |
| `agents/server.py` | 490 | PR 7 sends the token through `orchestrator_session.py`; the refresh lives in `server_reregister.py` |

## Risks

| Risk | Mitigation |
|------|------------|
| Turning the check on refuses the fleet. | PRs 5–9 in order, so a route requires a credential only after agents send it; PR 9's tests call every route with and without each credential; the live arc runs under `enabled`. |
| An account speaks as a manager. | G-7 binds every publish to its caller (PR 8). |
| An orchestrator restart strands agents with dead tokens. | A refused token re-registers once (PR 7); the Known Gap issue (PR 16) records the restart cost. |
| The enrollment secret is unset in a deployment that enables auth. | Registration fails closed and startup names the setting (PR 9); Upgrade Notes 2. |
| The argument check refuses calls that ran before. | It refuses only what fails today and converts lossless forms (G-2); every golden replays unchanged (PR 3). |
| A held action's text lingers in `data/channels.db`. | It is cleared when the row settles (G-11). |
| A forged decision event moves a tie or releases an action. | The agent confirms every event against the queue (G-10). |

## Notes

> 2026-10-21 — Plan written for row R2. RFC 0009 moves to 🚧 Implementing
> with it, and its amendment records open questions 9 and 10, G-1, G-4 and
> G-5.

## Related documentation

- [RFC 0009 Phases 1–2 plan](0009-pr-plan.md) — what v0.3.0 shipped
- [RFC 0028 PR plan](0028-pr-plan.md) — the hold, the release and the approval tokens this queue serves
- [RFC 0012 PR plan](0012-pr-plan.md) — ties and graph changes on the queue
- [RFC 0029 Phase 3 PR plan](0029-phase3-pr-plan.md) — the society tables the queue lives in
- [v0.4.0 plan](../v0.4.0-plan.md) — rows A3 and A4, the identity lock, the acceptance
- [Release cycle §Phase 1](../methodology/release-cycle.md#phase-1--implementation-prs) · [review process](../methodology/review-process.md)
