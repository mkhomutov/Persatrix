# RFC 0009 — PR Implementation Plan (Phases 3–4): notes on merged PRs

**Plan**: [RFC 0009 Phases 3–4 PR plan](0009-phases-3-4-pr-plan.md)
**RFC**: [0009-security-sandboxing.md](0009-security-sandboxing.md)
**Split out**: 2026-10-30, unchanged, from the plan, when PR 5's second
review took the plan toward its 8 000-word cap. Each merged PR's
implementation note and review findings move here; its scope, tests and
estimate stay in the plan.

---

## PR 3 — `feature/v040-rfc0009-arg-schemas`

Merged as [#1069](https://github.com/mkhomutov/Persatrix/pull/1069) on
2026-10-29.

**Implementation note (2026-10-29)**: how G-2 is met, each point a new
default this PR's review confirms or overturns.

- **Where.** The check is `agents/tools/arg_check.py`, not `registry.py`,
  which keeps room for PR 4's output cap and PR 15's tag; `registry.py`
  reads each tool's signature once into an `ArgumentSpec`.
  `tool_list.admit_call` replaces the lookup at both call sites, and
  RFC 0028 PR 10's seat goes between the admission and the run, on the
  checked arguments. Since the PR edits `base.py` at 505 lines, it first
  moves the task agent's tool round (`_build_tool_definitions`,
  `_execute_tools`) unchanged into `agents/task_tool_round.py`, the seam
  RFC 0028 PR 6 named, as personas have `tool_round.py`. A check that
  itself fails refuses the call: running it unchecked would give the seat a
  value nobody checked.
- **Against what.** Missing and unknown arguments are read off the
  signature, not the generated schema, which admits unknown keys; a tool
  with `**kwargs` takes any name. The schema no longer offers `*args` or
  `**kwargs` as a required string: a call binds by name alone, so the model
  was told to send what the check refuses. No built-in has either, so no
  golden changes. `jsonschema` checks each parameter's narrowing, and only
  on a tool that narrows one.
- **Type.** A declared type is enforced only on a parameter the tool
  narrows (`@tool(narrow=...)`), since an off-type value runs today on five
  of the nine built-ins: a `shell_exec` timeout of 5.5, a falsy `tags`, a
  `recall_notes` limit of `true`. A narrowing is added to the offered
  schema, and a narrowed parameter is checked against that whole schema, its
  declared type included, so the model is told what is checked; a narrowed
  number must also be finite. A narrowed parameter's null is checked rather
  than replaced by its default, so a null that failed inside the tool is
  refused, unless the annotation takes `None`, when it runs as before. The
  keywords are the ones every provider takes, Gemini's subset being the
  narrowest; a narrowing that cannot work fails when the tool is defined: a
  keyword on a type it does not limit, an empty enum, a bound that is not
  finite, a minimum over its maximum. The built-ins narrow eleven string
  parameters where every off-type value fails today, which a test pins by
  running each tool without the check; these restate the type, so no offered
  schema or golden changes.
- **One form.** An integer parameter given canonical integer text (`"30"`,
  never `" 30"` or `"030"`) or a whole float gets the int; a bool, a
  fraction, NaN and infinity pass unchanged, so RFC 0028 PR 10's hash must
  handle those itself. A number parameter given an int or integer text the
  float holds exactly, or decimal text that reads back the same (`"0.5"`,
  never `"0.50"` or `"1e5"`), gets the float, so Anthropic's `1` and
  Gemini's `1.0` are one call; a bool passes unchanged. A null for an
  optional parameter means its default, unless the annotation takes `None`
  or the tool narrows the parameter. Defaults are not filled in, and text
  passes exactly as sent (`http_request` upper-cases its own method), so
  PR 10's hash binds defaults itself, and PR 15's rule for a writing method
  compares as the tool does.
- **One exception to "fails today".** On Python 3.11 a null `shell_exec`
  command read the agent's standard input; its narrowing refuses it.
- **Refusal and audit.** The model gets JSON with
  `tool_argument_validation_error` and lists of the missing, unknown and
  invalid arguments; the audit line is INFO on `agents.tools.audit`,
  written through the helper the fact tier's audit uses
  (`agents/observability/audit_record.py`). Neither carries a value, and the
  audit line counts the unknown names instead of listing them, since the
  model chooses a name as freely as a value; the reply passes each through
  `bounded`, the one log-injection defence. A refused call is recorded as a
  failed tool call, an `agent.tool.execute` span with an error status and
  `agent.tool.invocations` with `tool.success=false`, as the wrapper
  recorded the `TypeError` it replaces.

