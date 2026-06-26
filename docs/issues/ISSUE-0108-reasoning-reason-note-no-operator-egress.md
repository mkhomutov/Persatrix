---
id: ISSUE-0108
summary: "The RFC 0051 reasoning trace's verbatim `reason_note` has NO operator egress in shipped v0.3.10. It is parsed (`agents/salience_deliberation.py`) and carried on the `SalienceDecision` (`agents/salience_bid.py:469`) but no log statement ever writes it — the code/tests call its egress 'the operator-debug path, wired in a later PR' (`tests/unit/python/test_salience_bid_reasoning.py:78`), and that later PR was the operator-reveal PR 7, which was CUT from v0.3.10. Compounding it, the count-only `agent.deliberated` audit (`agents/persona_runtime/salience_gate.py:102`) attaches `reason_code`/`should_post` via stdlib `extra=`, but the structlog `ProcessorFormatter.foreign_pre_chain` (`agents/observability/logging.py`) has no `ExtraAdder`, so those fields are dropped from the rendered log line — the audit log is presence-only. Net: the deliberation REASON is observable only as the `deliberation.suppressed{reason_code,mode}` metric LABEL, never in the agent log; the verbatim `reason_note` is observable nowhere. MT-REASON-001 Step 2 and RFC 0051 §E both describe the agent log as the reason_note's egress; that description is aspirational, not wired."
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

## Proposed fix (deferred — its own reviewed PR, not release-prep)

Wire the deferred operator-debug egress (the cut PR 7's agent-log half):

1. Add an `ExtraAdder` to the structlog `foreign_pre_chain` (or emit the audit via
   a structlog-native bound logger) so the count-only `agent.deliberated` audit's
   `reason_code` / `should_post` reach the rendered line — the audit's intended
   payload.
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
[log]: ../../agents/observability/logging.py
[noleak]: ../../tests/integration/test_deliberation_no_leak.py
