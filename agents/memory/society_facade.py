"""Society-tier mixin for :class:`agents.memory.store.MemoryStore` (RFC 0029).

RFC 0029 draws the personal/society storage boundary behind the
``MemoryStore`` facade.  *Personal-tier* methods (episodes, notes, facts,
bonds-self, commitments) hit per-agent SQLite.  *Society-tier* methods —
the cross-agent shared pools and inbound-trust graph — hit Postgres when
a ``society_dsn`` is configured.

Phase 1 (v0.3.2) ships **single-agent mode only**: there is no Postgres
backend, no ``asyncpg`` dependency.  The society-tier method *signatures*
ship now so the v0.4.0 boundary is frozen (RFC 0029 §C), but every body
raises the :class:`SocietyBackendUnavailable` hierarchy.  The Postgres
backend lands in RFC 0029 Phase 3 (v0.4.0).

Lives in its own module — not on :class:`MemoryStore` directly — to keep
``store.py`` under the repo's 500-line file cap, mirroring the
``facade_procedural`` / ``shared_pool_facade`` mixin precedent.
"""

from __future__ import annotations

from typing import Any

#: The ``config/agents.yaml`` key that enables the society tier.  Named
#: verbatim in every :class:`SocietyBackendUnavailable` message so an
#: operator who hits one knows exactly which knob to set.
SOCIETY_DSN_CONFIG_KEY = "memory.society.dsn"


# ─── Society backend exception hierarchy ────────────────────────


class SocietyBackendUnavailable(RuntimeError):  # noqa: N818 — RFC 0029 §C vocabulary
    """Abstract base — a society-tier call could not reach a backend.

    Catch-all ``except SocietyBackendUnavailable`` covers both the
    intentional single-agent mode and a transient outage; the two
    concrete subclasses distinguish *why* per RFC 0029 §C.
    """


class SocietyDisabled(SocietyBackendUnavailable):  # noqa: N818 — RFC 0029 §C vocabulary
    """The society tier is intentionally disabled — single-agent mode.

    Raised when ``society_dsn`` is ``None``.  This is the normal v0.3.2
    deployment shape: one binary, one SQLite file, no shared store.
    """


class SocietyTransientError(SocietyBackendUnavailable):
    """The society tier is configured but the backend is unreachable.

    Raised when a ``society_dsn`` is set.  In Phase 1 there is no
    Postgres backend at all, so a configured DSN always surfaces here —
    distinct from :class:`SocietyDisabled` so a caller that *expects* a
    society store can tell "not connected" from "not configured".
    """


# ─── Society-tier mixin ─────────────────────────────────────────


class SocietyFacadeMixin:
    """Society-tier methods for :class:`MemoryStore`.

    Every method raises in Phase 1 — the surface is frozen, the backend
    is not.  Expects the host class to provide ``_society_dsn`` (set in
    :meth:`MemoryStore.__init__` from ``StoreConfig.society_dsn``).
    """

    _society_dsn: str | None

    def _society_unavailable(self) -> SocietyBackendUnavailable:
        """Build the exception for the host's current society mode.

        ``society_dsn is None`` → :class:`SocietyDisabled` (intentional);
        a configured DSN → :class:`SocietyTransientError` (no Phase-1
        backend).  Both name :data:`SOCIETY_DSN_CONFIG_KEY` so the
        operator sees the actionable config path.
        """
        if self._society_dsn is None:
            return SocietyDisabled(
                "society tier is disabled (single-agent mode) — set "
                f"{SOCIETY_DSN_CONFIG_KEY} in config/agents.yaml to enable "
                "it; the Postgres society backend lands in RFC 0029 Phase 3 "
                "(v0.4.0)",
            )
        return SocietyTransientError(
            f"society tier is configured ({SOCIETY_DSN_CONFIG_KEY} is set) "
            "but unreachable — RFC 0029 Phase 1 ships single-agent mode "
            "only; the Postgres society backend lands in Phase 3 (v0.4.0)",
        )

    async def read_pool(
        self, pool: str, *, min_confidence: float | None = None,
    ) -> list[Any]:
        """Read entries from a cross-agent shared pool (RFC 0029 §C).

        Society-tier — needs the Postgres ``pool_entries`` table.  Phase 3
        narrows the return type to ``list[PoolEntry]``.
        """
        raise self._society_unavailable()

    async def query_inbound_trust(
        self, threshold: float = 0.7,
    ) -> list[Any]:
        """Return agents whose trust in *this* agent is ≥ *threshold*.

        Society-tier — a cross-agent query over the Postgres
        ``bonds_inbound`` table.  Phase 3 narrows the return type to
        ``list[InboundTrust]``.
        """
        raise self._society_unavailable()


__all__ = [
    "SOCIETY_DSN_CONFIG_KEY",
    "SocietyBackendUnavailable",
    "SocietyDisabled",
    "SocietyFacadeMixin",
    "SocietyTransientError",
]
