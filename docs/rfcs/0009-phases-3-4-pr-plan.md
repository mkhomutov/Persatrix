# RFC 0009 — Agent Identity, Security & Sandboxing — PR Implementation Plan (Phases 3–4)

**RFC**: [0009-security-sandboxing.md](0009-security-sandboxing.md)
**Status**: 🔄 In progress
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

RFC 0009's open questions for these phases, and the design points G-1 to
G-14 that settle them, are in
[their own file](0009-phases-3-4-design-pr-plan.md#phase-0--open-questions-and-design-points),
split out unchanged when this plan reached its word cap. Each PR below names
the points it implements.

---

## Progress Overview

| PR | Row | Title | Branch | Status | GitHub PR | Merged |
|----|-----|-------|--------|--------|-----------|--------|
| 1 | A4 | ISSUE-0152 steps 1–2: one quarantine no longer refuses every anonymous caller | `fix/quarantine-spares-anonymous-callers` | ✅ | [#1050](https://github.com/mkhomutov/Persatrix/pull/1050) | 2026-10-22 |
| 2 | A4 | ISSUE-0152 step 3, agent half: agents send their id on REST | (in PR 1) | ✅ | [#1050](https://github.com/mkhomutov/Persatrix/pull/1050) | 2026-10-22 |
| 3 | A3 | Tool arguments checked against the tool's schema | `feature/v040-rfc0009-arg-schemas` (head branch `claude/affectionate-faraday-sewbto`, assigned by the session that opened it) | ✅ | [#1069](https://github.com/mkhomutov/Persatrix/pull/1069) | 2026-10-29 |
| 4 | A3 | Shell CPU limit, output caps, `resource_limits:` | `feature/v040-rfc0009-resource-limits` | 🔀 | [#1089](https://github.com/mkhomutov/Persatrix/pull/1089) | — |
| 5 | A4 | Agent tokens: issue, check, revoke (Go) | `feature/v040-rfc0009-agent-token` (head branch `claude/admiring-cori-ibird5`, assigned by the session that opened it) | ✅ | [#1071](https://github.com/mkhomutov/Persatrix/pull/1071) | 2026-10-30 |
| 6 | A4 | Registration returns a token and accepts the enrollment secret | `feature/v040-rfc0009-enrollment` (head branch `claude/magical-gates-p9ku7g`, assigned by the session that opened it) | ✅ | [#1076](https://github.com/mkhomutov/Persatrix/pull/1076) | 2026-11-01 |
| 7 | A4 | Agents send the secret, keep their token and refresh it | `feature/v040-rfc0009-agent-token-client` | ✅ | [#1079](https://github.com/mkhomutov/Persatrix/pull/1079) | 2026-11-03 |
| 8 | A4 | Token check on REST; every publish bound to its caller | `feature/v040-rfc0009-token-middleware` | ✅ | [#1085](https://github.com/mkhomutov/Persatrix/pull/1085) | 2026-11-04 |
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

> A PR's own row still says 🔀 when it merges. The next commit made
> through the pre-commit hook flips it; PRs 3, 5 and 6 needed a follow-up PR before that: see
> [ISSUE-0190](../issues/ISSUE-0190-merged-pr-plan-row-reddens-main.md).

## Dependency graph

```
R2 (this plan accepted)
PR 1 (#1050, with PR 2)                            (merged; waited on no row)
PR 3 ──► PR 4                                      ═══ A3 done
PR 3 ──► PR 5 ──► PR 6 ──► PR 7 ──► PR 8 ──► PR 9 ──► PR 10 ──► PR 11
PR 4 ──► PR 9
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

PR 3's implementation note and its review findings are in
[the plan's notes on merged PRs](0009-phases-3-4-notes-pr-plan.md#pr-3--featurev040-rfc0009-arg-schemas).

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
**From PR 7 review**: F-16 — agents now read the fleet enrollment secret from
their environment, and `shell_exec` inherits that environment until this PR
merges, so this PR comes before PR 9 makes the secret required. The token is held in memory only, so it has nothing to scrub. A
scrub stops short of G-14's words on Linux: a tool running as the agent's
user can read the secret from `/proc/<agent pid>/environ`, which keeps the
environment the agent started with. This PR or PR 11's guide says so.
**Implementation note (2026-11-04)**: how this PR meets the scope above.
Each point not settled above is a new default this PR's review confirms or
overturns.

- **The block.** `resource_limits` sits beside `permissions` in an agent's
  entry, with `shell_cpu_seconds` (1 to 300, the wall-time ceiling) and
  `tool_output_bytes` (1 024, room for the notice, to 102 400, the built-in
  tools' own cut, which it can lower and never raise). An empty block or key
  sets no limit, and `10.0` reads as 10, as the schema reads them.
  `load_agent` reads it before the agent is built, so a value the schema
  refuses stops the agent at startup rather than being dropped (new
  default), and `builtin.configure` installs it with the permissions once
  the agent is built, so a load that fails leaves nothing behind. That
  wiring moved out of `server_persona.py`, which keeps it at 500 lines.
  `config/agents.yaml` shows the block commented out with §D's numbers, so
  the shipped agents behave as before; a test keeps the example valid.
- **The CPU limit.** `RLIMIT_CPU`, set in the command's own process before
  it starts, and only when a limit is set, so a command with none starts
  exactly as before. A fresh interpreter (`python -I -S -c`) sets the limits
  and then becomes the command (`os.execv`): Python run in a child forked
  from the agent, whose threads make that unsafe, could hang it
  (`preexec_fn`). The soft limit is the setting and the hard one a second
  more, never above a lower hard limit already in force, such as a
  container's, with the soft limit then a second under it, since Linux
  checks the hard limit first; a lower soft limit already in force stands.
  SIGXCPU comes at the limit; on Linux SIGKILL
  follows a second later to a command that ignores it, while macOS sends
  none and such a command runs to its wall time. The shim also sets the
  core-file limit to 0, since SIGXCPU writes one by default. The limit holds
  for each process the command starts, on its own, so a command that starts
  others can spend more in all, up to its wall time, which stops its whole
  process group: each command runs in a session of its own. Only SIGXCPU to
  the command's own process reads as the limit: the call fails with
  `ResourceLimitError`, naming the soft limit applied, and keeps the
  command's output (new default).
- **The output cap.** Applied in the `@tool` wrapper to every tool's
  result, a failed one's too, measured as the model is shown it (JSON for a
  dict or a list), the notice included. A text is cut with `_truncate`'s
  notice, naming the cap; `_truncate` builds the same notice, so the 100 KB
  notice and every golden stay as they were. A dict or a list keeps its
  shape: each of its texts is cut to one common length, the longest that
  fits, so a long stdout keeps `shell_exec`'s stderr and exit code (new
  default); a text no longer than that length and the notice stays whole.
  One with too many fields for that is cut as text. A failed call's error
  text is held to the same cap on its own; the tool round's error label and
  data envelope come on top. Both tool rounds render through the cap's own
  helper and reuse the rendering it measured. An output too deep to render
  is passed on as it is.
- **The audit line.** `tool.resource_limited`, on `agents.tools.audit` as
  G-4 says, names the agent, the tool, the limit (`shell_cpu_seconds`,
  `max_execution_seconds` or `tool_output_bytes`) and its value, and for the
  cap the size before the cut. A command stopped at the agent's own
  wall-time limit writes one too. A shorter timeout the model chose, and the
  built-in tools' own 100 KB cut, write none: each is the tool's behaviour,
  not a limit an operator set.
- **G-14.** `shell_exec` starts its command with `tool_process_env()`, the
  agent's environment less every variable whose name starts with
  `SECURITY_`, in any letter case; a test ties that prefix to the secret's
  name. At startup the agent also moves the secret from its environment
  into memory, so no process it starts by any means inherits it. The token
  is held in memory only, and a test shows keeping it puts nothing in the
  environment. Two things still reach a command, and PR 11's guide says so: on Linux
  a command running as the agent's user can read the secret from
  `/proc/<agent pid>/environ` (PR 7's F-16), and every other variable,
  provider API keys included, still reaches it, since an allowlisted command
  may need one. PR 16 files both.

**Review findings** (PR 4): F-1 to F-8 are an independent review's, each
proved by a run or by reading the code, F-9 to F-11 the author's, and F-12
to F-23 a second independent review's, after the rebase.

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| F-1 | Medium | `RLIMIT_CPU` holds each process on its own, so `shell_cpu_seconds` did not cap a command's CPU time in all, as the schema, CHANGELOG and glossary said: a command that starts children gives each a fresh limit, and a child stopped at it is reported nowhere | Docs fixed in-PR: each now says the limit holds per process, the total is bounded by wall time and the container, and only the command's own process is reported |
| F-2 | Low | Under an inherited hard limit at or below the setting, soft and hard were equal, and Linux checks the hard limit first, so the command died with SIGKILL and no audit line. macOS never sends SIGKILL, so a command that ignores SIGXCPU runs to its wall time | Fixed in-PR: the soft limit sits a second under a lower hard limit, with a test for each case. The ignored-SIGXCPU kill stays an ordinary exit; the docstrings and this note say so |
| F-3 | Low | SIGXCPU dumps core by default, so with an unlimited core limit each stop wrote a core file into the agent's workspace | Fixed in-PR: the child sets its core-file limit to 0; the test raises the agent's own first |
| F-4 | Low | A structured output fell back to cut text when cutting every text to nothing did not fit, though cutting a short text lengthens it by the notice, so a longer common length could still fit | Fixed in-PR: a text is cut only where that shortens it, so the search finds the longest length that fits; a test with many short texts beside a long one |
| F-5 | Low | The text and fallback cuts added the notice after cutting to the cap, so the model got up to 30 bytes more than the cap | Fixed in-PR: the notice counts toward the cap, and the smallest cap is 1 024 bytes, in the schema and the code |
| F-6 | Low | The schema and the runtime read two blocks differently: `10.0` passed the schema and stopped the agent, and a null block or key, what YAML gives for a header uncommented alone, failed the schema and ran | Fixed in-PR: both take a null block or key as no limit, and `10.0` as 10; parity tests for each |
| F-7 | Info | A command stopped at the timeout the model chose wrote the wall-time audit line, even with no limit set, as if an operator had set it | Fixed in-PR: only the agent's own `max_execution_seconds` stopping a command writes one, under that name |
| F-8 | Low | Three places left stale: ROADMAP's sandbox row called both limiters unbuilt, the audit-record module named two writers, and `shell_exec` and `http_request` still tell the model their cut is 100 KB when a lower cap applies | The first two fixed in-PR. The tool descriptions → Noted: changing them re-records every golden, and the cut's notice names the cap |
| F-9 | Low | A block with a number key beside a name raised `TypeError` sorting the unknown keys, not the `ValueError` naming them | Fixed in-PR, with a test |
| F-10 | Info | +1 051 lines at review, 556 of them tests, past the ~400 estimate | Noted |
| F-11 | Info | The fixes took this plan to 7 973 of its 8 000 words, and RFC 0009 stood at 7 686 before this PR | Fixed in-PR for the plan: PR 8's merged note moved, unchanged, to a second notes file. The RFC → Noted: this PR's own note is short, and the next note splits the RFC |
| F-12 | Medium | The CPU limit was set through `preexec_fn` in the threaded agent, so every at-fork hook ran in the child (OTEL starts threads there), and the event loop waits for that child to start the command, so a child that hung there hung the agent | Fixed in-PR: a fresh interpreter sets the limits and becomes the command; a test shows no Python runs in a forked child |
| F-13 | Medium | A command out of time was stopped alone: a process it started ran on, so the wall time bounded nothing it started, as the docs said | Fixed in-PR: each command runs in a session of its own, and its whole process group is stopped; a test shows a started process does not outlive the limit |
| F-14 | Low | Three docstrings said a command cannot read the enrollment secret, against this note's `/proc/<agent pid>/environ` gap | Fixed in-PR: each names the gap |
| F-15 | Low | The soft limit ignored a lower soft limit already in force and could raise it | Fixed in-PR: a lower one stands, with a test |
| F-16 | Low | Under a lower hard limit the error and audit line named the setting, not the sooner limit applied | Fixed in-PR: both name the soft limit applied, with a test |
| F-17 | Low | The cap left a failed call's error text uncut, and the schema left out the tool round's error label and envelope | Fixed in-PR: the error text is held to the cap, with a test; the schema says what comes on top |
| F-18 | Low | A cap that raised, as on output nested too deep to render, turned a call that ran into a failure, recorded twice | Fixed in-PR: such an output passes as it is, a failed structured cut falls back to the text cut, and the call is recorded once; tests for both |
| F-19 | Low | The limits were installed before the rest of the load, so a load that failed later left them behind | Fixed in-PR: read first, installed last, with a test |
| F-20 | Low | The cap copied the tool rounds' rendering rule, so the two could drift | Fixed in-PR: both rounds render through the cap's helper |
| F-21 | Low | Only `shell_exec` kept the secret from its command; any other process the agent started inherited it | Fixed in-PR: the agent moves the secret into memory at startup, with a test |
| F-22 | Low | With a cap set, each output was rendered once to measure and again by the tool round | Fixed in-PR: the round reuses the rendering, with a test |
| F-23 | Info | The agent-token test passed with the scrub removed, so it checked nothing of the scrub | Fixed in-PR: it now checks what it can, that keeping the token puts nothing in the environment |

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

PR 5's implementation note and its review findings are in
[the plan's notes on merged PRs](0009-phases-3-4-notes-pr-plan.md#pr-5--featurev040-rfc0009-agent-token).

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
**From PR 5**: registration's ID pattern (`planner.ResourceIDRegex`) has no
length limit, and `Issue` refuses an ID over 256 bytes, so this PR refuses
such a registration with 400 before it registers anything. `Revoke` keeps
nothing for an agent with no working token, so deregistration may call it
for any ID it removes.
**From PR 5 review**: F-6 — this PR serializes one agent's registration and
deregistration, so a racing pair cannot leave the agent registered with a
token already revoked.
F-11 — `Issue` refuses `web-console` and `anonymous`, so registration
refuses them with 400 too. F-12 — a registration retires the agent's
earlier tokens, so the audit line for a re-registration from a new address
(G-14) can say the old address's token stopped working.

PR 6's implementation note and its review findings are in
[the plan's notes on merged PRs](0009-phases-3-4-notes-pr-plan.md#pr-6--featurev040-rfc0009-enrollment).

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
**From PR 6**: the agent sends `X-Enrollment-Secret` on registration and
reads `agent_token` and `agent_token_expires_at` from the `201` body.
Reading the secret from the same `SECURITY_ENROLLMENT_SECRET` the
orchestrator reads keeps one name per deployment; PR 4's scrub of a tool's
environment removes it.
**From PR 5 review**: F-12 — a new token retires the agent's earlier
ones, so the client registers again once for all the calls in flight, and
a call refused as revoked while the client already holds a newer token
retries with that one. F-13 — a client told its token was revoked stops and
does not register again by itself: the issuer reports a revocation only
while it holds the agent, and later the same token reads as expired.

PR 7's implementation note and its review findings are in
[the plan's notes on merged PRs](0009-phases-3-4-notes-pr-plan.md#pr-7--featurev040-rfc0009-agent-token-client).

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
**From PR 6 review**: F-9 — registration admits the ID `local`, which is
`channels.DefaultPrincipalID`, so an agent can hold a token naming it. This
PR either refuses that ID, as `IssuableAgentID` refuses `web-console` and
`anonymous`, or shows by a test that such a token gets none of the default
principal's standing.
**From PR 7 review**: the client reads a refusal from the `code` of a 401 in the
usual error body: `AGENT_TOKEN_INVALID`, `AGENT_TOKEN_EXPIRED` or
`AGENT_TOKEN_REVOKED`, one for each of `Check`'s refusals, with any other
token refused, a token naming another agent than `X-Agent-ID` among them, as
invalid. So this PR answers with those three, and names them where the
agents' constants in `agents/orchestrator_session.py` can be checked against
them. A call is refused before it acts, since the client sends it again.
Registration stays outside the check: it shows no token, by design. F-15 —
an agent deregisters before its gRPC server's shutdown grace, which revokes its
token, so a task finishing inside that grace can no longer publish once this
PR checks the token. This PR settles that one way or the other: deregistration
moved after the grace, or the lost publish accepted and said. F-4 — "revoked"
covers both a token an operator revoked and one a later registration replaced,
so the client has to guess, and a late registration can stop a healthy agent
until it restarts. This PR weighs giving a replaced token a code of its own,
which ends the guess, against two processes on one ID then fighting over it
until the limiter stops them.

PR 8's implementation note and its review findings are in
[the plan's notes on merged PRs, part 2](0009-phases-3-4-notes-2-pr-plan.md#pr-8--featurev040-rfc0009-token-middleware).

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
**From PR 6 review**: F-10 — no compose file sets `env_file` or any
`SECURITY_*` variable, so `SECURITY_ENROLLMENT_SECRET` in `.env` reaches
neither the orchestrator nor the agents, and `.env.example` does not list
it. This PR lists it there and passes it through in every
`docker-compose*.yaml`; otherwise, once it requires the secret, a composed
stack under `enabled` refuses every registration.
**From PR 7 review**: F-5 — an agent idle past 24 hours deregisters on an
expired token and cannot replace it, so a route that admits only a live token
leaves a healthy row for a dead agent. This PR admits an expired token on that
one route, or says what clears the row. F-18 — a call that shows no token is
not retried, so under `enabled` an agent whose first registration was refused
has every call refused until a reconnect or a restart. This PR says so in the
refusal's log line, or has such a refusal register once.
**From PR 8 review**: F-1 — until this PR anyone can take a live agent's ID:
registration admits a caller with no secret, and deregistration is open.
Requiring the secret closes the first; this PR also binds deregistration to
the agent's own token or an operator. F-5 — since PR 8 such an agent's
publishes get 403 `sender_not_caller`, which is no token refusal, so F-18's
harm starts there. F-6 — this PR refuses to register an ID that an
account's participant holds. F-11 — once the ingress is gated, those two
comments stop calling the sender unchecked.

### PR 10 — `feature/v040-rfc0009-grpc-token`

**Scope**: `internal/security/middleware.go`'s gRPC interceptor and
`cmd/orchestrator/grpcserver.go` (180).
**Implementation**: under `enabled`, the wallet's calls need a valid token
whose agent matches `x-agent-id`, the same rules as PR 8.
**Tests**: a wallet lease with no token, an invalid one or another agent's
is refused under `enabled`; under `disabled` nothing changes; the log stream
stays outside, as its TODO records.
**Estimate**: ~300 lines.
**From PR 7 review**: F-14 — `WalletClient` shows the token but does not act
on a refusal, since only REST calls are refused so far. A wallet call refused
for its token would reach `_acquire`'s "anything else" branch and fail the model call, and
a persona that wakes after 24 hours quiet calls the wallet first. So this PR
settles the gRPC refusal's shape and gives the wallet the REST rule: ask
`AgentTokenKeeper.after_refusal` and retry once. F-15 — `AgentServer.stop`
deregisters, which revokes the token, before the agent's last wallet calls:
`close_memory(write_open=True)` writes the summaries of open conversations,
and each takes a lease. This PR moves the deregistration or exempts them.

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
**From PR 4**: G-14's limit is two gaps. On Linux a command `shell_exec`
runs as the agent's user can read the enrollment secret from
`/proc/<agent pid>/environ`, which the scrub cannot change; running tools as
another user, or allowlisting no command that reads files outside the
workspace, closes it. And only `SECURITY_*` variables are scrubbed, so a
command still sees the provider API keys.

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
**From PR 8 review**: F-4 — ISSUE-0194, posts into a channel quarantining
its personas through their wallet bids, goes in the Known Gaps unless an
amendment slots it.
**From PR 4**: the two G-14 gaps PR 11's guide states, the secret readable
in `/proc/<agent pid>/environ` and the provider API keys in a tool's
environment, each filed and named in the Known Gaps.

### PR 17 — `feature/v040-rfc0009-closeout`

**Purpose**: the review findings deferred from PRs 1–16, paraphrased by
component; RFC and ROADMAP status to ✅ Implemented, all four phases
shipped; this plan's statuses closed. The RFC splits at a real seam first if
the closeout would take it past its cap.

---

## File-size constraints

| File | Lines now | Plan |
|------|-----------|------|
| `agents/base.py` | 412 | PR 3 moved the task agent's tool round, unchanged, to `agents/task_tool_round.py` (121 lines) |
| `internal/server/server.go` | 500 | PR 12 registers its routes from their own file |
| `agents/server_servicers.py` | 502 | PR 14 splits at a real seam first |
| `cmd/orchestrator/main.go` | 499 | PR 6 read its setting in `cmd/orchestrator/enrollment.go`, which took the unquarantine wiring with it; still 499 after |
| `agents/wallet_client.py` | 430 | PR 7 moved `Lease` and `BudgetExceededError` to `agents/wallet_lease.py`, then read the token on each call |
| `internal/server/auth_middleware.go` | 330 | PR 8 adds the agent principal |
| `agents/server.py` | 500 | PR 7 sent the token through `orchestrator_session.py`, and the refresh lives in `server_reregister.py`; the next PR to add a line splits registration out first |

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
- [Notes on merged PRs](0009-phases-3-4-notes-pr-plan.md) — this plan's merged PRs' implementation notes, PRs 3 and 5 to 7, and review findings
- [Notes on merged PRs, part 2](0009-phases-3-4-notes-2-pr-plan.md) — the same from PR 8 on
- [RFC 0028 PR plan](0028-pr-plan.md) — the hold, the release and the approval tokens this queue serves
- [RFC 0012 PR plan](0012-pr-plan.md) — ties and graph changes on the queue
- [RFC 0029 Phase 3 PR plan](0029-phase3-pr-plan.md) — the society tables the queue lives in
- [v0.4.0 plan](../v0.4.0-plan.md) — rows A3 and A4, the identity lock, the acceptance
- [Release cycle §Phase 1](../methodology/release-cycle.md#phase-1--implementation-prs) · [review process](../methodology/review-process.md)
