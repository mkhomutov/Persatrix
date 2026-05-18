"""Personal-tier construction seam (RFC 0029 Phase 1 PR 3).

``create_persona_agent`` was the sole production site outside
``agents/memory/`` that constructed the per-agent personal tiers
(:class:`~agents.memory.episodic.EpisodicMemory`,
:class:`~agents.memory.relationship.RelationshipMemory`,
:class:`~agents.memory.facts.FactStore`) directly.  For
``EpisodicMemory`` and ``RelationshipMemory`` that made it the only
production site where the RFC 0029 PR 2 ``DeprecationWarning`` on
direct external construction fired — those two tiers call
:func:`~agents.memory._boundary.warn_external_construction` from their
``__init__``.  ``FactStore`` carries no such boundary guard, so its
construction never warned; routing it through this seam is a
*consistency* move — one builder for all three personal tiers — not a
deprecation-window close.

:func:`build_personal_tiers` moves that construction *inside*
``agents/memory/`` — the facade package — so the boundary warning stays
silent on the production path while the persona runtime keeps driving
each tier's own ``initialize()`` / ``close()`` lifecycle
(:class:`agents.persona_runtime.state_persistence._StatePersistenceMixin`).
It is the construction half of the RFC 0029 §C ``MemoryStore`` facade:
``MemoryStore`` owns the *runtime* personal-tier surface, this helper
owns *building* the tiers the persona runtime then wires up by hand.
"""

from __future__ import annotations

from dataclasses import dataclass

from .episodic import EpisodicMemory
from .facts import FactStore
from .relationship import RelationshipMemory

__all__ = ["PersonalTiers", "build_personal_tiers"]


@dataclass(frozen=True, slots=True)
class PersonalTiers:
    """The three un-initialised per-agent personal-tier handles.

    Returned by :func:`build_personal_tiers`.  Frozen so a caller cannot
    silently swap a handle after the persona runtime has wired it into
    its :class:`~agents.persona_runtime.MemoryNamespace`.
    """

    episodic: EpisodicMemory
    relationship: RelationshipMemory
    facts: FactStore


def build_personal_tiers(
    agent_id: str,
    *,
    db_path: str = "data/memory.db",
) -> PersonalTiers:
    """Construct the three personal tiers for *agent_id* (RFC 0029 §C).

    Construction happens here, inside ``agents/memory/``, so the RFC 0029
    PR 2 boundary ``DeprecationWarning`` stays silent — the persona
    factory is no longer a direct external constructor of
    ``EpisodicMemory`` / ``RelationshipMemory`` (``FactStore`` carries no
    boundary guard, so it never warned either way).  The returned handles
    are *un-initialised*: the caller (the persona runtime) opens and
    closes each tier.

    ``FactStore`` is built with ``shared_db=None`` — each tier owns its
    own ``aiosqlite`` connection, exactly as the pre-PR-3 factory did.
    For file-backed databases the connections share the file and the
    umbrella migration runner is idempotent across them; under
    ``:memory:`` each connection is an isolated database, so a
    cross-tier ``JOIN`` would not find rows on the test path — no caller
    relies on that join today.
    """
    return PersonalTiers(
        episodic=EpisodicMemory(agent_id=agent_id, db_path=db_path),
        relationship=RelationshipMemory(agent_id=agent_id, db_path=db_path),
        facts=FactStore(agent_id=agent_id, db_path=db_path, shared_db=None),
    )
