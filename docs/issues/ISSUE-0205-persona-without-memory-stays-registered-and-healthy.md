---
id: ISSUE-0205
summary: "A persona whose memory database fails to open is left out of the event dispatcher but still registers with the orchestrator, which marks an agent healthy at registration and never checks again. Every message sent to it is then refused with the text `receiver overloaded: agent event-loop queue full`, which names the wrong cause."
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
  - internal/registry/registry.go
  - internal/server/agent_handlers.go
  - docs/issues/ISSUE-0146-registration-not-a-property-of-a-live-connection.md
---

# ISSUE-0205: A persona whose memory fails to open stays registered and healthy

## Summary

At startup `agents/server_persona.py` initialises each persona's memory.
On failure it logs "agent will NOT receive dispatched events or tick
scheduling" and skips the persona. Nothing else learns of it:

- `_self_register` (`agents/server.py`) still registers every hosted
  agent with the orchestrator.
- The orchestrator marks an agent healthy when it registers and never
  checks again. Its registry has no health-check loop yet
  (`internal/registry/registry.go` carries the TODO), so the agent's own
  `health_check`, which always returns `True`, has no reader.

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

The fleet looks healthy, the persona never replies, and the error the
orchestrator receives points at load, not at the database. The agent's
own log does hold the startup error, and a warning per message that the
dispatch target was not found. Likely causes are a corrupt or locked
`memory.db`, a failed migration, or a volume that is not writable.

## Proposed fix / investigation path

Either exit non-zero when a hosted persona cannot open its memory, or
skip its registration, at startup and on re-registration. Reporting it
as "not serving" would have no reader today. Give the dispatcher's
"target not found" case its own refusal text.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; confirmed by reading the code, not by a live run.
