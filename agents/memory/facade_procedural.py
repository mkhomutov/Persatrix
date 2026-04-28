"""Procedural-tier mixin for :class:`agents.memory.facade.MemoryFacade`.

Extracted from ``facade.py`` to keep that file under the 500-line repo
cap once the RFC 0008 PR 5 procedural-tier surface (decay knobs +
``store_procedure`` refresh + ``retrieve_procedures`` stale alert) was
added.  Lives as a mixin rather than a free-function module because the
methods need to share the facade's per-process ``EpisodicMemory``
connection and ``_require_initialised`` lifecycle gate.

The companion module :mod:`agents.memory.episodic_procedural` owns the
SQL; this mixin owns the *facade* concerns: arg validation against the
configured ``c_min``/``lambda_per_day``/stale-threshold knobs and the
``stale_memory_injection`` structured log emitted by ``retrieve_procedures``.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from .episodic_procedural import (
    ProcedureRecallEntry,
)

if TYPE_CHECKING:
    from .episodic import EpisodicMemory
from .episodic_procedural import (
    recall_procedures as _recall_procedures,
)
from .episodic_procedural import (
    refresh_confidence as _refresh_confidence,
)

logger = logging.getLogger(__name__)


def validate_decay_params(
    *,
    lambda_per_day: float,
    c_min: float,
    stale_confidence_alert_threshold: float,
) -> None:
    """Validate the procedural-tier decay knobs at facade construction.

    Raises :class:`ValueError` so a misconfigured ``config/agents.yaml``
    surfaces at agent startup rather than producing nonsensical decayed
    confidence values at recall time.
    """
    if lambda_per_day < 0:
        raise ValueError(
            f"lambda_per_day must be non-negative, got {lambda_per_day}"
        )
    if not 0.0 <= c_min <= 1.0:
        raise ValueError(f"c_min must be in [0.0, 1.0], got {c_min}")
    if not 0.0 <= stale_confidence_alert_threshold <= 1.0:
        raise ValueError(
            "stale_confidence_alert_threshold must be in [0.0, 1.0], "
            f"got {stale_confidence_alert_threshold}"
        )
    if stale_confidence_alert_threshold < c_min:
        # Stale alert window is ``[c_min, threshold)``; an inverted pair
        # would silently disable the alert.
        raise ValueError(
            "stale_confidence_alert_threshold must be >= c_min "
            f"(got threshold={stale_confidence_alert_threshold}, "
            f"c_min={c_min})"
        )


class ProceduralFacadeMixin:
    """Procedural-tier methods for :class:`MemoryFacade`.

    Expects the host class to provide ``self._agent_id``,
    ``self._episodic`` (an initialised :class:`EpisodicMemory`),
    ``self._lambda_per_day``, ``self._c_min``,
    ``self._stale_alert_threshold``, and ``self._require_initialised()``.
    """

    _agent_id: str
    _episodic: EpisodicMemory
    _lambda_per_day: float
    _c_min: float
    _stale_alert_threshold: float

    def _require_initialised(self) -> None: ...  # pragma: no cover — host

    async def store_procedure(
        self,
        key: str,
        content: str,
        *,
        confidence: float,
        expires_at: float | None = None,
    ) -> None:
        """Persist a procedural-tier entry under *key*.

        Procedural rows are stored as episodes tagged
        ``procedure:{key}`` with the supplied ``confidence`` mapped onto
        both ``importance`` (so the eviction hybrid score sees it) and
        the dedicated ``confidence`` column added by migration v6 (so
        the read-time decay clock has a stable base value independent
        of importance).

        PR 5: when an entry with the same ``key`` already exists for
        this agent, this method calls
        ``episodic_procedural.refresh_confidence`` to reset
        ``confidence = 1.0`` and stamp ``last_validated_at = now`` on
        the existing rows — implementing the RFC 0008 §G "Confidence
        refresh on successful reuse" contract.  ``content`` is currently
        a no-op when the key already exists; rewriting the body lands
        when the procedural tier gains an UPDATE path (PR 6+).
        """
        self._require_initialised()
        if not key or not key.strip():
            raise ValueError("key must not be empty")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {confidence}"
            )
        db = self._episodic._ensure_db()  # noqa: SLF001 — facade owns the connection
        # Refresh path: existing key → reset confidence + last_validated.
        refreshed = await _refresh_confidence(db, self._agent_id, key)
        if refreshed:
            return
        context: dict[str, Any] = {"procedure_key": key}
        if expires_at is not None:
            context["expires_at"] = expires_at
        episode_id = await self._episodic.store_episode(
            summary=content,
            context=context,
            importance=confidence,
            tags=[f"procedure:{key}"],
        )
        # Stamp the dedicated confidence column on the freshly-inserted
        # row so the decay clock has a base value below 1.0 when needed.
        await db.execute(
            "UPDATE episodes SET confidence = ?, last_validated_at = ? "
            "WHERE id = ? AND agent_id = ?",
            (confidence, time.time(), episode_id, self._agent_id),
        )
        await db.commit()

    async def retrieve_procedures(
        self,
        query: str = "",
        *,
        limit: int = 10,
    ) -> list[ProcedureRecallEntry]:
        """Return procedural entries with read-time confidence decay applied.

        Wraps :func:`agents.memory.episodic_procedural.recall_procedures`
        so callers consume the procedural tier through the same facade
        boundary as the episodic tier.  Decay parameters come from the
        facade constructor (i.e. the agent's ``config/agents.yaml``
        ``memory.procedural_memory`` block).

        Emits a ``stale_memory_injection`` structured log for each
        admitted entry whose decayed confidence falls in
        ``[c_min, stale_confidence_alert_threshold)`` per RFC 0008
        Open Question 5 — execution is not blocked, the entry is still
        returned to the caller.
        """
        self._require_initialised()
        entries = await _recall_procedures(
            self._episodic._ensure_db(),  # noqa: SLF001
            self._agent_id,
            query=query,
            limit=limit,
            c_min=self._c_min,
            lambda_per_day=self._lambda_per_day,
        )
        for entry in entries:
            if entry.decayed_confidence < self._stale_alert_threshold:
                # Structured log — the orchestrator-side log ingestion
                # path (RFC 0019 PR 4 LogServiceServer) increments the
                # ``orchestrator.memory.stale_memory_injection`` counter
                # when it sees this event.  Field names are part of the
                # log contract and must match the Go-side parser.
                logger.warning(
                    "stale_memory_injection",
                    extra={
                        "metric": "stale_memory_injection",
                        "agent_id": self._agent_id,
                        "key": entry.key,
                        "decayed_confidence": entry.decayed_confidence,
                        "base_confidence": entry.base_confidence,
                        "c_min": self._c_min,
                        "stale_threshold": self._stale_alert_threshold,
                    },
                )
        return entries


__all__ = [
    "ProceduralFacadeMixin",
    "procedural_kwargs_from_config",
    "validate_decay_params",
]


def procedural_kwargs_from_config(memory_cfg: dict) -> dict:
    """Project ``memory.procedural_memory`` config into MemoryFacade kwargs.

    Extracted so :class:`agents.base.BaseAgent.initialize_memory` can
    fan the optional config block into the facade constructor in one
    call site rather than three branching ``proc_cfg.get`` lookups.
    Defaults track the ``DEFAULT_*`` constants exported by
    :mod:`agents.memory.decay`.
    """
    from .decay import (
        DEFAULT_C_MIN,
        DEFAULT_LAMBDA_PER_DAY,
        DEFAULT_STALE_CONFIDENCE_ALERT_THRESHOLD,
    )

    proc_cfg = memory_cfg.get("procedural_memory") or {}
    return {
        "lambda_per_day": float(
            proc_cfg.get("lambda_per_day", DEFAULT_LAMBDA_PER_DAY)
        ),
        "c_min": float(proc_cfg.get("c_min", DEFAULT_C_MIN)),
        "stale_confidence_alert_threshold": float(
            proc_cfg.get(
                "stale_confidence_alert_threshold",
                DEFAULT_STALE_CONFIDENCE_ALERT_THRESHOLD,
            )
        ),
    }
