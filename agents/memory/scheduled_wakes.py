"""Per-agent SQLite cache for the ``scheduled_wakes`` table — RFC 0024 PR 2.

``config/agents.yaml`` is the canonical source of truth for every agent's
periodic timer set; this table exists so a restart mid-jitter-window
does not fire the timer immediately on resume.  Per
:doc:`RFC 0024 §OQ §1 <../../docs/rfcs/0024-event-driven-scheduling>`
the table is *rebuilt from config* on every startup — operators who
remove a timer from YAML see the orphan row deleted on the next bring-up.

The ``source`` column reserves the runtime-mutation hook (a future
``RegisterTimer()`` RPC, etc.) without shipping it in Phase 2.  Every
row written through :meth:`rebuild_from_config` is marked
``source='config'``; no API to insert a ``source='runtime'`` row exists
yet, and PR 2's tests pin that fact so a Phase 3+ change has to surface
the hook deliberately.  The schema-level ``CHECK (source IN ('config',
'runtime'))`` constraint codifies the two-state enum as the permanent
contract.

Multiple agents share the same ``data/memory.db`` (v0.3.x convention),
so every read/write filters on ``agent_id`` and the primary key is
``(agent_id, timer_id)``.  ``ScheduledWakesCache`` instances are
per-agent and own their cursor over the shared file.

.. note::
   **``next_fire_at_ms`` clock contract.** The column stores
   ``time.monotonic_ns() // 1_000_000`` per
   :doc:`RFC 0024 §C <../../docs/rfcs/0024-event-driven-scheduling>`
   (monotonic, to avoid wall-clock-jump drift over long uptime).
   Python guarantees monotonicity *within a single process*; cross-
   process stability is a *platform* property — current CPython on
   Linux uses ``clock_gettime(CLOCK_MONOTONIC)`` (system uptime),
   macOS uses ``mach_absolute_time`` (system uptime), Windows uses
   ``QueryPerformanceCounter`` (typically session/system uptime).
   Anchors therefore survive an agent-process restart on all three
   platforms today, which is what the "restart-mid-jitter-window"
   guarantee in the persona-init wiring relies on.

   **Failure mode on system reboot.** A reboot resets the monotonic
   epoch, so anchors written by the previous boot become meaningless
   (typically appear "far in the future" relative to the new boot's
   ``now``).  The loader in
   :mod:`agents.server_persona_timers` clamps the restored
   ``initial_delay`` into ``[_MIN_INTERVAL, interval + jitter_max]``
   so a meaningless anchor degrades to "fires within
   ``interval + jitter_max`` of restart" — indistinguishable from a
   fresh first-fire's maximum.  Bounded silent degradation, not
   correctness loss; pinned by
   ``test_scheduled_wakes_cache_wiring_anchors.TestStaleAnchorClamp``.

.. note::
   **Wiring status (PR 2.1):** this module's schema + class is wired
   into ``initialize_persona_agents`` via
   :mod:`agents.server_persona_timers`.  Persona agents declaring
   ``autonomy.timers`` open one cache per agent at start, rebuild
   from config (orphan cleanup), and pass saved anchors as
   ``initial_delay`` overrides downstream.  Caches are closed in
   :meth:`agents.server.AgentServer.stop` after the schedulers stop
   and before the gRPC server tears down.  PR 2 shipped this module
   dormant; PR 2.1 turned it on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiosqlite

logger = logging.getLogger(__name__)

__all__ = ["ScheduledWakeRow", "ScheduledWakesCache"]


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS scheduled_wakes (
    agent_id        TEXT NOT NULL,
    timer_id        TEXT NOT NULL,
    kind            TEXT NOT NULL,
    interval_ms     INTEGER,
    jitter_ms       INTEGER NOT NULL DEFAULT 0,
    next_fire_at_ms INTEGER NOT NULL DEFAULT 0,
    source          TEXT NOT NULL DEFAULT 'config'
                    CHECK (source IN ('config', 'runtime')),
    PRIMARY KEY (agent_id, timer_id)
)
"""

