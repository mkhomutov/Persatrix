# RFC 0009 — PR Implementation Plan (Phases 3–4): open questions and design points

**Plan**: [RFC 0009 Phases 3–4 PR plan](0009-phases-3-4-pr-plan.md)
**RFC**: [0009-security-sandboxing.md](0009-security-sandboxing.md)
**Split out**: 2026-10-29, unchanged, from the plan, when PR 3's review
findings took the plan past its 8 000-word cap. The design points G-1 to
G-14 keep their numbers; the plan's PRs name the points they implement.

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
  PR 3. *Revision (PR 3):* how the check reads arguments, and its one
  exception to "refuses only what fails today", are in
  [PR 3's implementation note](0009-phases-3-4-pr-plan.md#pr-3--featurev040-rfc0009-arg-schemas).
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
