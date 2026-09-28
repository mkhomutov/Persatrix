---
id: ISSUE-0172
summary: "An agent that stops drops every conversation it still holds open, and with it everything that conversation should have written to memory: nothing closes an open record with the shutdown reason, though the reason is defined. A room that ends by its idle window tells no member, and a reactive persona checks for idle conversations only when its next event arrives, so such a conversation stays open until then. EXP-001's arm D restarts its advisers after every meeting; on real processes, a briefing that ended by the idle window left all four advisers' memory empty."
status: resolved
severity: medium
area: memory
created: 2026-09-28
closed: 2026-09-28
closed_pr: 1014
refs:
  - agents/server.py
  - agents/persona_runtime/state_persistence.py
  - agents/persona_runtime/close_lifecycle.py
  - agents/memory/boundary_detectors.py
  - internal/channels/close_notification.go
  - evaluators/persona_driver.py
  - docs/experiments/EXP-001-harness.md
---

# ISSUE-0172: An agent that stops drops the conversations it still holds open

## Summary

A persona agent writes a conversation to memory when it closes it: a
summary, and the facts the summary extracts. It closes a conversation when
the room tells it the discussion is over, when a message arrives under a
new interaction id, or once the conversation has been quiet for its idle
window. The idle check runs only when the agent's next event arrives, and a
reactive persona gets no timer ticks between events. When the agent stops,
`close_memory` waits for the summaries already running and then closes its
stores. It does not close the conversations still open. They were lost with
the process, and what they should have written to memory never was. The
close reason `shutdown` is defined for exactly this, but nothing used it.

## Context

Found while building arm D of EXP-001 (harness PR 5c), which restarts its
advisers between meetings so each meeting gets its own clock and call-log
settings. The orchestrator tells every member when a discussion closes by
an end vote or by a bounded close (round limit, depth cap or cost). It
tells no one when a discussion ends by its idle window: that close is
recorded only when the next message arrives. So after an idle close each
adviser still holds the conversation open. At the memo turn only the chair
hears the next message; the other three are observers, and the
orchestrator sends observers nothing.

A probe held arm D's practice briefing and first plan meeting on real
processes, on the offline mock provider, where every governed discussion
ends by the idle window. Before the plan meeting, all four advisers'
stores were empty: the briefing had written nothing. After it, only the
chair had one episode, closed when the memo request reached it. The same
two meetings with arm B's governance, which end at the depth cap or round
limit and so notify everyone, left every adviser with the briefing's
episodes.

## Impact

A stop loses the memory of the conversations an agent still holds open
unless the next start's catch-up reads them again. On the real clock it
replays each channel's last 50 messages and derives them, but not a
conversation older than that window, one from before a clock anchored
after it, or one from an orchestrator older than channel-store v12.
Nothing is logged, since nothing was attempted. For EXP-001 it would bias
the experiment against memory: arm D keeps memory as shipped, and each
restart anchors a new clock, so catch-up drops the earlier meetings and
the restarts only the experiment makes would erase every meeting that
ended by its idle window.

## Fix

A stopping agent (`AgentServer.stop` passes `close_memory(write_open=True)`)
now closes every open conversation before it waits for the running
summaries, so their summaries run and are waited for too. One instant
closes them all, as a room close does: by the idle rule a conversation
whose idle window has already run out, as the next event would have, and
with the shutdown reason the rest. A conversation the catch-up replay
opened at startup is left alone: it is written only when its replay pass
ends, and the next start reads it again. A summary still running when the
wait's bound (60 seconds) runs out stays marked pending, as before. The
summariser is not told a shutdown close's reason, so it cannot record the
stop as part of the conversation.

`close_memory()` alone only releases the stores, as tests and the
golden-trace driver want: that driver ends a run at its snapshot, and
writing the conversations still open would summarise them, a model call no
golden recorded.

On the real clock, a conversation written at the stop and still inside the
next start's catch-up window is derived a second time there: the replay's
guard matches only an earlier replay's digest. That is the duplicate a
conversation closed live before a restart already had, accepted over the
loss where the replay cannot read the window again, and tracked as
[ISSUE-0174](ISSUE-0174-restart-derives-a-conversation-twice.md).

The root causes stay open as
[ISSUE-0173](ISSUE-0173-idle-ended-conversation-unwritten-until-next-event.md):
while the process keeps running, a conversation that ended by its idle
window is written only when the persona's next event arrives.

## Notes

- EXP-001's arms B and C stop their advisers after every meeting too. They
  now write a few summaries as they stop. Their memory is off, so the
  harness records those calls but does not count them in the arms'
  dollars.
- The harness gives each process 90 seconds to stop before killing it,
  which covers the 60-second wait. The compose files give each persona
  service the same 90 seconds (`stop_grace_period`), where Docker's
  default of 10 would kill a stop whose summaries are slow.
