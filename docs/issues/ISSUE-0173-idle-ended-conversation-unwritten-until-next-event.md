---
id: ISSUE-0173
summary: "A conversation that ends by its idle window is written to memory only when the persona's next event arrives. The orchestrator records an idle close only at the channel's next message and tells no member, and a reactive persona has no tick to run its own idle check. Until that event the conversation is open, unwritten and cannot be recalled, and the reply to the event itself is written before the idle check runs, so that reply cannot recall it either. ISSUE-0172 fixed only the stop."
status: open
severity: low
area: memory
created: 2026-09-28
refs:
  - docs/issues/ISSUE-0172-stopping-agent-drops-open-conversations.md
  - agents/persona_runtime/action_loop.py
  - agents/persona_runtime/episode_routing.py
  - agents/server_persona.py
  - internal/channels/interaction_resolver.go
  - docs/rfcs/0030-interaction-id-producer-pr-plan.md
  - docs/rfcs/0020-pr-plan.md
---

# ISSUE-0173: An idle-ended conversation stays unwritten until the persona's next event

## Summary

A persona writes a conversation to memory when it closes it. A conversation
that ends because the channel went quiet closes only when the persona's next
event arrives, because nothing else looks for it:

- the orchestrator records an idle close only when the channel's next
  message is published (`interaction_resolver.go`), and notifies no member;
- a reactive persona gets no tick (`server_persona.py` starts a tick
  scheduler only for semi-autonomous and autonomous personas), and its idle
  check runs on the event path (`episode_routing.py`).

Until then the conversation is open, unwritten and cannot be recalled. When
the next event does arrive, the persona builds its reply's memory context
(`action_loop.py`, `_inject_memory_context`) before the idle check writes the
old conversation (`_store_event_episode`), so that reply cannot recall it
either; only the reply after it can.

## Context

Found while fixing
[ISSUE-0172](ISSUE-0172-stopping-agent-drops-open-conversations.md), which
makes a stopping agent write the conversations it still holds open. That
covers a stop, not a deployment that keeps running: a meeting that ends by
its idle window in the evening is still unwritten when the next meeting's
opener arrives in the morning. The lag itself is documented (the channels
guide calls it "inherent to the lazy rotation"). RFC 0030's producer plan
defers an idle-close timer sweep "only if the lag bothers the summary
surface or operators" (open question 3). This issue is that evidence.

## Impact

A long-running persona answers the first message of a new conversation
without the conversation that ended just before it, whenever that one ended
by its idle window. Recall catches up one turn later. The channel's own
conversation window still carries the end of the earlier conversation when
both happen in the same channel, which hides the gap there; it shows when
the next conversation is in another channel.

## Proposed fix / investigation path

Either closes the conversation when its idle window ends, whether the
process keeps running or stops:

- **Persona side:** a periodic idle sweep for every persona, reactive ones
  included, that makes no model call of its own: under the agent's lock,
  `idle_check()` over the live records and the close path for each. The
  RFC 0020 PR plan meant `idle_check` to run from such a janitor; the
  janitor that exists runs only on a tick and only sweeps stuck summaries.
- **Orchestrator side:** an idle-close timer that retires the interaction id
  when the window ends and sends every member a close notification with the
  idle trigger, as the bounded and end-vote closes already do.

## Notes

> 2026-09-28 — captured in the review of the ISSUE-0172 fix
> ([#1014](https://github.com/mkhomutov/Persatrix/pull/1014)).
