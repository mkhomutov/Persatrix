---
id: ISSUE-0198
summary: "The sweep that rescues an episode stuck at `[summary pending]` runs only at the end of a tick. Stock personas ship `timers: []` and reactive personas have no scheduler, so the sweep never runs for them and recall hides the stuck episode. An episode stuck by a replayed close is then counted as already derived on every later boot and is never retried."
status: resolved
severity: medium
area: agents/memory
created: 2026-11-10
closed: 2026-11-13
closed_pr: 1111
refs:
  - agents/persona_runtime/__init__.py
  - agents/persona_runtime/close_lifecycle.py
  - agents/persona_runtime/state_persistence.py
  - agents/persona_runtime/finalize_close.py
  - tests/unit/python/test_pending_summary_sweep_without_a_tick.py
  - agents/persona_runtime/close_path.py
  - agents/persona_runtime/replay_identity.py
  - agents/server_persona.py
  - agents/tick.py
  - agents/memory/interaction_janitor.py
  - agents/memory/interaction_tracker.py
  - agents/memory/_episodic_replay_dedup.py
  - agents/memory/episodic.py
  - config/agents.yaml
  - docs/issues/ISSUE-0173-idle-ended-conversation-unwritten-until-next-event.md
  - docs/issues/ISSUE-0186-persona-memory-has-no-capacity-bound.md
---

# ISSUE-0198: The interaction janitor never runs for a persona that has no tick

## Summary

When an interaction closes, its episode is first written with the
summary-pending sentinel, `[summary pending]`, and the real summary is
filled in by a second step. If that second step never finishes, the
interaction janitor is meant to mark the episode as failed so that a
later boot can derive it again. The janitor's only caller is the end of
`on_tick`. A persona with no timers never ticks, so for it the episode
stays pending for good.

## Context

- **The only caller.** `maybe_run_janitor` is called at the end of
  `on_tick` in `agents/persona_runtime/__init__.py`.
- **No tick for stock personas.** The three personas in
  `config/agents.yaml` ship `timers: []`. With `timers` set,
  `agents/server_persona.py` passes `register_legacy_timer=False`, so no
  timer fires. A `reactive` persona gets no scheduler at all.
- **A ticking persona can miss it too.** The sweep is skipped while the
  persona is idle (`agents/tick.py`) and on a tick that times out.
- **How a row gets stuck.** A crash between the two close steps, the
  shutdown drain cancelling tasks after its timeout, or a failed summary
  update.
- **Why it stays hidden.** Recall filters pending rows out
  (`agents/memory/episodic.py`).
- **What a later boot does with it.** That depends on which close wrote
  the row:
  - A row left by a *replayed* close carries the replay span identity.
    The re-derivation guard (`replay_identity.py`,
    `_episodic_replay_dedup.py`) counts it as derived on every later
    boot, so the span is never retried. Each boot logs "replayed span
    already derived — skipping re-derivation", which reads as success.
    The guard's comment says recovery "is therefore bounded and
    automatic"; without a tick it is neither.
  - A row left by a *live* close has a random id
    (`interaction_tracker.py`) that the guard never matches. The row
    stays hidden, and its conversation comes back only if the next
    boot's catch-up replays it, as a second episode.

[ISSUE-0173](ISSUE-0173-idle-ended-conversation-unwritten-until-next-event.md)
mentions the tick-only janitor in passing, for a different symptom.
[ISSUE-0186](ISSUE-0186-persona-memory-has-no-capacity-bound.md) has the
same shape for memory clean-up: upkeep that exists and never runs for a
persona.

## Impact

A stock persona that is stopped or crashes mid-close keeps a hidden,
half-written episode. If a replayed close wrote it, that interaction's
summary and the facts derived from it are lost for good, and the only
later log line says the span was already derived. If a live close wrote
it, the summary returns only when catch-up happens to replay the
conversation.

## Proposed fix / investigation path

Run the sweep on a path every persona has: at boot, before the
re-derivation guard reads the database, and on inbound events with the
cooldown the tick path already uses. Fix
[ISSUE-0197](ISSUE-0197-zero-row-write-keeps-the-memory-write-lock.md)
first, since the sweep is one of the writes that keeps the lock.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; confirmed by reading the code, not by a live run.

> 2026-11-12 — the fix for
> [ISSUE-0197](ISSUE-0197-zero-row-write-keeps-the-memory-write-lock.md)
> makes the sweep commit when it finds nothing, so it no longer keeps
> the write lock and can run on the paths proposed above. This issue
> itself is unchanged.

> 2026-11-13 — fixed. The sweep has one entry point,
> `sweep_stuck_closes` (`agents/persona_runtime/close_lifecycle.py`),
> and three callers that share its five-minute cooldown:
>
> - **Boot.** `initialize_memory` runs it once the memory tiers are
>   open. The boot catch-up runs after that, so its re-derivation guard
>   reads a store the sweep has already been over.
> - **An inbound event.** `on_event` runs it before the turn, so a turn
>   that times out or fails does not skip it.
> - **A tick**, as before.
>
> The boot sweep keeps the sweep's own rule: it marks only a row whose
> close is older than the grace period (five minutes). So a row left by
> a replayed close is derived again on the same boot when the process
> was down for longer than that, and on the boot after when it came
> back sooner; in between, the event path marks it. Marking every
> pending row at boot would recover on the first boot always, but it
> would rest on one process being the only writer of an agent's rows,
> which nothing checks.
>
> A sweep that fails is logged and counted, and does not stop the boot
> or the event. Checked against the code at `dd006cba`: the Context
> above was right as written.
>
> What is unchanged: a row left by a *live* close is marked, so recall
> shows it as `[interaction summary unavailable]`, but its content
> comes back only if a later catch-up replays the conversation, as a
> second episode beside the marked row. A ticking persona that has gone
> idle and gets no event is swept at its next boot.

> 2026-11-14 — review of the fix. One finding changed behaviour.
>
> - **The new sweep could mark rows this process was still
>   summarising.** A boot catch-up closes its replayed conversations
>   within seconds, and their summaries are then written four at a time,
>   each a model call of up to 30 seconds. With enough of them the queue
>   outlasts the five-minute grace period, and the first event after
>   that marked every row still in it; each queued summary was then
>   written and thrown away. A ticking persona had this race before the
>   fix; the fix gave it to the personas that never tick. The sweep now
>   waits while the persona has a summary of its own on its way, on all
>   three paths, and does not restart the cooldown while it waits.
> - The tick stamped its sweep with the time the tick started, so a tick
>   that waited for the agent's lock could be followed by an event's
>   sweep inside one cooldown. Every path now stamps the sweep itself.
> - Tests: the shared cooldown is now shown in both orders (an event's
>   sweep holds back a tick's, and the reverse), the boot test checks a
>   state only a load can bring back, the failing-sweep test checks the
>   sweep was tried, and one test checks the sweep runs under the
>   agent's lock.
> - Texts: the stop-time drain's warning said a cancelled row
>   "re-derives on the next boot"; that is the boot after the janitor
>   has marked it, which a restart straight after a stop is not.
>   ISSUE-0173 has a note that its "runs only on a tick" is out of date.
