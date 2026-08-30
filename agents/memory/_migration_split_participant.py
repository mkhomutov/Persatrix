"""[ISSUE-0120] migration v17 — fold a human's pre-ISSUE-0119 **split**
relationship rows back into one person.

Split out of :mod:`agents.memory._migration_handlers` to keep that module
under the project's 500-line review cap, mirroring the v8–v16 splits.

**What & why.**  Until [ISSUE-0119], a human publishing into a group channel
reached the persona with no ``participant_type``, so the sender resolved to
the ``"agent"`` default (:mod:`agents.sender_type`) and the close path wrote
that room's trust, notes and identity onto an **agent-typed** relationship
row — while the same human's DM traffic wrote the correct **user-typed** row.
One person, two records, never merged.  The publish path now stamps the type
server-side so no new split can open; this migration repairs the ones already
on disk.

**The heuristic, and why it is safe enough to run automatically.**  The merge
has to know which agent-typed ids are really humans, and this database has no
registry to ask — the registry lives in the orchestrator, one process away.
The fingerprint used instead is *an id holding **both** a user-typed and an
agent-typed row for the same ``(agent, principal, epoch)``*: only the chat
path writes the user type, and that path is the human door, so a real agent
peer does not accumulate a user-typed row.  Rows without a user-typed twin
are left **untouched** — a genuine agent peer is never rewritten, so the
blast radius is exactly the bug's own footprint.

**Merge semantics** (the operator decisions this migration encodes):

* ``interaction_count`` — **sum**.  The two rows record genuinely distinct
  interactions with one person.
* ``last_interaction_at`` — **max** (``NULL`` counts as "never").
* ``trust_score`` — **interaction-weighted average**, because trust *is* an
  aggregate over interactions: weighting by how many interactions produced
  each score is the only merge that preserves its meaning, so a 2-interaction
  group row cannot outvote a 40-interaction DM row.  With no interactions on
  either side there is nothing to weight, and the user row's value stands.
* ``identity`` — :func:`~agents.memory.relationship_types.merge_identity`,
  applied **older-into-newer** by ``last_interaction_at`` so the live
  write-through's last-writer-wins scalar rule survives the fold.
* ``notes`` — the **newer** row's, not a concatenation: the column holds the
  latest *trust-change reason* (overwritten by every ``update_trust``), so
  joining two of them would manufacture a reason that never existed.

``principal_id`` and ``epoch_id`` are match axes, never merged across — the
tenant and run-isolation walls stay absolute (docs/memory-scope-axes.md).

**Operator-visible, not silent.**  Every fold emits one INFO line naming the
person and the resulting counts.  A rewrite of person records that nobody can
see afterwards is the wrong shape for this even when the heuristic is right.

Idempotency / partial-restore safety: a ``sqlite_master`` check
short-circuits a baseline without ``relationships``, and ``PRAGMA
table_info`` confirms the v11/v12/v13 columns this reads (``principal_id`` /
``epoch_id`` / ``identity``) landed first.  The transform is idempotent by
construction — the agent-typed row is **deleted** as part of each fold, so a
re-run finds no pair to merge and does nothing (which is also what keeps the
summed ``interaction_count`` from double-counting on a crash-replay between
this handler and the ``schema_version`` record).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

import aiosqlite

from .relationship_types import merge_identity

__all__ = ["_apply_migration_17", "merge_trust"]

logger = logging.getLogger(__name__)

#: The two peer types the fold runs between.  Local constants rather than an
#: import from ``agents.sender_type``: this migration reads a legacy on-disk
#: shape, and must keep reading it the same way if the live vocabulary ever
#: grows a third type.
_USER_TYPE = "user"
_AGENT_TYPE = "agent"


async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return await cursor.fetchone() is not None


async def _has_columns(db: aiosqlite.Connection, table: str, columns: set[str]) -> bool:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return columns <= {row[1] for row in await cursor.fetchall()}


async def _apply_migration_17(db: aiosqlite.Connection) -> None:
    """[ISSUE-0120]: fold split ``(id, "agent")`` rows into their
    ``(id, "user")`` twin.  See the module docstring for the full contract.

    No-op (clean return) when ``relationships`` is absent (partial-restore
    baseline) or the columns this reads have not landed — never crashing the
    migration chain.  Single tail ``db.commit()``; ``_apply_migrations``
    records ``schema_version`` after this returns.
    """
    if not await _table_exists(db, "relationships"):
        return
    if not await _has_columns(
        db, "relationships", {"principal_id", "epoch_id", "identity"},
    ):
        return

    # Every (persona, person, principal, epoch) tuple carrying BOTH types —
    # the split fingerprint. The self-join is the whole selection rule: a row
    # with no twin is not returned, so it is never touched.
    cursor = await db.execute(
        """
        SELECT u.participant_id, u.participant_type, u.other_participant_id,
               u.principal_id, u.epoch_id,
               u.trust_score, u.interaction_count, u.last_interaction_at,
               u.notes, u.identity,
               a.trust_score, a.interaction_count, a.last_interaction_at,
               a.notes, a.identity
        FROM relationships AS u
        JOIN relationships AS a
          ON  a.participant_id        = u.participant_id
          AND a.participant_type      = u.participant_type
          AND a.other_participant_id  = u.other_participant_id
          AND a.principal_id          = u.principal_id
          AND a.epoch_id              = u.epoch_id
          AND a.other_participant_type = ?
        WHERE u.other_participant_type = ?
        """,
        (_AGENT_TYPE, _USER_TYPE),
    )
    pairs = list(await cursor.fetchall())

    for row in pairs:
        await _fold_pair(db, row)

    if pairs:
        logger.info(
            "memory migration v17 [ISSUE-0120]: folded %d split person "
            "record(s) back onto the user-typed row",
            len(pairs),
        )
    await db.commit()


def merge_trust(
    user_trust: float, user_count: int, agent_trust: float, agent_count: int,
) -> float:
    """Interaction-weighted mean of the two rows' trust scores.

    Pure and exported for direct testing.  Falls back to ``user_trust`` when
    neither row records an interaction: with nothing to weight, the canonical
    (DM-written) row's value stands rather than an unweighted average of two
    scores that no interaction earned.
    """
    total = user_count + agent_count
    if total <= 0:
        return user_trust
    return (user_trust * user_count + agent_trust * agent_count) / total


async def _fold_pair(db: aiosqlite.Connection, row: Sequence[Any]) -> None:
    """Merge one agent-typed row into its user-typed twin and delete it."""
    (
        agent_id, self_type, other_id, principal_id, epoch_id,
        u_trust, u_count, u_last, u_notes, u_identity,
        a_trust, a_count, a_last, a_notes, a_identity,
    ) = row

    # Column defaults are nullable in the v4 schema; normalise before maths so
    # a legacy NULL cannot poison the merge.
    u_trust = u_trust if u_trust is not None else 0.5
    a_trust = a_trust if a_trust is not None else 0.5
    u_count = u_count or 0
    a_count = a_count or 0

    trust = merge_trust(u_trust, u_count, a_trust, a_count)
    count = u_count + a_count
    last = max(
        (t for t in (u_last, a_last) if t is not None), default=None,
    )

    # Which row is "newer" decides both the scalar-identity winner and the
    # surviving trust-change reason. A row that never recorded an interaction
    # loses to one that did; with both unset the user row stands.
    agent_is_newer = a_last is not None and (u_last is None or a_last > u_last)
    older, newer = (
        (u_identity, a_identity) if agent_is_newer else (a_identity, u_identity)
    )
    identity = merge_identity(
        json.loads(older) if older else {},
        json.loads(newer) if newer else {},
    )
    notes = a_notes if agent_is_newer else u_notes

    await db.execute(
        """
        UPDATE relationships
        SET trust_score=?, interaction_count=?, last_interaction_at=?,
            notes=?, identity=?
        WHERE participant_id=? AND participant_type=?
          AND other_participant_id=? AND other_participant_type=?
          AND principal_id=? AND epoch_id=?
        """,
        (
            trust, count, last, notes,
            json.dumps(identity) if identity else None,
            agent_id, self_type, other_id, _USER_TYPE, principal_id, epoch_id,
        ),
    )
    # Deleting the agent-typed row is what makes the fold idempotent: the
    # next run finds no pair, so the summed count cannot double.
    await db.execute(
        """
        DELETE FROM relationships
        WHERE participant_id=? AND participant_type=?
          AND other_participant_id=? AND other_participant_type=?
          AND principal_id=? AND epoch_id=?
        """,
        (agent_id, self_type, other_id, _AGENT_TYPE, principal_id, epoch_id),
    )
    logger.info(
        "memory migration v17 [ISSUE-0120]: %s + %s -> %s "
        "(trust %.3f, %d interactions, principal=%s epoch=%s)",
        agent_id, other_id, other_id, trust, count, principal_id, epoch_id,
    )
