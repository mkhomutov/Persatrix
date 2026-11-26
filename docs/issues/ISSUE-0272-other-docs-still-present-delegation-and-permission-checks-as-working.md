---
id: ISSUE-0272
summary: "Outside the instruction files, seven places in the glossary, two guides, a config comment and the orchestration spec still present delegation or sub-agent spawns as working. Neither is built or in use. They are listed here to be corrected in one pass. An eighth place, the security policy's line on tool permission checks, became true when ISSUE-0214 was resolved."
status: open
severity: low
area: docs
created: 2026-11-16
refs:
  - docs/ai-glossary.md
  - docs/guides/persona-agents.md
  - docs/observability.md
  - SECURITY.md
  - config/optimization.yaml
  - docs/ai-agents-orchestration-spec.md
  - agents/action_executor.py
  - agents/sub_agents/spawner.py
  - docs/issues/ISSUE-0214-declared-tool-permissions-are-never-enforced.md
  - docs/issues/ISSUE-0285-sub-agent-permission-rule-and-delegation-timeout-are-not-built.md
  - https://github.com/mkhomutov/Persatrix/pull/1133
---

# ISSUE-0272: Other docs still present delegation and permission checks as working

## Summary

[#1133](https://github.com/mkhomutov/Persatrix/pull/1133)
made the assistant instructions, three glossary entries and the
orchestration spec's notes say that sub-agents are not in use and that
a tool's declared permissions are not checked
([ISSUE-0214](ISSUE-0214-declared-tool-permissions-are-never-enforced.md)).
Other texts still say or imply the opposite. Each was read against the
code on 2026-11-16.

What the code does: the `delegate` and `spawn_sub_agent` actions both
answer `not_implemented` (`agents/action_executor.py`);
`PersonaAgent.spawn_sub_agent()` has no client behind it; the
in-process spawner (`agents/sub_agents/spawner.py`) is called by tests
only; `request_approval`, `grant_approval` and `deny_approval` answer
`not_implemented` too. On that day the permissions a tool declares with
`@tool` were also stored and never checked; they are checked since
2026-11-26 (see the note below).

## Context

| Place | What it says | What is so |
|---|---|---|
| `docs/ai-glossary.md`, Message Bus | Lists "delegation" among the messages the orchestrator and agents exchange; the example is "Delegation messages travel over the message bus" | No delegation message is sent: the `delegate` action answers `not_implemented` |
| `docs/ai-glossary.md`, Autonomy Level | Example: "A `semi-autonomous` agent may delegate but needs approval for external comms" | No agent can delegate, and no approval is asked for at any autonomy level |
| `docs/guides/persona-agents.md`, the v0.3.2 note on the wallet lease | Lists "sub-agent spawn" among the LLM calls that first take a lease | The lease code for a sub-agent exists and is reached only through the spawner, which only tests call |
| `docs/observability.md`, the note on idle reasons | "Sub-agent leases ride the `CAUSE_SUB_AGENT` cause … for delegated work" | The same: the cause exists, nothing in a running system uses it |
| `docs/observability.md`, the table of what a refused lease does | A row "Sub-agent spawn: Spawn aborts; the parent persona observes the failure on its turn" | No spawn happens, so none aborts |
| `SECURITY.md`, the links under the security model | "Tool permission checks … deny-by-default, run in the Python agents" | Settled on 2026-11-26, no correction needed: the permissions any tool declares are now checked before it runs, on both agent kinds ([ISSUE-0214](ISSUE-0214-declared-tool-permissions-are-never-enforced.md)). When this was filed that held only for the nine built-in tools |
| `config/optimization.yaml`, `models.routing.defaults.sub_agents` | The comment calls the key "live" | The key is read (`agents/optimization.py`), by the model choice for a sub-agent, which only the spawner reaches |
| `docs/ai-agents-orchestration-spec.md` §2.1 and §2.4 | Task agents are "used … as ephemeral sub-agents"; persona agents have "sub-agent spawning"; the message bus carries "Delegation" | These two sections carry no dated note. §8.2 does, since 2026-11-14 |

The glossary's Sub-agent, Tool and Permission entries, the spec's §2.2,
§2.5, §4.2 and §8.2, and the instruction files are already corrected
and are not in this list.

## Impact

A reader who starts from one of these places plans on delegation.
Proposed low, as the tracker's other "the text says more than the code
does" findings whose code half is tracked elsewhere: the mechanisms
themselves were
[ISSUE-0214](ISSUE-0214-declared-tool-permissions-are-never-enforced.md),
medium, and what it left unbuilt is
[ISSUE-0285](ISSUE-0285-sub-agent-permission-rule-and-delegation-timeout-are-not-built.md).
The severity is proposed here, not ruled.

## Proposed fix / investigation path

One docs pull request that corrects the seven places left: a clause in
each glossary example, "designed, not in use" at the two guide passages
and the config comment, and two dated notes in the spec. `docs/observability.md` was being
changed by another open pull request when this was filed, which is why
#1133 left it alone.

## Notes

> 2026-11-16 — listed by the review of [#1133](https://github.com/mkhomutov/Persatrix/pull/1133),
> each place then checked against the code. Filed there; none of the
> eight places is changed by that pull request.

> 2026-11-26 — one of the eight is settled by the code, not by a
> correction. The fix for
> [ISSUE-0214](ISSUE-0214-declared-tool-permissions-are-never-enforced.md)
> checks a tool's declared permissions before the tool runs, so the
> security policy's line on tool permission checks now holds as written.
> The other seven are about delegation and sub-agent spawns and are
> unchanged: none is built or in use. The rule that a sub-agent gets no
> more than its parent is tracked in
> [ISSUE-0285](ISSUE-0285-sub-agent-permission-rule-and-delegation-timeout-are-not-built.md).
