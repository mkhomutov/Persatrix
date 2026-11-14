---
id: ISSUE-0205
summary: "A persona whose memory database fails to open is left out of the event dispatcher but still registers with the orchestrator, which marks an agent healthy at registration and never checks again. Every message sent to it is then refused with the text `receiver overloaded: agent event-loop queue full`, which names the wrong cause."
status: resolved
severity: medium
area: agents
created: 2026-11-10
closed: 2026-11-14
closed_pr: 1114
refs:
  - agents/server_persona.py
  - agents/server_cli.py
  - agents/persona.py
  - agents/channel_receive.py
  - agents/server_pools.py
  - agents/memory/shared_pool.py
  - agents/persona_runtime/state_persistence.py
  - tests/unit/python/test_persona_without_memory_stops_the_agent.py
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

> 2026-11-13 — fixed by stopping the process. Checked against the code
> at `dd006cba` first, and this time by running it: a persona given a
> folder where its database file should be started, was registered, had
> no dispatch target, and had a message refused as
> `receiver overloaded: agent event-loop queue full`. The Summary and
> Context hold as written.
>
> Of the two options, the process now **exits with status 1** when a
> hosted persona cannot open its memory, before it registers anything:
>
> - An agent process hosts one agent. The channel-message handler
>   already refuses a server that hosts more than one, so a persona
>   without its memory leaves a process that serves nothing.
> - An exit is the one signal every supervisor already reads: a
>   container's restart policy, the exit status of `make run-agent`. The
>   shipped containers' own health check only tests that the port accepts
>   a connection, so a process that stayed up would pass it.
> - A restart also retries a cause that passes, such as a locked file.
> - Skipping the registration would have needed the same skip in the
>   re-registration watcher, the token renewal and the start-up catch-up,
>   to keep alive a process with nothing to do.
>
> `initialize_persona_agents` raises `PersonaMemoryError`, and
> `start_or_exit` (`agents/server_cli.py`) stops the server and exits.
> The stop comes first because a database connection is a thread that
> is not a daemon: with one tier open and a later one failing, the
> process could not have ended.
>
> The refusal text: a channel message for an agent with no dispatch
> target is refused as that
> (`NO_DISPATCH_TARGET`, `agents/channel_receive.py`), and
> `receiver overloaded` is left for a full queue. A task agent is the
> case that remains. The handler moved out of
> `agents/server_servicers.py`, which was over the 500-line warning.
>
> Two existing tests pinned the old behaviour (a failing persona is
> skipped and the next one starts) and now pin the new one. With more
> than one agent in a process, which is not supported, one persona's
> failure stops them all.

> 2026-11-14 — review of the fix. Two findings were more than wording.
>
> - **The process did not always end.** `config/agents.yaml` declares a
>   shared memory pool. A pool keeps its rows in the persona's own
>   database file and opens it first, through a connection of its own.
>   When the file opened and its first statement failed (a corrupt or a
>   locked store), the pool was dropped with that connection still open,
>   out of reach of the stop, and its thread kept the process alive: it
>   printed that it was stopping and stayed. A failing pool is now
>   closed before it is dropped (`agents/server_pools.py`). Run with the
>   shipped config: status 1 after 0.4 s on a file that is not a
>   database and after 10.8 s on a locked one, where the first fix was
>   still alive after 25 s. The real-process test now covers that store
>   with the shipped pool block; it used a folder, which leaves no
>   thread behind.
> - **The stop wrote default state over saved state.** Stopping the
>   server saves each persona's state, and the state is read from the
>   store on the last step of opening the memory. A start that failed
>   before that step wrote a new persona's defaults over what an earlier
>   run had saved. The state is now saved only once it was loaded. On
>   `main` the same overwrite happened at the next ordinary stop.
>
> Smaller ones: any other failure to start now stops the server before
> it is raised, so that process ends too; the agent's own log has a
> WARNING for a message refused for want of a dispatch target; the
> `receiver overloaded` text is also what an agent whose event loop is
> stopping answers, and the texts now say so; the cost-regression job's
> list of wake-path files names `agents/channel_receive.py`.
>
> Not changed: a registration left by an earlier run that was killed is
> not removed by a start that fails (ISSUE-0146), and the exit path does
> not take the agent out of the `agent.active` count before the last
> metrics export, as the exit for a port that cannot be bound never did.
