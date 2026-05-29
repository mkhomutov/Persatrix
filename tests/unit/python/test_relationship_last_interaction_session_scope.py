"""
Tests for the ``last_interaction_at`` session-scoping fix — RFC 0031
Phase 2 PR 5 deep-review follow-up to `ISSUE-0080
<../../../docs/issues/ISSUE-0080-relationship-recent-interactions-cross-session-leak.md>`_.

The original ISSUE-0080 fix session-filtered ``recent_interactions`` /
``interaction_count`` / ``first_interaction_at`` but read
``last_interaction_at`` straight from the ``relationships`` column.
``record_interaction``'s ``ON CONFLICT`` refreshes that column keyed on
the participant 4-tuple with **no** session predicate, so a cross-session
write bumps the first-seen (or ``legacy``) row's "Last seen" timestamp —
the ``MAX(created_at)`` twin of the ``first_interaction_at`` leak.  Both
:meth:`get_relationship_summary` and :meth:`get_all_relationships` now
derive ``last_interaction_at`` from the session-filtered ``interactions``
subquery.

Split into its own module (rather than appended to
:mod:`tests.unit.python.test_relationship_session_scope`) to keep that
file under the project's 500-line review-friendly cap — same precedent
as :mod:`tests.unit.python.test_notes_mutation_session_scope`.
"""

from __future__ import annotations

import contextlib

import pytest

from agents.memory.relationship import RelationshipMemory
from agents.session_id import SESSION_ID_ENV_VAR


class _SeqClock:
    """Deterministic clock so ``record_interaction`` timestamps are distinct.

    ``record_interaction`` stamps each row with ``time.time()``.  Two
    rapid real-clock calls can return equal floats, which would make a
    ``last_interaction_at`` assertion non-deterministic — patch
    :mod:`agents.memory.relationship_mutations`'s ``time`` reference with
    this monotonic stub instead.
    """

    def __init__(self, *values: float) -> None:
        self._it = iter(values)

    def time(self) -> float:
        return next(self._it)


@pytest.fixture
async def memory_at_run_a(monkeypatch: pytest.MonkeyPatch):
    """``RelationshipMemory`` constructed with ``PERSATRIX_SESSION_ID=run-a``."""
    monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
    mem = RelationshipMemory(agent_id="test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


class TestLastInteractionAtIsSessionScoped:
    """``last_interaction_at`` reflects only the active session's visible
    interactions, not the cross-session ON-CONFLICT bump on the column.
    """

    async def test_summary_excludes_foreign_session_timestamp(
        self, memory_at_run_a: RelationshipMemory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``run-b`` interaction bumps the ``run-a``-tagged row's
        column; ``get_relationship_summary`` must surface ``run-a``'s
        own latest visible interaction instead.
        """
        from agents.memory import relationship_mutations as _rm_mod

        monkeypatch.setattr(_rm_mod, "time", _SeqClock(100.0, 200.0))
        # First-seen under run-a → relationships row tagged "run-a".
        await memory_at_run_a.record_interaction(
            "peer-a", "task_delegation", session_id="run-a",
        )  # t=100.0
        # Cross-session interaction bumps the run-a row's column to 200.0.
        await memory_at_run_a.record_interaction(
            "peer-a", "task_delegation", session_id="run-b",
        )  # t=200.0
        summary = await memory_at_run_a.get_relationship_summary("peer-a")
        # Pre-fix: 200.0 (run-b's bump).  Post-fix: 100.0 (run-a's only
        # visible interaction).
        assert summary.last_interaction_at == 100.0, summary.last_interaction_at

    async def test_summary_none_when_no_visible_interaction(
        self, memory_at_run_a: RelationshipMemory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A visible (``legacy``) row whose only interaction is foreign
        surfaces ``last_interaction_at = None``.

        Distinguishes "derive from the filtered interactions subquery"
        from any half-fix that keeps reading the relationships column
        when the visible interaction set is empty.  ``update_trust``
        creates a ``legacy``-default row with no interaction (visible to
        ``run-a`` via the carve-out); its sole interaction is recorded
        under ``run-b``.
        """
        from agents.memory import relationship_mutations as _rm_mod

        # update_trust creates a legacy-tagged row with no interaction
        # and last_interaction_at = NULL.
        await memory_at_run_a.update_trust("peer-seed", 0.2, "seed")
        # A foreign-session interaction bumps the legacy row's column.
        monkeypatch.setattr(_rm_mod, "time", _SeqClock(500.0))
        await memory_at_run_a.record_interaction(
            "peer-seed", "task_delegation", session_id="run-b",
        )  # t=500.0 — invisible to run-a
        summary = await memory_at_run_a.get_relationship_summary("peer-seed")
        # Row visible (legacy carve-out), but no run-a/legacy interaction.
        assert summary.last_interaction_at is None, summary.last_interaction_at

    async def test_get_all_relationships_excludes_foreign_session_timestamp(
        self, memory_at_run_a: RelationshipMemory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """List-mode read derives ``last_interaction_at`` per-session too
        — it must not surface the cross-session column bump.
        """
        from agents.memory import relationship_mutations as _rm_mod

        monkeypatch.setattr(_rm_mod, "time", _SeqClock(100.0, 200.0))
        await memory_at_run_a.record_interaction(
            "peer-a", "task_delegation", session_id="run-a",
        )  # t=100.0
        await memory_at_run_a.record_interaction(
            "peer-a", "task_delegation", session_id="run-b",
        )  # t=200.0
        rels = await memory_at_run_a.get_all_relationships()
        peer_a = next(r for r in rels if r.other_participant_id == "peer-a")
        # Pre-fix: 200.0 (r.last_interaction_at column).  Post-fix: 100.0.
        assert peer_a.last_interaction_at == 100.0, peer_a.last_interaction_at


if __name__ == "__main__":
    with contextlib.suppress(SystemExit):
        pytest.main([__file__, "-v"])