# Index speeds up ``list_timers`` for the multi-agent shared-db path —
# without it a 100-agent process pays a full table scan on every restart.
_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_scheduled_wakes_agent
    ON scheduled_wakes (agent_id)
"""


@dataclass(frozen=True)
class ScheduledWakeRow:
    """One row of the ``scheduled_wakes`` cache.

    Mirrors the column layout precisely.  ``interval_ms == None`` marks a
    one-shot timer (the dataclass surface RFC 0024 Phase 3+ may use; not
    exposed via ``agents.yaml`` in Phase 2 — the schema only carries
    ``interval_seconds``).
    """

    timer_id: str
    kind: str
    interval_ms: int | None
    jitter_ms: int = 0
    next_fire_at_ms: int = 0
    source: str = "config"


class ScheduledWakesCache:
    """Per-agent reader/writer over the shared ``scheduled_wakes`` table.

    Lifecycle: ``__init__`` → ``initialize()`` → use → ``close()``.
    ``initialize()`` creates the table on first call (idempotent across
    agents sharing the same SQLite file) and the per-agent-id index.
    """

    def __init__(self, *, db_path: str, agent_id: str) -> None:
        self._db_path = db_path
        self._agent_id = agent_id
        self._conn: aiosqlite.Connection | None = None

    @property
    def db_path(self) -> str:
        return self._db_path

    @property
    def agent_id(self) -> str:
        return self._agent_id

    async def initialize(self) -> None:
        """Open the connection and ensure the table + index exist.

        Idempotent — :func:`CREATE TABLE IF NOT EXISTS` keeps the call
        safe for a multi-agent process where every agent's cache races
        for the first-startup create.
        """
        if self._conn is not None:
            return
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute(_CREATE_TABLE_SQL)
        await self._conn.execute(_CREATE_INDEX_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None

    async def rebuild_from_config(
        self,
        rows: list[ScheduledWakeRow],
    ) -> None:
        """Replace this agent's cached rows with ``rows`` atomically.

        Per RFC 0024 §OQ §1 — ``agents.yaml`` is canonical, so any row
        not present in ``rows`` is deleted ("orphan cleanup").  Performed
        in a single transaction so a partial rebuild cannot leave the
        cache in a half-config state.
        """
        if self._conn is None:
            raise RuntimeError("ScheduledWakesCache used before initialize()")

        async with self._conn.cursor() as cur:
            await cur.execute("BEGIN")
            try:
                await cur.execute(
                    "DELETE FROM scheduled_wakes WHERE agent_id = ?",
                    (self._agent_id,),
                )
                if rows:
                    await cur.executemany(
                        """
                        INSERT INTO scheduled_wakes
                            (agent_id, timer_id, kind, interval_ms,
                             jitter_ms, next_fire_at_ms, source)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                self._agent_id,
                                row.timer_id,
                                row.kind,
                                row.interval_ms,
                                row.jitter_ms,
                                row.next_fire_at_ms,
                                row.source,
                            )
                            for row in rows
                        ],
                    )
                await self._conn.commit()
            except Exception:
                await self._conn.rollback()
                raise

        logger.info(
            "scheduled_wakes.rebuild: agent_id=%s count=%d",
            self._agent_id,
            len(rows),
        )

    async def list_timers(self) -> list[ScheduledWakeRow]:
        """Return every cached row for this agent."""
        if self._conn is None:
            raise RuntimeError("ScheduledWakesCache used before initialize()")
        async with self._conn.execute(
            """
            SELECT timer_id, kind, interval_ms, jitter_ms,
                   next_fire_at_ms, source
            FROM scheduled_wakes
            WHERE agent_id = ?
            ORDER BY timer_id
            """,
            (self._agent_id,),
        ) as cur:
            raw = await cur.fetchall()
        return [
            ScheduledWakeRow(
                timer_id=row[0],
                kind=row[1],
                interval_ms=row[2],
                jitter_ms=row[3],
                next_fire_at_ms=row[4],
                source=row[5],
            )
            for row in raw
        ]