**Review findings** (PR 3):

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| F-1 | Low | A narrowing was checked on its own, while the model was offered it merged with the declared type, so `{"minimum": 1}` admitted `false`, which a tool reads as 0 | Fixed in-PR: a narrowed parameter is checked against its whole offered schema |
| F-2 | Low | A numeric bound let NaN through, since NaN compares false with every bound, and Python's `json` reads NaN in a model's arguments | Fixed in-PR: a narrowed number must be finite |
| F-3 | Low | Four allowed keywords (`const`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`) and an enum of numbers make Gemini refuse every request that offers the tool; a fractional `multipleOf` made the check raise on NaN, infinity or a huge integer; `pattern` runs as Python's `re.search`, where `$` matches before a final newline | Fixed in-PR: the keywords are `type`, `enum` (text only), `minimum`, `maximum`, `minLength` and `maxLength`; a test pins the set, and another checks them against Gemini's SDK where it is installed |
| F-4 | Low | A null was replaced by the default for an annotation that takes `None` but is a `Literal` or a type alias | Fixed in-PR |
| F-5 | Low | No test covered `Optional`, `Union`, a `Literal`, an alias or annotations that cannot be resolved | Fixed in-PR: one case each |
| F-6 | Low | An unknown name was escaped only for lone surrogates, so control and right-to-left characters reached the audit line raw, and the one test of it could not fail | Fixed in-PR: every character that does not print is escaped, and the test runs the audit fields through the log shipper's encoder |
| F-7 | Low | The audit line's leak check read only some of its fields | Fixed in-PR: every field the record carries |
| F-8 | Low | No test covered the audit line's redactor or its fallback, though a redactor that raised past it would end the agent's turn | Fixed in-PR: two tests |
| F-9 | Low | The binding test checked only refused or admitted, so a `**kwargs` tool could lose its extra arguments unseen | Fixed in-PR: an admitted call keeps every argument |
| F-10 | Info | A 5 000-digit case passed only under the interpreter's default digit limit | Fixed in-PR: its own test sets the limit |
| F-11 | Info | The fail-closed test did not check the operator's log line, the only record of why the call did not run | Fixed in-PR |
| F-12 | Info | A refusal read "at least 1 characters" | Fixed in-PR |
| F-13 | Info | The CHANGELOG said arguments that are not an object failed inside the tool (they failed before it, as an internal error), that every argument is named (16 unknown names are), stated the null rule without its exception, and read as a full list of the narrowed arguments | Fixed in-PR |
| F-14 | Low | New comments said "RFC 0009 PR 3", which already names the Phases 1–2 plan's input sanitizer | Fixed in-PR: "RFC 0009 Phase 3" |
| F-15 | Low | The comments stating the rule "narrow only what fails today" left out its one exception, the null `shell_exec` command | Fixed in-PR |
| F-16 | Info | The `argument_spec` comment left out the positional-only case | Fixed in-PR |
| F-17 | Info | `tool_list.py` described the pre-tool seat as already there | Fixed in-PR |
| F-18 | Info | The glossary's *Narrowing* did not set itself apart from the recall tool's narrowing parameters | Fixed in-PR |
| F-19 | Info | The Go audit registry gave no pointer that the Python runtime now writes `tool.arg_invalid` | Fixed in-PR: a comment, as `agent.deliberated` has |
| F-20 | Low | RFC 0009 had no dated note on how PR 3 meets its amendment, and G-2 no pointer to the exception | Fixed in-PR |
| F-21 | Info | The note to RFC 0028 did not say defaults are not filled in, nor that text passes as sent | Fixed in-PR |
| F-22 | Info | The pin test sends no falsy value of another type | Noted: verification found each fails through the same branch as one the test sends |
| F-23 | Medium | A null for an optional narrowed parameter was replaced by its default before the narrowing ran, so `http_request` with a null method, which failed inside the tool, sent a live GET; the pin test ran the tools without the check only | Fixed in-PR: a narrowed parameter's null is checked, and a test runs the check on every off-type value of every narrowed built-in |
| F-24 | Low | A narrowed parameter annotated to take `None` refused a null the tool ran on before | Fixed in-PR: such a null passes the narrowing unchecked |
| F-25 | Low | The generated schema still offered `*args` and `**kwargs` as required strings, so the model was told to send what the check refuses | Fixed in-PR: neither is offered; no built-in has either, so no golden changes |
| F-26 | Low | A narrowing that cannot work passed the definition-time check: a NaN or infinite bound (which also breaks the request's JSON), an enum that is not a list (a `TypeError`) or is empty, a length on a number, a bound on text | Fixed in-PR: each raises `ValueError`, and so does a minimum over its maximum |
| F-27 | Low | Only integer parameters had one form: a number parameter kept `1` and `1.0`, and `"0.5"` and `0.5`, apart, so RFC 0028's hash would see two calls | Fixed in-PR: a number parameter gets the float where the conversion is lossless |
| F-28 | Low | A refused call recorded no tool span or metric, though the `TypeError` it replaces did, so a tool failure rate stopped counting it | Fixed in-PR: a failed `agent.tool.execute` span and `agent.tool.invocations{tool.success=false}`, also for a call refused because the check itself failed |
| F-29 | Low | Unknown argument names, which the model chooses, went verbatim into the audit line, so a name could carry a secret into the log stream | Fixed in-PR: the audit line counts them; the model's reply, which repeats what the model sent, still names them |
| F-30 | Low | The tool audit helper copied the fact tier's redact-then-log body without its re-entry guard | Fixed in-PR: one helper, `agents/observability/audit_record.py`, guard included, writes both |
| F-31 | Info | Unknown names had an escaper of their own beside `agents/sub_agents/_log_safety.bounded`, the single log-injection defence | Fixed in-PR: `bounded` replaces every character that does not print, and the check calls it |
| F-32 | Info | The narrowing pass ran on every call, even for a tool with no narrowing, and `ArgumentSpec.narrowed` was read only by tests | Fixed in-PR: the pass runs only over narrowed parameters |
| F-33 | Info | The wording for a broken keyword was split between a table and inline branches, with a fallback that could not run | Fixed in-PR: one table for all six keywords, their types included |
| F-34 | Low | The PR edited `agents/base.py` at 505 lines without splitting it, against the size rule | Fixed in-PR: the task agent's tool round moved unchanged to `agents/task_tool_round.py`, RFC 0028 PR 6's seam; `base.py` is 412 lines |
| F-35 | Info | The PR description ended with an assistant attribution line, which the repository's rules forbid in PRs and commits | Fixed in-PR: removed from the description |
| F-36 | Info | The head branch is not the planned `feature/v040-rfc0009-arg-schemas` | Noted: the session that opened the PR assigned its head branch, which a PR cannot change; the row records the planned name, as for #941 and #1063, and the squash commit carries no branch name |

## PR 5 — `feature/v040-rfc0009-agent-token`

Merged as [#1071](https://github.com/mkhomutov/Persatrix/pull/1071) on
2026-10-30.

**Implementation note (2026-10-29, revised 2026-10-30 after the second
review)**: how G-5 and OQ 4 are met, each point a new default this PR's
review confirms or overturns.

- **What.** `AgentTokenIssuer` in `internal/security/agent_token.go`, with
  `Issue`, `Check` and `Revoke`, and its key from `crypto/rand` when
  `NewAgentTokenIssuer()` makes it. Nothing calls it yet: PR 6 makes one at
  startup. The constructor takes no clock, so no caller can hand it one
  without time.Now's monotonic reading (F-10); a later PR that must fake
  the time from another package adds a constructor taking both clocks'
  readings, never a single `Now`. The glossary's *agent token* moves here
  from PR 6, since this PR puts the term in code; *fleet enrollment secret*
  stays with PR 6.
- **Format.** `agt1.<fields>.<signature>`, both parts base64url without
  padding. The fields are four 8-byte numbers — the issue time and the
  expiry by the time of day, the expiry by the issuer's running clock, and
  the serial number — then the agent ID; the signature is HMAC-SHA256 over
  the text before it, prefix included. G-5's nonce is the serial number: a
  number used once in a process, which also orders a token against
  revocations (F-20). The prefix names the format and makes a leaked token
  easy to find, so a redaction pattern can key on it later. A token is at
  most 433 characters, and `Check` refuses a longer one before any work.
- **Check order.** The signature part is decoded strictly and compared, in
  constant time, with the signature of the text exactly as sent, so no
  other spelling of a token checks: the last character of a base64 part has
  spare bits, and a decoder skips line breaks, so a signature part holding
  one decodes short. Only then are the fields read, then revocation, then
  expiry, so a revoked token reads as revoked while the issuer holds its
  agent (F-13). The three refusals stay apart — invalid (garbled, changed,
  or another process's key), revoked, expired — for PR 6's audit line and
  PR 7's retry rule; what a caller is told is PR 8's choice. A check that
  passes makes one allocation, the agent ID it returns (F-19).
- **Time.** Two clocks: the time of day, and the time the issuer has been
  running, from time.Now's monotonic reading. A token stops when 24 hours
  have passed on either: at its `ExpiresAt`, by the time of day, which
  counts the hours a machine sleeps (F-14), or 24 running hours after it
  was issued, which no time sync moves (F-10). So no step of the clock and
  no sleep makes a token last longer. A step forward ends tokens early, and
  their agents register again; stepped back again, the clock can let such
  a token work until its 24 running hours end. `IssuedAt` and `ExpiresAt`
  are times of day at issue, as an audit line's are, so an orchestrator
  started before its first time sync dates only the tokens issued before
  the sync wrongly, and the sync's step forward ends them (F-15). `Check`
  reads both clocks with the agent's revocation, under the issuer's lock
  (F-1).
- **Revocation (OQ 4).** By serial number, not time (F-20). For each agent
  whose latest token may still work, the issuer keeps the serial number up
  to which its tokens are refused, and that token's expiry by the running
  clock. `Issue` refuses every earlier token the agent holds, so registering
  again shuts out a copied one (F-12), and `Revoke` refuses the latest too;
  a token issued after either works, so a deregistered agent can register
  again. This is one mark per agent, not RFC 0009's set keyed by
  `(AgentID, IssuedAt)`: no single token can be revoked alone (F-22).
  Revoking an agent with no working token keeps nothing. Once the map has
  doubled, `Issue` forgets each agent whose latest token has expired by the
  running clock, so the map stays within about twice the agents with a
  working token (F-3). That clock never goes back, so an unexpired token
  for an agent the issuer does not know was not issued here: it is refused
  as invalid.
- **ID.** `Issue` refuses an empty ID, any ID the REST and gRPC hooks
  refuse as an `X-Agent-ID`, and the two the security layer gives callers
  that are not agents: `web-console` and `anonymous` (F-11).
- **Key.** It lives only in the function that makes the issuer's HMACs and
  in the HMACs, kept in a pool, and no method returns it, so printing or
  encoding the issuer, or a value holding it, shows no key byte, even to a
  printer that follows pointers. The test looks for the key in seven forms
  in a JSON log line, `fmt`'s output, JSON, a testify diff (F-16), the
  errors and a token.

**Review findings** (PR 5; F-10 to F-24 from a second review of the fixed
PR):

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| F-1 | Medium | Expiry and forgetting read the wall clock. Once a revocation was forgotten, a clock stepped back could bring the revoked token back; a forward jump and its correction dated every later token ahead and stretched its life without bound; and `Check` read the clock outside its lock, so forgetting could fall between the clock and the lookup | Fixed in-PR, and reworked by F-10 and F-20: a token stops at 24 hours on the running clock, which never goes back, revocation goes by serial number, and `Check` reads both clocks with the revocation under its lock; tests for the step back and the lock |
| F-2 | Low | The over-length case could not fail: its padded token was refused by its signature anyway, so nothing showed the length is checked first | Fixed in-PR: a lengthened real token is refused with no allocation |
| F-3 | Low | `Revoke` kept an entry for any ID, even one never issued a token, and every call added one, so the list grew with revocations, not agents | Fixed in-PR: one entry per agent with a working token, holding its revocation; revoking an agent with none keeps nothing; `Issue` forgets expired agents once the map has doubled |
| F-4 | Low | The glossary's *Approval Token* still called a capability token RFC 0009's, which the new *Agent Token* entry disallows; the ID rule left out that `Issue` also refuses an empty ID, which the hooks admit as anonymous | Fixed in-PR |
| F-5 | Info | `AgentToken` named what a token says, not the token the glossary defines | Fixed in-PR: renamed `AgentTokenClaims` before PRs 6–10 use it |
| F-6 | Info | A registration and a deregistration of one agent that race can leave it registered with a revoked token, which PR 7's retry rule, for invalid and expired tokens, would leave refused until it expires | → deferred to PR 6 (noted there) |
| F-7 | Info | The PR is over the ~350-line estimate and the 500-line target, most of it tests | Noted: 376 lines of code after the second review; the tests that prove it are not split from it |
| F-8 | Info | The head branch is not the planned `feature/v040-rfc0009-agent-token` | Noted: the session that opened the PR assigned it; the row records the planned name, as for PR 3 |
| F-9 | Info | The PR description ended with an assistant attribution line, added when the PR was opened, which the repository's rules forbid in PRs and commits | Fixed in-PR: removed from the description |
| F-10 | Medium | The F-1 fix worked only if the injected `Now` kept time.Now's monotonic reading, which nothing said or checked; `time.Now().UTC()`, common in this repository, strips it, so a token issued after a corrected forward jump still passed long after its 24 hours, and an expired one passed again after a step back | Fixed in-PR: no caller can give the issuer a clock, and a token stops at 24 hours on the running clock whatever the time of day says; tests for a step back, a corrected jump, and an expired token after a step back |
| F-11 | Medium | `Issue` signed tokens for `web-console`, which the circuit breaker never quarantines, and `anonymous`, the rate limiter's bucket for callers that name no agent | Fixed in-PR: both refused, the second named by a new `DefaultUnauthenticatedID` |
| F-12 | Low | A new token for an agent left its earlier ones working, so registering again never shut out a leaked or stolen copy | Fixed in-PR: `Issue` refuses the agent's earlier tokens as revoked; PR 7's client re-registers once for all its calls in flight (noted there) |
| F-13 | Low | `Check` tested expiry before revocation, so a revoked token read as expired once its 24 hours passed, and PR 7's client, which registers again on an expired token, would undo the operator's deregistration | Fixed in-PR as far as the issuer can: revocation is checked first, so a revoked token reads as revoked while the issuer holds its agent; it forgets the agent some time after the token expires, so not registering again after a revocation → deferred to PR 7 (noted there) |
| F-14 | Low | The monotonic clock stops while the machine sleeps, so a token lived 24 hours plus every hour the orchestrator's host slept | Fixed in-PR: a token also stops at its `ExpiresAt` by the time of day |
| F-15 | Low | A token's dates were the time of day at the issuer's start plus the time passed, so an orchestrator started before its first time sync, or after the host slept, dated every token wrongly for its whole life | Fixed in-PR: the dates are the time of day at issue |
| F-16 | Low | go-spew, which prints testify's failure diffs, followed the key's pointer and printed its bytes; the key test tried only zap, JSON and `fmt` | Fixed in-PR: the key lives only in the HMACs' pool and the function that makes them, and the test reads a testify diff |
| F-17 | Low | The test named for forgetting only once the map has grown could not fail: it would pass if `Issue` forgot on every call | Fixed in-PR: it expires every token and checks the next `Issue` forgets none until the map has doubled; the helper that grows the map fails instead of looping forever |
| F-18 | Low | The concurrency test ran on the real clock and ended in milliseconds, so nothing expired and no sweep deleted while other goroutines checked | Fixed in-PR: its clock moves on at every read, and the test checks agents were forgotten while the others ran |
| F-19 | Info | `Check` made 11 allocations, a fresh HMAC included, on what PRs 8–10 make every agent call | Fixed in-PR: pooled HMACs and buffers, one allocation for a token that checks; a test pins it, skipped under the race detector, which drops pooled values |
| F-20 | Info | Revocation was ordered by timestamps, with a nanosecond tie-break and a floor, which tied its correctness to the clock | Fixed in-PR: a serial number in each token orders it against revocations, and time is used only for expiry |
| F-21 | Info | The ID error and its comment restated the agent ID pattern and the 256-byte limit, which would go stale if `MaxAgentIDLen` changed | Fixed in-PR: the message names neither, and the comment links them |
| F-22 | Low | RFC 0009's open question 4 still called the `(AgentID, IssuedAt)` set the right shape, though PR 5 keeps one mark per agent | Fixed in-PR: its dated note says what was built and that no single token can be revoked alone |
| F-23 | Info | F-8's head branch breaks the repository's branch naming rule, and was noted rather than fixed | Noted: a PR cannot change its head branch, so a fix means reopening the PR from the planned branch; the maintainer chose to keep this PR |
| F-24 | Info | The PR lengthened the ROADMAP header's latest-changes note, already about 170 words | Fixed in-PR: the note says which rows are under way and links the v0.4.0 plan's progress table |

## PR 6 — `feature/v040-rfc0009-enrollment`

Merged as [#1076](https://github.com/mkhomutov/Persatrix/pull/1076) on
2026-11-01.

**Implementation note (2026-11-01)**: how this PR meets
[the plan's scope](0009-phases-3-4-pr-plan.md#pr-6--featurev040-rfc0009-enrollment).
Each point not settled there is a new default this PR's review confirms or
overturns.

- **Where.** `security.AgentEnrollment` (`internal/security/agent_enrollment.go`)
  holds the registry and token steps; `internal/server/agent_enrollment.go`
  holds the secret check, the registration limit and the audit lines; the
  handlers in `agent_handlers.go` call them. `cmd/orchestrator/enrollment.go`
  reads `SECURITY_ENROLLMENT_SECRET` and hands the server its agent-identity
  options; the unquarantine secret's wiring moved there from `main.go`
  (499 lines), which now makes the one `AgentEnrollment`, over the registry
  and one `AgentTokenIssuer`, and passes it in, so every server that
  registers agents, and PR 10's gRPC check, can share it. A server given
  none makes its own.
- **The secret (new default).** It travels in an `X-Enrollment-Secret`
  header, never `Authorization`, which carries account sessions, and is
  compared in constant time through the same SHA-256 helper the unquarantine
  bearer uses. Its surrounding whitespace is trimmed, as a header's is. A shown secret that differs from the one set gets 401 and an
  `agent.token_invalid` line with `reason: enrollment_secret` and the
  caller's address, read past trusted proxies; neither secret is ever
  written. No secret shown is admitted, as the scope says; with none set, a
  shown one is ignored, so a fleet given the secret before its orchestrator
  keeps registering. The check runs after the ID checks and before the
  capability checks, so a caller with a wrong secret cannot make
  registration write its capability audit lines.
- **The response (new default).** The `201` body adds `agent_token` and
  `agent_token_expires_at` to the agent's fields, with
  `Cache-Control: no-store`. The list and get routes carry no token.
- **IDs.** `security.IssuableAgentID`, exported from PR 5's unexported
  check, refuses `web-console`, `anonymous` and an ID over
  `MaxAgentIDLen` with 400 before anything registers.
- **One step at a time (PR 5's F-6).** One lock in `AgentEnrollment` covers a
  registration's registry upsert and token issue, and a deregistration's
  removal and revocation, so a racing pair leaves the agent either
  registered with a working token or gone with a revoked one; a test puts
  the deregistration between the two steps, and fails without the lock.
  Registration is rare, so one lock costs nothing a per-agent one would
  save. Revocation follows a successful removal only, and a registration
  whose token cannot be issued is undone.
- **The limit (new default).** One address may register 120 times a
  minute, counted by the caller's address, not its `X-Agent-ID`; over it,
  `429` with `Retry-After`. It bounds secret guessing and the audit
  flushes each registration forces, and fits a host's whole fleet.
- **Audit.** Every registration writes `agent.token_issued` with the
  address, the caller's source and the expiry, never the token, and, with
  a secret set, `enrollment_secret: shown` or `absent`. An agent already
  registered adds `previous_token: revoked` (PR 5's F-12), and `previous_address`
  or `previous_source` when the address it gives or the source it comes
  from differs (G-14). A deregistration writes `agent.deregistered` with
  the caller's source. All three are security-class, as `audit_event.go`
  has classed the token events since Phase 1.

**Review findings** (PR 6): F-1 to F-8 are the code review's, whose fixes
went into the PR's last commit, in that commit's order. That review kept no
numbers or severities, so these were added after the merge. F-9 and F-10 are
a security review's notes on the fixed PR, which found no vulnerability;
their severities, added after the merge too, rate what each would do if its
named PR left it out. No review recorded a ruling on the note's new defaults.
The fix commit shows this much: the secret's and the response's stood, with
the secret trimmed (F-7), and the limit's came from F-3.

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| F-1 | Medium | The takeover check compared the address in the request's body, which the caller picks | Fixed in-PR: `agent.token_issued` names the caller's source, and `previous_source` when a registered agent comes back from another one |
| F-2 | Low | With a secret set, a registration that showed none left no trace | Fixed in-PR: the audit line says `enrollment_secret: shown` or `absent`, and the startup log says such a registration is still admitted |
| F-3 | Medium | Nothing limited registrations, so a caller could guess the secret without end, and each try forced an audit flush | Fixed in-PR: one address may register 120 times a minute, counted by the caller's address; over it, `429` with `Retry-After` |
| F-4 | Low | Deregistration revokes tokens and is open to anyone, but wrote no audit line | Fixed in-PR: it writes `agent.deregistered` with the caller's source |
| F-5 | Low | The lock keeping the registry and the tokens in step was private to the REST server, and a registration whose token could not be issued stayed registered | Fixed in-PR: the lock and both steps moved into `security.AgentEnrollment`, which `main.go` makes once, and such a registration is undone |
| F-6 | Medium | The race test passed without the lock, so it could not catch what it claimed | Fixed in-PR: it stalls the registration between its two steps until the deregistration has run, and fails without the lock |
| F-7 | Low | A secret ending in a line break never matched a header's value | Fixed in-PR: the secret is trimmed, as a header is |
| F-8 | Low | The security package comment, the token events' reserved notes and the ROADMAP row still said registration hands out no tokens | Fixed in-PR |
| F-9 | Medium | Registration admits the ID `local`, which is `channels.DefaultPrincipalID`, so an agent can hold a token naming it. Nothing reads the token yet | → deferred to PR 8 (noted there) |
| F-10 | Medium | No compose file sets `env_file` or any `SECURITY_*` variable, so `SECURITY_ENROLLMENT_SECRET` in `.env` reaches no container. Nothing requires the secret yet | → deferred to PR 9 (noted there) |
| F-11 | Info | The PR merged with its description saying "Not yet reviewed", and with no findings table here | Noted: this table is the review's record. The description was not edited, so it still says that |
| F-12 | Info | The PR description ends with an assistant attribution line, which the repository's rules forbid in PRs and commits | Noted: a merged PR's description can still be edited, so the line stays only until the maintainer removes it |
| F-13 | Info | The head branch is not the planned `feature/v040-rfc0009-enrollment`, and the PR is over the ~350-line estimate and the 500-line target: 21 files, +1 167, of which 634 are tests | Noted: the row records the planned name, as for PRs 3 and 5. The PR missed the plan's 500-line limit and merged at that size: 442 lines of code, 91 of docs, and the tests that prove the code, not split from it |

## PR 7 — `feature/v040-rfc0009-agent-token-client`

Merged as [#1079](https://github.com/mkhomutov/Persatrix/pull/1079) on
2026-11-03.

**Implementation note (2026-11-01)**: how this PR meets
[the plan's scope](0009-phases-3-4-pr-plan.md#pr-7--featurev040-rfc0009-agent-token-client).
Each point not settled there is a new default this PR's review confirms or
overturns.

- **Where.** `AgentTokenKeeper` in `agents/server_reregister.py` holds the
  token and runs the registrations that replace it. `token_middleware` in
  `agents/orchestrator_session.py` shows the token on each call and acts on
  a refusal, through aiohttp's client middleware, which needs aiohttp 3.12,
  so `agents/pyproject.toml` raises its floor from 3.9, and names `yarl`, whose
  URLs it compares with the orchestrator's address. The token comes off the
  request once the answer is back, because a response error's `repr` prints
  the request's headers. `wallet_client.py`
  reads the token on each call through a function it is given, and first
  gave `Lease` and `BudgetExceededError` to a new `wallet_lease.py`, an MIT
  leaf listed beside it in the import contract.
- **What a refusal looks like (new default; PR 8 builds it).** A 401 with
  the orchestrator's usual error body, whose `code` is `AGENT_TOKEN_INVALID`,
  `AGENT_TOKEN_EXPIRED` or `AGENT_TOKEN_REVOKED`. Any other 401 is about
  something else and is left alone.
- **One registration (F-12).** `AgentTokenKeeper.register` starts a
  registration or joins the one under way, so a reconnect and every call
  refused meanwhile share it, and one failed registration fails them all
  rather than one more try for each. It runs as a task of its own, so a
  caller whose time runs out leaves it finishing for the others. A refused
  call whose token is still the current one joins a registration under way,
  which may be what retired it, and any newer token than the one it showed
  retries it. The retried call's answer stands, whatever it is, so nothing
  can loop.
- **Revoked (F-13).** A refusal of the current token that says revoked
  stops the client registering, by either road, until its process restarts,
  and it remembers, so a later "expired" for the same token changes nothing.
  A client never told cannot know: an agent deregistered while idle comes
  back at its next channel reconnect, which has nothing to check, or at its
  next call after the issuer forgot it, which reads as expired. An agent whose
  orchestrator restarted reads as invalid and registers too. The stop is not
  always an operator's: a second process registering the same ID retires the
  first one's token, and the first then stops and says so in its log.
- **A lost answer (new default).** The orchestrator issues a token and the
  connection drops before the answer arrives. That retires the token the
  client still holds, so its next call reads as revoked, though nobody
  revoked anything. While no registration has been answered, the client takes
  "revoked" for one more registration rather than stopping for good, which
  would mute the agent over one dropped connection. A 5xx counts as no
  answer, since a gateway may send one for a registration that landed; any
  other answer counts. The cost is narrow: an operator's deregistration in
  the same window is undone, and audited.
- **No timer (new default).** A token is replaced only when a call is
  refused or the channel reconnects, never ahead of its expiry, though the
  answer gives the time and the log records it. A timer needs no refusal to
  fire, so it would bring back an idle agent an operator had deregistered.
- **Registration.** It shows the secret and no token, since a stale token
  must not block its own replacement, and follows no redirect, so the secret
  reaches no other host. `AgentServer.stop` closes the keeper before it
  deregisters: a refused call cannot bring a leaving agent back, and the
  deregistration waits for a registration under way, so the two cannot pass
  each other on the wire (the client's half of PR 5's F-6). A redirect
  to another host carries no token either, since aiohttp runs the middleware
  on every hop.
- **One token per process.** The session names the first hosted agent, so
  only that agent's token is kept; a second agent's beside that ID is what
  PR 8 refuses.

**Review findings** (PR 7): F-1 to F-12 are an independent review's, proved by
running them, and F-13 to F-21 the author's.

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| F-1 | Medium | The one test of the origin check redirected to another port on the same host, so dropping the host or the scheme from the check left every test green | Fixed in-PR: a table of origins, and a redirect to `localhost` on the same port; each part of the check now has a test that fails without it |
| F-2 | Low | Nothing read a token on a started server's wallet, so deleting that wiring left every test green | Fixed in-PR: `test_the_wallet_shows_the_current_token` |
| F-3 | Low | A registration answered with a refusal still counted as unanswered, though a refusal issues no token, so a later "revoked" was doubted and an operator's deregistration undone | Fixed in-PR: an answer below 500 counts, and a 5xx stays in doubt, since a gateway may send one for a registration that landed |
| F-4 | Low | A registration that timed out here can still land after a later one was answered, which retires the later token; the client reads that as revoked and stops until it restarts, with no operator involved. It takes a 10-second stall and one registration overtaking another, and was reproduced against the test orchestrator only | → deferred to PR 8 (noted there) |
| F-5 | Low | With no timer and the keeper closed first, an agent idle past 24 hours deregisters on an expired token and nothing can replace it. Harmless until the route checks a token | → deferred to PR 9 (noted there) |
| F-6 | Low | A 401 whose `code` was a list or an object raised `TypeError` out of the call. Only a proxy in front could send one | Fixed in-PR |
| F-7 | Info | `urlsplit` tidies an address less than aiohttp's own URLs do, so an orchestrator at a spelled-out IPv6 address silently never got a token | Fixed in-PR: compared through `yarl` |
| F-8 | Info | `orchestrator_session.py`'s docstring said the ID never reaches an outside host, but a redirect carries it | Fixed in-PR: reworded |
| F-9 | Info | The note on revocation left out that a reconnect also brings back a deregistered idle agent | Fixed in-PR |
| F-10 | Info | Nothing ordered PR 4 before PR 9, though PR 9 makes agents hold the secret and `shell_exec` inherits it until PR 4 | Fixed in-PR: edge added to the graph |
| F-11 | Info | Seven unbounded waits in the tests hung on a broken client, and no pytest timeout is set: PR 5's F-17 again | Fixed in-PR: a bounded `until`, and teardown releases a held registration |
| F-12 | Info | +1,436 lines at review, 970 of them tests, past the ~350 estimate and the 500-line target | Noted: the refusal paths needed a loopback orchestrator |
| F-13 | Low | `repr` of aiohttp's response error prints the request's headers, token included, and `event_loop.py` logs a failed wake with `%r`. Every publish path catches the error first today, so no log line carried it | Fixed in-PR: the token comes off the request once the answer is back |
| F-14 | Info | The wallet shows the token but does not act on a refusal | → deferred to PR 10 (noted there) |
| F-15 | Low | An agent deregisters, which revokes its token, before its gRPC grace and its last wallet calls, so once tokens are checked that work is refused | → deferred to PR 8 and PR 10 (noted there) |
| F-16 | Info | `shell_exec` inherits the secret until PR 4, and `/proc/<agent pid>/environ` outlives a scrub | → deferred to PR 4 (noted there) |
| F-17 | Info | Anyone holding the fleet secret can stop a live agent by registering its ID, until it restarts. Until PR 9 refuses a retired token's deregistration, a rolling restart's old process can do the same to the new one | Noted: the secret is the fleet's, and the stopped agent logs why |
| F-18 | Low | A call that shows no token is not retried, so an agent whose first registration was refused has no token until a reconnect or a restart | → deferred to PR 9 (noted there) |
| F-19 | Info | `start()` after `stop()` on one server object stays closed | Noted: nothing restarts one |
| F-20 | Info | The client sets no floor on re-registering: if each new token is refused too, each refused call registers once | Noted: the orchestrator's limit of 120 a minute per address bounds it |
| F-21 | Info | `agents/server.py` stands at exactly 500 lines, so the next line added warns | Noted |
