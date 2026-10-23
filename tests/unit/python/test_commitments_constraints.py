"""Tests for the check constraints of memory-store migration v20, the
``commitments`` table (RFC 0021 Phase 2 PR 1).

The constraints keep the two kinds of row apart (the Phase 2 plan's H-2):
a commitment has a due time and a state; a directive has a directive
status and an issuer.  Each refused row differs in one column from a row
the table accepts — the positive controls are tested too — and each test
asserts which named constraint refused it, so a refusal can only come
from that constraint.  Split from ``test_commitments_migration.py``,
which covers the table's shape, the upgrade and the guards.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from agents.memory._migration_commitments import (
    COMMITMENT_STATES,
    DIRECTIVE_STATUSES,
)
from agents.memory.episodic import EpisodicMemory

from ._commitments_migration_helpers import COMMITMENT, DIRECTIVE, insert

_BASES = {"commitment": COMMITMENT, "directive": DIRECTIVE}


# ─── Check constraints ──────────────────────────────────────


class TestCheckConstraints:
    async def test_well_formed_rows_accepted(self, memory: EpisodicMemory):
        """The positive controls every refusal below is measured from;
        a directive needs no ``due_at``."""
        db = memory._ensure_db()
        await insert(db, COMMITMENT)
        await insert(db, DIRECTIVE)
        await insert(db, {**DIRECTIVE, "id": "d-2", "due_at": 3_000.0})
        async with db.execute("SELECT COUNT(*) FROM commitments") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 3

    @pytest.mark.parametrize("state", COMMITMENT_STATES)
    async def test_every_state_accepted(
        self, memory: EpisodicMemory, state: str,
    ):
        await insert(memory._ensure_db(), {**COMMITMENT, "state": state})

    @pytest.mark.parametrize("status", DIRECTIVE_STATUSES)
    async def test_every_directive_status_accepted(
        self, memory: EpisodicMemory, status: str,
    ):
        await insert(
            memory._ensure_db(), {**DIRECTIVE, "directive_status": status},
        )

    async def test_vocabularies_are_the_plans(self):
        assert COMMITMENT_STATES == ("open", "fulfilled", "missed", "cancelled")
        # The RFC 0012 plan's seven, outside the commitment ``state``
        # constraint.
        assert DIRECTIVE_STATUSES == (
            "pending", "accepted", "adapted", "refused", "escalated",
            "superseded", "fulfilled",
        )

    @pytest.mark.parametrize(
        "kind, change, constraint",
        [
            ("commitment", {"due_at": None}, "commitments_kind_shape"),
            ("commitment", {"state": None}, "commitments_kind_shape"),
            (
                "commitment",
                {"directive_status": "pending"},
                "commitments_kind_shape",
            ),
            ("commitment", {"issuer_id": "alice"}, "commitments_kind_shape"),
            ("directive", {"state": "open"}, "commitments_kind_shape"),
            ("directive", {"directive_status": None}, "commitments_kind_shape"),
            ("directive", {"issuer_id": None}, "commitments_kind_shape"),
            ("commitment", {"kind": "expectation"}, "commitments_kind"),
            ("commitment", {"state": "done"}, "commitments_state"),
            (
                "directive",
                {"directive_status": "approved"},
                "commitments_directive_status",
            ),
            (
                "directive",
                {"conflict_failures": -1},
                "commitments_conflict_failures",
            ),
        ],
        ids=[
            "commitment-without-due_at",
            "commitment-without-state",
            "commitment-with-directive_status",
            "commitment-with-issuer",
            "directive-with-state",
            "directive-without-directive_status",
            "directive-without-issuer",
            "unknown-kind",
            "unknown-state",
            "unknown-directive_status",
            "negative-conflict_failures",
        ],
    )
    async def test_wrong_shape_refused(
        self,
        memory: EpisodicMemory,
        kind: str,
        change: dict[str, Any],
        constraint: str,
    ):
        db = memory._ensure_db()
        with pytest.raises(
            sqlite3.IntegrityError,
            match=rf"CHECK constraint failed: {constraint}$",
        ):
            await insert(db, {**_BASES[kind], **change})
