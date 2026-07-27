"""Topic-subject enumeration for :class:`agents.memory.facts.FactStore`
(RFC 0026 topic-predicate amendment — RFC 0049 Phase 1 PR 1).

Split out of :mod:`agents.memory.facts` so the parent module stays
under the 500-line cap — same precedent as the :mod:`_facts_audit` /
:mod:`_facts_reinforce` / :mod:`_facts_erasure` splits.

:func:`topic_subjects_for_agent` returns the **distinct** subjects that
carry at least one live ``topic.*`` row, most-recently-asserted first.
It feeds recall seeding (:mod:`agents.persona_runtime.topic_seeds`):
the persona matches these canonical subjects against the inbound
stimulus and seeds :meth:`FactStore.recall` for the mentions — the
retrieval leg of the RFC 0049 scenario-2 capture path.

Scope discipline (PR 1 vs PR 2)
-------------------------------
The query applies the SAME agent / session §D-default / principal /
epoch filters as :meth:`FactStore.recall`.  PR 1 widens *capture* only;
the cross-room L2 *scope* widening is RFC 0049 PR 2 — until it lands, a
topic subject taught in another room must not seed here, or the recall
wall would be bypassed pre-gate.

The predicate filter enumerates :data:`TOPIC_PREDICATES` as an IN-list
(closed allowlist ⇒ equality set, no LIKE pattern) so the SQL cannot
drift wider than the vocabulary; the drift pin in
``test_fact_predicates.py`` holds the frozenset equal to the dotted
``topic.`` slice of the combined allowlist.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._epoch_filter import epoch_eq_clause
from ._principal_filter import principal_eq_clause
from ._session_filter import session_in_clause
from .fact_predicates import TOPIC_PREDICATES

if TYPE_CHECKING:
    import aiosqlite

__all__ = ["topic_subjects_for_agent"]


async def topic_subjects_for_agent(
    db: aiosqlite.Connection,
    *,
    agent_id: str,
    limit: int,
    session_list: list[str] | None,
    principal_id: str,
    epoch_id: str,
) -> list[str]:
    """Distinct live topic subjects, most-recently-asserted first.

    ``limit`` bounds the scan (recall seeding matches against a small,
    recent working set — an unbounded enumeration would make the
    per-event matching cost scale with total store size).  Superseded
    rows are excluded so a fully-retracted topic stops seeding; the
    ``rowid`` tiebreak keeps equal-instant ordering portable across
    hosts (the :meth:`FactStore.recall` golden-trace rationale).
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    sess_clause, sess_params = session_in_clause(
        session_list, column="session_id",
    )
    princ_clause, princ_params = principal_eq_clause(
        principal_id, column="principal_id",
    )
    epoch_clause, epoch_params = epoch_eq_clause(
        epoch_id, column="epoch_id",
    )
    predicates = sorted(TOPIC_PREDICATES)
    placeholders = ", ".join("?" for _ in predicates)
    sql = (
        "SELECT subject FROM facts "
        f"WHERE agent_id = ? AND predicate IN ({placeholders}) "
        "AND superseded_by IS NULL"
        f"{sess_clause}{princ_clause}{epoch_clause} "
        "GROUP BY subject "
        "ORDER BY MAX(asserted_at) DESC, MAX(rowid) DESC LIMIT ?"
    )
    async with db.execute(
        sql, (
            agent_id, *predicates, *sess_params, *princ_params,
            *epoch_params, limit,
        ),
    ) as cursor:
        rows = await cursor.fetchall()
    return [row[0] for row in rows]
