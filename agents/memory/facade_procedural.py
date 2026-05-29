"""Procedural-tier mixin for :class:`agents.memory.facade.MemoryStore`.

Extracted from ``facade.py`` to keep that file under the 500-line repo
cap once the RFC 0008 PR 5 procedural-tier surface (decay knobs +
``store_procedure`` refresh + ``retrieve_procedures`` stale alert) was
added.  Lives as a mixin rather than a free-function module because the
methods need to share the facade's per-process ``EpisodicMemory``
connection and ``_require_initialised`` lifecycle gate.

The companion module :mod:`agents.memory.episodic_procedural` owns the
SQL; this mixin owns the *facade* concerns: arg validation against the
configured ``c_min``/``lambda_per_day``/stale-threshold knobs.  The
``stale_memory_injection`` structured log is emitted from
:func:`agents.memory.episodic_procedural.recall_procedures` (PR 6b
relocation per PR 5 R2 Mi2 — keeps the stale-detection + log together
with the row that triggers it).
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any

from ..session_id import current_session_id
from ._principal_filter import resolve_active_principal
from ._session_filter import _resolve_session_list
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

# PR 6b (PR 5 R2 M1): procedural ``key`` is interpolated into a SQL
# LIKE pattern (after meta-char escaping) and renders into structured
# logs (``stale_memory_injection``).  Restrict to a conservative
# ASCII alphabet so a future SQL or log-pipeline change cannot be
# blindsided by an exotic Unicode key.  This is a **breaking change**
# relative to v0.2.x — any existing caller passing keys outside this
# alphabet will start raising ``ValueError`` at the facade boundary.
_PROCEDURE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_PROCEDURE_KEY_MAX_LEN = 256


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
    """Procedural-tier methods for :class:`MemoryStore`.

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
    # RFC 0031 Phase 1: facade-level construction-time default for the
    # operator-namespace tag (see :class:`agents.memory.facade.MemoryStore`).
    _session_id: str
    # ISSUE-0081 PR 3: facade-level tenant snapshot (same source).
    _principal_id: str

    def _require_initialised(self) -> None: ...  # pragma: no cover — host

    async def store_procedure(
        self,
        key: str,
        content: str,
        *,
        confidence: float,
        expires_at: float | None = None,
        session_id: str | None = None,
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
        refresh on successful reuse" contract.

        Refresh-path discards (PR #225 review S4): on the refresh path
        **both** the supplied ``content`` *and* the supplied
        ``confidence`` arguments are silently discarded — the existing
        row's body is preserved and confidence is unconditionally
        reset to ``1.0``.  This is intentional for PR 5: the procedural
        tier does not yet have an UPDATE path, and "successful reuse"
        per RFC 0008 §G is defined as a full revalidation
        (``c_t = 1.0``).  Callers who need to *downgrade* an existing
        procedure's confidence (e.g. failure-driven decay) must call
        ``refresh_confidence`` directly, or wait for the procedural
        UPDATE path landing in PR 6+.  The signature still accepts
        ``confidence`` because it is required on the insert path.
        ``expires_at`` is similarly only honoured on insert (PR #225
        review L1: no consumer reads it yet — vestige of PR 2 stub).
        """
        self._require_initialised()
        _validate_procedure_key(key)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {confidence}"
            )
        db = self._episodic._ensure_db()  # noqa: SLF001 — facade owns the connection
        # Refresh path: existing key → reset confidence + last_validated.
        # ISSUE-0081 PR 3: scope the refresh to the active tenant (symmetric
        # with ``retrieve_procedures``) so a second tenant re-storing the
        # same key neither refreshes the first tenant's row nor loses its
        # own write to the refresh short-circuit (review follow-up).
        refreshed = await _refresh_confidence(
            db, self._agent_id, key,
            principal_id=resolve_active_principal(self._principal_id),
        )
        if refreshed:
            return
        context: dict[str, Any] = {"procedure_key": key}
        if expires_at is not None:
            context["expires_at"] = expires_at
        # RFC 0031 Phase 1: thread session_id; caller passes ``None`` to
        # inherit the facade's construction-time default (see
        # ``MemoryStore.__init__``).  Procedural rows live in the same
        # ``episodes`` table as observations, so the session-tag column
        # is shared.
        episode_id = await self._episodic.store_episode(
            summary=content,
            context=context,
            importance=confidence,
            tags=[f"procedure:{key}"],
            # ISSUE-0081: call-time default — a per-request
            # ``session_scope`` wins over the construction snapshot.
            session_id=(
                session_id
                if session_id is not None
                else (current_session_id() or self._session_id)
            ),
            # RFC 0031 PR 4 follow-up F2: procedural rows live in the
            # same ``episodes`` table as observations; pin the counter
            # surface so dashboards can split the two write kinds.
            surface="procedure",
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
        now: float | None = None,
        sessions: list[str] | str | None = None,
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
        returned to the caller.  PR 6b (PR 5 R2 Mi2): the log is
        emitted from inside ``recall_procedures`` itself so the
        decision lives next to the decayed-confidence value that
        triggers it.

        ``now`` (PR 6b, PR 5 R1 L4): override the read-time clock for
        deterministic tests.  When ``None`` (the default) the helper
        uses :func:`time.time`.

        ``sessions`` (RFC 0031 Phase 2 PR 4 — OQ #4 back-compat
        extension): same §D shape as
        :meth:`MemoryStore.retrieve_relevant`.  ``None`` resolves to the
        facade's construction-time ``_session_id`` plus the ``legacy``
        carve-out; an explicit list still includes ``legacy``; ``"*"``
        bypasses the filter (CLI / debug); ``[]`` is :class:`ValueError`.
        Procedural rows live in the same ``episodes`` table as
        observations, so the §D predicate is applied verbatim.
        """
        self._require_initialised()
        session_list = _resolve_session_list(sessions, self._session_id)
        return await _recall_procedures(
            self._episodic._ensure_db(),  # noqa: SLF001
            self._agent_id,
            query=query,
            limit=limit,
            c_min=self._c_min,
            lambda_per_day=self._lambda_per_day,
            stale_threshold=self._stale_alert_threshold,
            now=now,
            session_list=session_list,
            principal_id=resolve_active_principal(self._principal_id),
        )


def _validate_procedure_key(key: str) -> None:
    """Validate *key* against :data:`_PROCEDURE_KEY_PATTERN`.

    PR 6b (PR 5 R2 M1): centralised so the rule is stated once and the
    error message is consistent across the refresh + insert paths.
    """
    if not isinstance(key, str) or not key:
        raise ValueError("key must not be empty")
    if len(key) > _PROCEDURE_KEY_MAX_LEN:
        raise ValueError(
            f"key must be ≤ {_PROCEDURE_KEY_MAX_LEN} chars, got {len(key)}"
        )
    if not _PROCEDURE_KEY_PATTERN.fullmatch(key):
        raise ValueError(
            "key must match ^[A-Za-z0-9._-]+$ "
            f"(PR 6b breaking change — see CHANGELOG); got {key!r}"
        )


__all__ = [
    "ProceduralFacadeMixin",
    "procedural_kwargs_from_config",
    "validate_decay_params",
]


def procedural_kwargs_from_config(memory_cfg: dict) -> dict:
    """Project ``memory.procedural_memory`` config into MemoryStore kwargs.

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
