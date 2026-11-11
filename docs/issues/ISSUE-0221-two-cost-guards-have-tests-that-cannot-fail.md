---
id: ISSUE-0221
summary: "Two guards on spending are covered by tests that pass when the guard is removed. The test that an idle persona stops calling the model asserts a lower bound on the call count, and the check that the persona's main turn bills its interaction is a search for a line of source text that now appears twice."
status: resolved
severity: medium
area: tests
created: 2026-11-10
closed: 2026-11-11
closed_pr: 1100
refs:
  - tests/unit/python/test_tick_scheduler.py
  - agents/tick.py
  - tests/integration/test_bored_persona_cost.py
  - tests/unit/python/test_cross_language_interaction_wire_drift.py
  - agents/persona_runtime/action_loop.py
---

# ISSUE-0221: Two cost guards have tests that cannot fail

## Summary

Both guards work today. In both cases the test that is meant to keep
them working would stay green if they stopped.

## Context

**1. An idle persona skips model calls.** `TickScheduler`
(`agents/tick.py`) stops calling `on_tick` once the persona has been idle
for `idle_after_ticks` ticks. `test_idle_skip_llm_calls`
(`tests/unit/python/test_tick_scheduler.py`) runs the scheduler for half
a second and asserts `call_count >= 2`. The behaviour is an upper bound;
the assertion is a lower bound.

With the idle branch changed to keep calling `on_tick`, a control
measured 9 calls where the unchanged code makes 2, and the tick-related
test files all passed, `test_bored_persona_cost.py` among them.

**2. The main turn bills its interaction.** The persona's main model
call passes `interaction_id=lease_interaction_id` so the wallet lease is
counted against the interaction's cost ceiling. The only guard is
`test_lease_call_sites_thread_the_interaction`, which checks that this
literal occurs somewhere in `action_loop.py`. The same literal now also
appears in the later revise call in that file.

With the argument removed from the main call, the tests stayed green in
two runs: 833 tests in a wide selection of wallet, lease and interaction
test files, and 47 tests in the eight files closest to the action loop.
No test asserts the interaction id the loop passes.

## Impact

Either guard can be lost in a refactor without a red test. The first
would let an idle semi-autonomous persona call the model on every tick.
The second would leave the main turn's spend out of the per-interaction
ceiling and the soft-budget close.

## Proposed fix / investigation path

1. Assert `call_count == idle_after_ticks`, on a fake clock so the test
   does not depend on real sleep.
2. Replace the source-text check with a test that drives one channel
   turn through the action loop with a recording wallet and asserts the
   lease's interaction id. Where a text check is kept, require an exact
   number of occurrences.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; both mutations were applied to copies outside the
> repository and the related test files run against them, once when
> found and again in review.

> 2026-11-11 — fixed, tests only. (1) `test_idle_skip_llm_calls` moves the
> event loop's clock by hand, fires the scheduler's timer ten times and
> asserts `on_tick` ran exactly `idle_after_ticks` times; with the idle
> branch changed to keep calling `on_tick` it fails with `10 == 2`.
> (2) A new test in `test_interaction_lease_threading.py` drives one
> direct-message turn through the action loop with a recording wallet and
> asserts the lease's interaction id; with the argument dropped from the
> main call it fails. The text check now requires an exact count, two in
> `action_loop.py`. Both faults reproduced first. One correction: tests
> already covered the client forwarding the id to the lease and the
> loop-side reader; only the loop's own call between them was untested.
