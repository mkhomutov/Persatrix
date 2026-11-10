---
id: ISSUE-0205
summary: "A persona whose memory store fails to open is left out of the event dispatcher but still registers with the orchestrator and reports healthy. Every message sent to it is then refused with the text `receiver overloaded: agent event-loop queue full`, which names the wrong cause."
status: open
severity: medium
area: agents
created: 2026-11-10
refs:
  - agents/server_persona.py
  - agents/server.py
  - agents/base.py
  - agents/dispatch.py
  - agents/server_servicers.py
  - docs/issues/ISSUE-0146-registration-not-a-property-of-a-live-connection.md
---

# ISSUE-0205: A persona whose memory fails to open stays registered and healthy

## Summary

At startup `agents/server_persona.py` initialises each persona's memory.
On failure it logs "agent will NOT receive dispatched events or tick
scheduling" and skips the persona. Nothing else learns of it:

- `_self_register` (`agents/server.py`) still registers every hosted
  agent with the orchestrator.
- `BaseAgent.health_check` (`agents/base.py`) always returns `True`.

## Context

When a channel message arrives for the skipped persona, the dispatcher
finds no target and returns `False` (`agents/dispatch.py`). The servicer
turns any `False` into a `TaskAck` with the error "receiver overloaded:
agent event-loop queue full (discard-not-block backpressure)"
(`agents/server_servicers.py`).

[ISSUE-0146](ISSUE-0146-registration-not-a-property-of-a-live-connection.md)
covers the orchestrator side: a registered agent is shown as healthy
whatever happens to it later.

## Impact

The fleet looks healthy, the persona never replies, and the one error an
operator can see points at load, not at the store. Causes include a
corrupt or locked `memory.db`, a failed migration, or a volume that is
not writable.

## Proposed fix / investigation path

Either exit non-zero when a hosted persona cannot open its memory, or
skip its registration and report it as not serving. Give the dispatcher's
"target not found" case its own refusal text.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; confirmed by reading the code, not by a live run.
