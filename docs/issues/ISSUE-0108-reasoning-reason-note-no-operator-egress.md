---
id: ISSUE-0108
summary: "The RFC 0051 reasoning trace's verbatim `reason_note` has NO operator egress in shipped v0.3.10. It is parsed (`agents/salience_deliberation.py`) and carried on the `SalienceDecision` (`agents/salience_bid.py:469`) but no log statement ever writes it — the code/tests call its egress 'the operator-debug path, wired in a later PR' (`tests/unit/python/test_salience_bid_reasoning.py:78`), and that later PR was the operator-reveal PR 7, which was CUT from v0.3.10. Compounding it, the count-only `agent.deliberated` audit (`agents/persona_runtime/salience_gate.py:102`) attaches `reason_code`/`should_post` via stdlib `extra=`, but the structlog `ProcessorFormatter.foreign_pre_chain` (`agents/observability/logging.py`) has no `ExtraAdder`, so those fields are dropped from the rendered log line — the audit log is presence-only. Net: the deliberation REASON is observable only as the `deliberation.suppressed{reason_code,mode}` metric LABEL, never in the agent log; the verbatim `reason_note` is observable nowhere. MT-REASON-001 Step 2 and RFC 0051 §E both describe the agent log as the reason_note's egress; that description is aspirational, not wired. UPDATE: the `agent.deliberated` (and latent `fact.*`) audit-drop half — the missing `ExtraAdder` — is now FIXED; the audit payload reaches the rendered line. Remaining open scope is the verbatim `reason_note` egress + the §E/MT-REASON-001 doc correction (Gap B), deferred to its own PR."
status: open
severity: low
area: agents
created: 2026-06-26
refs:
  - docs/rfcs/0051-reasoning-before-posting.md
  - docs/rfcs/0051-pr-plan.md
  - docs/manual-tests/MT-REASON-001.md
  - docs/manual-tests/v0.3.10-execution-report.md
  - agents/persona_runtime/salience_gate.py
  - agents/salience_bid.py
  - agents/observability/logging.py
---

## Summary

The RFC 0051 private deliberation produces a verbatim, free-text **`reason_note`**
(a one-clause justification for a silence verdict). [RFC 0051 §E][e] designates
the operator-debug **agent log** as the *only* egress for it, and
[MT-REASON-001][mt] Step 2 reads it from there. In shipped v0.3.10 **that egress
does not exist** — the `reason_note` is parsed and carried on the decision object
but **no log statement ever writes it**.

Two compounding facts make even the low-cardinality `reason_code` unreadable from
the agent log:

1. **The `reason_note` is never logged.** It is parsed by
   [`_parse_reason_note`][parse] and set on `SalienceDecision.reason_note`
   ([`agents/salience_bid.py:469`][bid]), but no consumer reads it for output.
   The unit test that pins it is explicit: *"its only egress is the operator-debug
   path, **wired in a later PR**"* ([`test_salience_bid_reasoning.py`][test]).
   The "later PR" was the OQ 6(a) operator-reveal **PR 7**, which was **cut from
   v0.3.10** (see the [release-prep plan §Known follow-up issues][prep]).
2. **The `agent.deliberated` audit is presence-only.** The count-only audit
   ([`salience_gate.py:102`][gate]) attaches `reason_code` / `should_post` /
   `transcript_turns` via the stdlib `logging` `extra=` dict. But the agent's
   structlog `ProcessorFormatter.foreign_pre_chain`
   ([`agents/observability/logging.py`][log] `_build_processors`) has **no
   `ExtraAdder`**, so stdlib record extras are dropped from the rendered JSON.
   The emitted line carries only `message: "agent.deliberated"` + trace context —
   the `reason_code` payload never appears.

Net effect: the deliberation **reason** is observable **only** as the
`deliberation.suppressed{reason_code, mode}` Prometheus metric **label**; the
agent log shows *that* a deliberation happened (countable `agent.deliberated`
events) but not *why*, and the verbatim `reason_note` is observable **nowhere**.

