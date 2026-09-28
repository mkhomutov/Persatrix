---
id: ISSUE-0175
summary: "When a persona closes many conversations at once, every summary call starts at the same moment. A stop closes all of them (ISSUE-0172), and the per-event idle check can close many too. Only replayed conversations are paced (four at a time); a live close draws no wallet lease either. A provider that limits concurrent calls refuses the excess, and each refused summary is stored as the permanent '[interaction summary unavailable]' with its facts skipped."
status: open
severity: low
area: memory
created: 2026-09-28
refs:
  - https://github.com/mkhomutov/Persatrix/pull/1014
  - agents/persona_runtime/close_path.py
  - agents/persona_runtime/replay_sweep.py
  - agents/persona_runtime/summarize_close.py
  - agents/persona_runtime/finalize_close.py
---

# ISSUE-0175: A persona's close summaries all start at once

## Summary

A persona summarises each conversation it closes in the background. When
one moment closes many conversations, every summary call starts at once:

- a stopping persona closes everything it holds open
  ([ISSUE-0172](ISSUE-0172-stopping-agent-drops-open-conversations.md));
- the idle check on each event closes every conversation whose window has
  run out, across all channels.

Only summaries of replayed conversations are paced, four at a time
(`replay_sweep.py`). Live closes are left unpaced because they are
"wire-bounded and metered", but a stop meets neither condition: it closes
every channel at once, and an unmarked close draws no wallet lease.

## Context

Found in the review of the ISSUE-0172 fix
([#1014](https://github.com/mkhomutov/Persatrix/pull/1014), finding F-5).
A scratch test stopped a persona holding 12 open conversations: all 12
summary calls were in flight together, and against a provider that refused
more than four concurrent calls, eight were stored as unavailable.

## Impact

Under a provider's concurrency or rate limit, the excess summaries fail
after the SDK's retries and the 30-second summary timeout. A failed
summary is stored as `[interaction summary unavailable]` and never
retried, so those conversations lose their memory. It shows only when one
moment closes more conversations than the provider accepts at once.

## Proposed fix / investigation path

Pace every close summary, not only replayed ones, sized so that a stop's
summaries still finish within the 60-second wait (`DRAIN_TIMEOUT_SEC`). A
plain four-at-a-time gate would instead leave the queued tail pending when
that wait runs out.

## Notes

> 2026-09-28 — filed from the review of #1014 (finding F-5).
