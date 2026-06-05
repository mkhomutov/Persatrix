# Manual Test MT-PERSONA-009: Group Channel Has a Shared Roster (Who's Here + Roles)

**Test ID**: `MT-PERSONA-009`
**Feature Area**: Persona
**Version**: 1.0
**Created**: 2026-06-05
**Last Updated**: 2026-06-05
**Status**: Active

---

> **Origin**: In a group channel with no shared world-state, personas
> confabulated who is present and what each other does — *"what project do
> you work on together?"* drew three different answers; *"do you know each
> other?"* diverged. Tracked as finding **F-4** in the
> [v0.3.7 conversation test-findings PR plan §PR 6](../v0.3.7-test-findings-pr-plan.md);
> fixed by injecting a per-event channel roster (slice A = the module,
> slice B = the wiring).

---

## Overview

**Purpose**: Verify that, on a group channel, a persona is given — and
uses — a shared **roster** (channel description + each member's name and
role) so answers about who is present and what they do are consistent
across personas.

**Scope**: `channel_roster.inject_channel_roster` wired into
`_inject_memory_context` (group channels only), fed by
`HttpChannelRosterFetcher` (`GET /api/v1/channels/{id}` + `GET
/api/v1/agents`).

**Out of Scope**: DMs (no roster — two known participants); the roster's
effect on relevance/turn-taking (RFC 0030).

---

## Related Documentation

- [`agents/persona_runtime/channel_roster.py`](../../agents/persona_runtime/channel_roster.py)
- [docs/v0.3.7-test-findings-pr-plan.md §PR 6](../v0.3.7-test-findings-pr-plan.md) — F-4.

**Related Automated Tests**:
- [`tests/unit/python/test_channel_roster.py`](../../tests/unit/python/test_channel_roster.py) — build/render/fetch + injection (group injects; DM / no-fetcher / fetch-failure inject nothing; stale roster cleared on a later DM turn).

---

## Preconditions

- ☐ A group channel with ≥2 persona members (e.g. `group:planning` → ember-owl, iron-fox, nova-sparrow).
- ☐ Provider key for the live step; **rebuild the agent image** (`docker compose up -d --build`) — code is baked in.

---

## Test Procedure

### Step 1: Deterministic Gate

```bash
.venv/bin/python -m pytest tests/unit/python/test_channel_roster.py -v
```

**Expected**: all pass — including `TestInjectChannelRoster` (group injects a roster; DM does not; stale roster cleared on a later DM turn).

---

### Step 2: Live — "Who is in this channel?"

**Action**: In a group channel, ask each (or @-mention) — "Who's in this channel, and what does each person do?"

**Expected Result**: Personas name the **actual** members and their roles consistently (from the roster), instead of inventing them. Asked "what do we work on together?" / "do you know each other?", answers are consistent across personas and grounded in the channel brief — no three-different-projects divergence.

**Verification**:
- [ ] Members named match the channel's actual membership
- [ ] Roles match `config/agents.yaml`
- [ ] Cross-persona answers about the room are consistent

---

### Step 3: Live — DM Has No Roster (Belt-and-Braces)

**Action**: Open a DM with one persona and ask "who's in this channel?".

**Expected Result**: The persona does not produce a multi-member roster (a DM is one-on-one). No stale group roster leaks in if you DM right after a group turn.

**Verification**:
- [ ] No group roster in the DM reply

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Roster unit tests pass | ☐ |
| 2 | Consistent, accurate "who's here + roles" on a group channel | ☐ |
| 3 | No roster in a DM; no stale-group leak | ☐ |

---

## Notes

- The roster is injected **outside** the recall `MemoryBudget` (structural
  room context, priority 9, non-compressible), so it is reliably present
  on group turns and never summarized away.
- Enriched names/roles come from `GET /api/v1/agents` (one call for all
  members — no N+1); a member absent from the directory falls back to its
  id.
