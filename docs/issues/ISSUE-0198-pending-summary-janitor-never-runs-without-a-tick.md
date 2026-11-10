---
id: ISSUE-0198
summary: "The sweep that rescues an episode stuck at `[summary pending]` runs only at the end of a tick. Stock personas ship `timers: []` and reactive personas have no scheduler, so the sweep never runs for them, recall hides the stuck episode, and the boot replay guard treats it as already written."
status: open
severity: medium
area: agents/memory
created: 2026-11-10
refs:
  - agents/persona_runtime/__init__.py
  - agents/server_persona.py
  - agents/memory/interaction_janitor.py
  - agents/memory/_episodic_replay_dedup.py
  - agents/memory/episodic.py
  - config/agents.yaml
  - docs/issues/ISSUE-0173-idle-ended-conversation-unwritten-until-next-event.md
---

# ISSUE-0198: The stuck-summary janitor never runs for a persona that has no tick

## Summary

When an interaction closes, its episode is first written with the
placeholder summary `[summary pending]` and the real summary is filled in
by a second step. If that second step never finishes, a janitor sweep is
meant to mark the episode as failed so that the next boot can derive it
again. The sweep's only caller is the end of `on_tick`. A persona with no
timers never ticks, so for it the episode stays pending for good.

## Context

- **The only caller.** `maybe_run_janitor` is called at the end of
  `on_tick` in `agents/persona_runtime/__init__.py`.
- **No tick for stock personas.** The three personas in
  `config/agents.yaml` ship `timers: []`. With `timers` set,
  `agents/server_persona.py` passes `register_legacy_timer=False`, so no
  timer fires. A `reactive` persona gets no scheduler at all.
- **How a row gets stuck.** A crash between the two close steps, the
  shutdown drain cancelling tasks after its timeout, or a failed summary
  update.
- **Why it stays lost.** Recall filters pending rows out
  (`agents/memory/episodic.py`). The replay guard in
  `_episodic_replay_dedup.py` counts a pending row as already derived and
  waits for the janitor to convert it. Its comment says recovery "is
  therefore bounded and automatic"; without a tick it is neither.

[ISSUE-0173](ISSUE-0173-idle-ended-conversation-unwritten-until-next-event.md)
mentions the tick-only janitor in passing, for a different symptom.

## Impact

A stock persona that is stopped or crashes mid-close loses that
interaction's summary, and the facts and relationship updates derived from
it, with no log line after the original failure.

## Proposed fix / investigation path

Run the sweep on a path every persona has: at boot, before the replay
guard reads the store, and on inbound events with the cooldown the tick
path already uses. Fix
[ISSUE-0197](ISSUE-0197-zero-row-write-keeps-the-memory-write-lock.md)
first, since the sweep is one of the writes that keeps the lock.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; confirmed by reading the code, not by a live run.
