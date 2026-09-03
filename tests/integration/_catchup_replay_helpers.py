"""Shared fixtures for the ISSUE-0130 (b) catch-up replay suites.

Extracted (v0.3.15 PR B2 review round 3) so
``test_catchup_replay_attribution.py`` and
``test_catchup_replay_dedup.py`` share one history-row builder, one pass
driver and one set of episode probes without either file crossing the
500-line cap ``scripts/checks/file_size.py --strict`` enforces.

The leading underscore keeps pytest from collecting this as a test module.
"""

from __future__ import annotations

from agents.channel_replay_event import build_replay_event
from agents.memory.interactions import scope_for_group
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_runtime.replay_identity import REPLAY_INTERACTION_ID_PREFIX

from ._interaction_multi_turn_helpers import GROUP_CHANNEL

__all__ = [
    "CHANNEL",
    "SCOPE",
    "_derived",
    "_history_row",
    "_replay",
    "_replay_derived",
    "_replay_identity",
]

SCOPE = scope_for_group(GROUP_CHANNEL)
CHANNEL = {"channel_type": "group", "id": GROUP_CHANNEL}


def _history_row(
    msg_id: str, sender: str, content: str, principal: str | None,
) -> dict:
    """One ``channelMessageResponse`` as catch-up receives it.

    ``principal=None`` omits the key entirely — a pre-v12 orchestrator.
    The Go DTO is deliberately not ``omitempty``, so on a v12
    orchestrator the key is always present, ``"local"`` included.
    """
    row: dict = {
        "id": msg_id,
        "channel_id": GROUP_CHANNEL,
        "sender_id": sender,
        "content": content,
        "mentions": [],
        "metadata": {"interaction_id": "wire-A"},
    }
    if principal is not None:
        row["principal_id"] = principal
    return row


async def _replay(agent: _LLMPersonaAgent, *rows: dict) -> None:
    """One catch-up pass: replay ``rows``, then sweep the scopes it opened.

    Through ``on_event`` rather than ``_store_event_episode`` on purpose
    — binding the seeded principal for the ingest is
    ``request_scope_from_metadata``'s job there, and a test that skipped
    it would pass while production attributed nothing.
    ``close_replayed_interactions`` is the pass-end sweep
    ``replay_for_persona_agents`` runs in its ``finally``.
    """
    for row in rows:
        await agent.on_event(build_replay_event(row, GROUP_CHANNEL, "all", CHANNEL))
    await agent.close_replayed_interactions()
    await agent.drain_pending_summaries()


async def _derived(agent: _LLMPersonaAgent) -> list[tuple]:
    """``(principal_id, speaker_id, summary)`` per episode — the triple the
    release's live MT reads, in the order the rows were written."""
    db = agent._episodic_memory._ensure_db()
    async with db.execute(
        "SELECT principal_id, speaker_id, summary FROM episodes "
        "WHERE agent_id = ? ORDER BY created_at",
        (agent.agent_id,),
    ) as cursor:
        return [tuple(r) for r in await cursor.fetchall()]


async def _replay_derived(agent: _LLMPersonaAgent) -> list[tuple]:
    """Only the episodes a REPLAYED span produced.

    ``REPLAY_INTERACTION_ID_PREFIX`` is the production marker — a replay
    derivation writes its content digest as ``interaction_id``, where a
    live close writes a ``uuid4`` — so this is the discriminator the store
    itself carries, not one the test invents.  Filtering by principal is
    NOT enough: an authenticated live turn derives under the same tenant
    as the replayed rows it interleaved with, which is the whole point of
    shape (b).
    """
    db = agent._episodic_memory._ensure_db()
    async with db.execute(
        "SELECT principal_id, speaker_id, turn_count FROM episodes "
        "WHERE agent_id = ? AND interaction_id LIKE ? ORDER BY created_at",
        (agent.agent_id, f"{REPLAY_INTERACTION_ID_PREFIX}%"),
    ) as cursor:
        return [tuple(r) for r in await cursor.fetchall()]


async def _replay_identity(agent: _LLMPersonaAgent) -> list[str]:
    """The ``replay-`` interaction ids this agent has stored."""
    db = agent._episodic_memory._ensure_db()
    async with db.execute(
        "SELECT interaction_id FROM episodes WHERE agent_id = ? "
        "AND interaction_id LIKE ? ORDER BY created_at",
        (agent.agent_id, f"{REPLAY_INTERACTION_ID_PREFIX}%"),
    ) as cursor:
        return [r[0] for r in await cursor.fetchall()]
