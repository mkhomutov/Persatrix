"""Shared rows for the ``commitments`` migration tests (RFC 0021 Phase 2
PR 1).

``test_commitments_migration.py`` and ``test_commitments_constraints.py``
both write a row of each kind the table accepts, through one insert.  The
pattern mirrors ``_close_path_test_helpers.py`` — a private module sibling
to the test files.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

__all__ = ["COMMITMENT", "DIRECTIVE", "insert"]

# A row of each kind the table accepts.  Every refusal case in
# ``test_commitments_constraints.py`` starts from one of these and changes
# one column.
COMMITMENT: dict[str, Any] = {
    "id": "c-1",
    "agent_id": "ember-owl",
    "kind": "commitment",
    "description": "send Bob the design notes",
    "due_at": 2_000.0,
    "target_party": "bob",
    "created_at": 1_000.0,
    "state": "open",
}
DIRECTIVE: dict[str, Any] = {
    "id": "d-1",
    "agent_id": "ember-owl",
    "kind": "directive",
    "description": "do not post the launch date in #general",
    "created_at": 1_000.0,
    "issuer_id": "alice",
    "issuing_channel_id": "ops",
    "channel_authority": "operational",
    "issuer_authority": "directive",
    "scope": "general",
    "directive_status": "pending",
    "polarity": "do_not",
    "action_class": "publish_channel",
    "target": "general",
    "graph_version": 3,
}


async def insert(db: aiosqlite.Connection, row: dict[str, Any]) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    await db.execute(
        f"INSERT INTO commitments ({cols}) VALUES ({marks})",  # noqa: S608 — test-owned keys
        tuple(row.values()),
    )
