"""RFC 0031 Phase 2 PR 4 — facade-layer cross-session isolation.

Integration pin for the §D recall contract at the
:class:`~agents.memory.MemoryStore` facade boundary.  Phase 2 PR 2 / PR 3
filtered recall at the *tier* level; PR 4 threads the same ``sessions=``
parameter through the facade so callers that never bypass it — the
task-agent / sub-agent path, the shared-pool consumer — get the
session-scoped default for free.

What this file pins, separately from
:file:`tests/unit/python/test_episodic_session_scope.py`,
:file:`tests/unit/python/test_relationship_session_scope.py`, and
:file:`tests/unit/python/test_facts_session_scope.py` (which exercise
the tier APIs directly):

* The full persona recall path is session-scoped end to end through
  the facade — a facade constructed under ``PERSATRIX_SESSION_ID=run-b``
  does NOT see ``run-a`` writes via ``retrieve_relevant`` or
  ``retrieve_procedures``.
* The ``legacy`` carve-out is preserved through the facade — a row
  tagged ``session_id='legacy'`` (the column default; every pre-RFC
  row) is visible from every session via the facade.
* The ``sessions="*"`` debug sentinel surfaces every session via the
  facade (the CLI / debug-only path).

This is the test the PR plan calls the "F-3 reproduction at the
facade level" — a second run that reuses the same channel + user but
runs under a different ``PERSATRIX_SESSION_ID`` does not inherit
persona memory from the prior run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.memory.facade import MemoryStore


@pytest.fixture
async def facade_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a ``MemoryStore`` keyed to a named ``PERSATRIX_SESSION_ID``.

    Returns an async factory ``(session_id, agent_id) -> MemoryStore``.
    All stores share the same ``db_path`` so writes under one session
    are visible to recall under another (the cross-run state-bleed
    surface this test exists to close).
    """
    db_path = tmp_path / "shared.db"
    stores: list[MemoryStore] = []

    async def _build(session_id: str, agent_id: str = "ember-owl") -> MemoryStore:
        monkeypatch.setenv("PERSATRIX_SESSION_ID", session_id)
        fac = MemoryStore(agent_id=agent_id, db_path=str(db_path))
        await fac.initialize()
        stores.append(fac)
        return fac

    yield _build
    for s in stores:
        await s.close()


# ─── retrieve_relevant ──────────────────────────────────────


class TestRetrieveRelevantCrossSessionIsolation:
    """``MemoryStore.retrieve_relevant`` respects the §D default."""

    async def test_run_b_does_not_recall_run_a_observations(
        self, facade_factory,
    ) -> None:
        # Write an observation under ``run-a``.
        fac_a = await facade_factory("run-a")
        await fac_a.store_observation("kayak on the lake")

        # A second store under ``run-b`` — same agent, same DB — must
        # NOT recall the ``run-a`` row.  This is the F-3 closer at the
        # facade boundary.
        fac_b = await facade_factory("run-b")
        results = await fac_b.retrieve_relevant("kayak")
        contents = [r.content for r in results]
        assert "kayak on the lake" not in contents, (
            "RFC 0031 §D F-3 closer: facade under run-b must not surface "
            "rows tagged session_id=run-a by default; "
            f"got {contents!r}"
        )

    async def test_legacy_rows_visible_from_every_session(
        self, facade_factory,
    ) -> None:
        # Write a row explicitly under ``legacy`` (the always-visible
        # carve-out — every pre-RFC row defaults to this column value).
        fac_legacy = await facade_factory("legacy")
        await fac_legacy.store_observation("kayak from before sessions")

        # Recall under ``run-a`` must still see it (the carve-out is
        # load-bearing for the "ship Phase 2 with no backfill" property).
        fac_a = await facade_factory("run-a")
        results = await fac_a.retrieve_relevant("kayak")
        contents = [r.content for r in results]
        assert "kayak from before sessions" in contents, (
            "RFC 0031 §D legacy carve-out: rows tagged session_id=legacy "
            "must be visible from every session via the facade; "
            f"got {contents!r}"
        )

    async def test_all_sentinel_surfaces_every_session(
        self, facade_factory,
    ) -> None:
        fac_a = await facade_factory("run-a")
        await fac_a.store_observation("kayak on the lake")
        fac_b = await facade_factory("run-b")
        await fac_b.store_observation("kayak in the river")

        results = await fac_b.retrieve_relevant("kayak", sessions="*")
        contents = {r.content for r in results}
        assert "kayak on the lake" in contents
        assert "kayak in the river" in contents


# ─── retrieve_procedures ────────────────────────────────────


class TestRetrieveProceduresCrossSessionIsolation:
    async def test_run_b_does_not_recall_run_a_procedures(
        self, facade_factory,
    ) -> None:
        fac_a = await facade_factory("run-a")
        await fac_a.store_procedure(
            "deploy.rollback", "run `make rollback`", confidence=0.9,
        )

        fac_b = await facade_factory("run-b")
        results = await fac_b.retrieve_procedures()
        keys = {r.key for r in results}
        assert "deploy.rollback" not in keys

    async def test_legacy_procedures_visible_from_every_session(
        self, facade_factory,
    ) -> None:
        fac_legacy = await facade_factory("legacy")
        await fac_legacy.store_procedure(
            "deploy.smoke", "curl health", confidence=0.9,
        )

        fac_a = await facade_factory("run-a")
        results = await fac_a.retrieve_procedures()
        keys = {r.key for r in results}
        assert "deploy.smoke" in keys