## Update — fact (2) resolved (Gap A)

The `agent.deliberated` audit-drop above (fact 2) is **fixed**. The root cause
was repo-wide: the audit convention emits its payload via the stdlib
`logger.info(event, extra={…})` idiom — both `agent.deliberated`
([`salience_gate.py`][gate]) and the whole `fact.*` family
([`agents/memory/_facts_audit.py`][facts]) — but the structlog
`ProcessorFormatter` chain surfaced no `extra=` keys, so every such payload was
dropped from the rendered/shipped line (not just the deliberation audit). The
fix adds a `surface_stdlib_extra` processor
([`agents/observability/_stdlib_extra.py`][extra], wired into the shared chain in
[`logging.py`][log] `_build_processors`), placed *before* redaction (so surfaced
extras are still redacted) and *after* `merge_contextvars` (so a bound execution
identity is already present). The `agent.deliberated` `reason_code` /
`should_post` / `transcript_turns` — and every `fact.*` audit payload — now reach
the rendered JSON.

It is **not** a bare `structlog.stdlib.ExtraAdder()`, which is wrong on two axes:

1. **Clobbering.** `ExtraAdder` copies every `extra` key over whatever the chain
   already set, so a colliding key silently replaces (a) `level` —
   `_normalise_level` *prefers* an existing `level` key, so `extra={"level":
   "ERROR"}` on an `info()` call rendered as `ERROR`; (b) the OTEL `trace_id` /
   `span_id` when no span is active (forged correlation); or (c) a
   contextvar-bound identity such as `agent_id` / `service.*` (merged just above)
   — which the deliberation/`fact.*` audits *do* carry, so the audit value was
   overriding the trace-context identity on every line. The guard instead drops a
   surfaced key when it is reserved (schema machinery / OTEL IDs) or already
   present (the contextvar/service value wins), and otherwise lets it *fill the
   gap* — so an audit's own `agent_id` still surfaces when no contextvar is bound
   (early startup / CLI / migrations).

2. **Third-party blast radius.** A bare adder also surfaces the attributes of
   *third-party* records (`grpc`, `asyncio`, `anthropic`/`openai`) into our
   schema'd line and ships them, when the `extra=` audit convention is ours
   alone. The processor is therefore **scoped to our own application logger
   roots** (`agents` / `persatrix_agents` / `Persatrix`); third-party records
   render exactly as they did before ISSUE-0108.

### The CI hang (separate cause — test isolation, not the chain)

The first pushes hung the `Python (lint + test)` job for ~9–13 min in a real-`AgentServer`
test (`TestStartupCatchUpWiring` / `test_registration`'s `TestSessionLifecycle`),
not reproducible on macOS even with the full ordered suite. Root cause was **a
leaked log handler between tests**, not the chain logic:

* `configure_logging` installs a `ProcessorFormatter` handler on the **root**
  logger whose `foreign_pre_chain` runs `_ship_to_orchestrator` — every
  propagated record is enqueued onto the *active* log shipper.
* The only `tests/unit/python/` tests that call `configure_logging` are this PR's
  new rendered-egress tests (the pre-existing fact-audit tests only mention it in
  docstrings — which is why the parent never hung), and their fixtures never
  removed the handler. It leaked onto the root logger.
* A *later* real-`AgentServer` test starts a real shipper against a dead
  orchestrator; the shipper's stream-error path re-logs through the leaked
  handler, which re-enqueues onto the shipper's own queue — a self-feeding loop
  that wedges under the CI runner's gRPC/event-loop behaviour (macOS
  drains/cancels it, so it passed locally).

Fix: an autouse fixture in
[`tests/unit/python/conftest.py`](../../tests/unit/python/conftest.py) snapshots
the root handler set and strips anything a test added (resetting
`configure_logging`'s idempotency guard). No production change. The deeper
shipper self-feedback loop (the shipper re-shipping its own error logs) is a
real latent design smell tracked as a separate follow-up.

New tests assert at the *rendered* layer (the egress an operator reads), not the
`caplog` `LogRecord` layer where the bug was invisible, covering each clobber
case, the third-party-not-surfaced scope, and the `fact.*` triple's redacted
egress.

**Still open (Gap B):** the verbatim `reason_note` (fact 1) still has **zero**
egress, and the RFC 0051 §E / MT-REASON-001 Step 2 docs still describe the agent
log as its egress. That half is intentionally deferred to its own PR (see below)
and keeps this issue open.

## Impact

- **Not a privacy defect — the opposite.** The §E wall is *stronger* than
  documented: the `reason_note` has zero egress, so it cannot leak to a message,
  the store, or a peer. The no-leak gate ([`test_deliberation_no_leak.py`][noleak])
  is unaffected.
- **It is an observability + documentation gap.** The headline promise
  ("a persona stays silent *with a reason*") is delivered at the **reason_code**
  granularity via the suppression metric, but the *operator-readable* surface
  (§E's "agent log") and the *verbatim* `reason_note` are unwired. RFC 0051 §E and
  MT-REASON-001 Step 2 should not claim the agent log carries the reason until the
  egress lands.

## Observed live (MT-REASON-001, v0.3.10 release-prep)

On the `group:planning` channel under `reasoning.mode: bid` against a real
provider: a directed-elsewhere turn suppressed a participant's pile-on
(`deliberation.suppressed{reason_code=…, mode=bid}` incremented and the persona
posted nothing), but the `agent.deliberated` log line carried no `reason_code`,
and no `reason_note` appeared in any agent log at INFO or DEBUG. The reason was
read from the **metric label**, which is what the execution report records as the
v0.3.10 silence-with-a-reason evidence.

## Proposed fix

1. ~~Add an `ExtraAdder` to the structlog `foreign_pre_chain` (or emit the audit
   via a structlog-native bound logger) so the count-only `agent.deliberated`
   audit's `reason_code` / `should_post` reach the rendered line — the audit's
   intended payload.~~ **Done (Gap A)** — a guarded `_surface_stdlib_extra`
   processor added to the shared chain (see *Update* above); fixes the `fact.*`
   audit drop in the same stroke, and (unlike a bare `ExtraAdder`) guarantees a
   caller's `extra` can never overwrite a chain-owned or bound-identity field.

The remaining steps are **deferred — their own reviewed PR, not release-prep**
(Gap B: wiring the cut PR 7's verbatim-`reason_note` agent-log half):

2. Add a single **DEBUG**-level egress for the verbatim `reason_note` on the
   suppression path ([`salience_gate.py`][gate]), guarded to the agent log only,
   with a no-leak test that it reaches the debug log but **never** a message, the
   channel store, or a peer's reconstructed `messages` (extend
   [`test_deliberation_no_leak.py`][noleak]).
3. Update [RFC 0051 §E][e] + [MT-REASON-001][mt] Step 2 to match what actually
   egresses (metric label today; agent log once wired), and flip the
   "wired in a later PR" test docstrings.

Adding an egress to a deliberately-walled field is feature work that wants its own
PR + review, not a ride-along on the release-prep MT-execution PR — hence
deferred.

[e]: ../rfcs/0051-reasoning-before-posting.md#e-privacy-boundary--the-trace-is-walled
[mt]: ../manual-tests/MT-REASON-001.md
[prep]: ../v0.3.10-release-prep-plan.md#known-follow-up-issues
[parse]: ../../agents/salience_deliberation.py
[bid]: ../../agents/salience_bid.py
[test]: ../../tests/unit/python/test_salience_bid_reasoning.py
[gate]: ../../agents/persona_runtime/salience_gate.py
[facts]: ../../agents/memory/_facts_audit.py
[log]: ../../agents/observability/logging.py
[extra]: ../../agents/observability/_stdlib_extra.py
[noleak]: ../../tests/integration/test_deliberation_no_leak.py
