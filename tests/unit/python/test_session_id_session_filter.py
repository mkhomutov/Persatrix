"""Unit tests for :mod:`agents.memory._session_filter`.

The shared §D recall-filter helpers underpin every persona-memory
tier's ``sessions=`` parameter; the four tiers (``episodes`` /
``relationships`` / ``facts`` / ``notes``) drift apart if one tier
silently loses the ``legacy`` carve-out or the empty-list guard while
the others keep it.  The behavioural pins in
:mod:`tests.unit.python.test_episodic_session_scope` exercise the four
§D modes end-to-end through ``EpisodicMemory.recall`` /
``NoteStore.recall_notes``, but they reach the helpers only via the
``sessions=None`` / list / ``"*"`` / ``[]`` paths.

This module pins the helpers directly:

* :func:`_resolve_session_list` — every branch including the
  ``TypeError`` path that the public-API tests cannot reach without
  bypassing the public type signature.
* :func:`session_in_clause` — the SQL-fragment shape (the
  ``" AND col IN (?, ?, ...)"`` body and the ``None`` → ``("", [])``
  short-circuit) so a later refactor cannot quietly change the
  placeholder count, the leading-space convention, or the column-name
  interpolation.

PR 2 review carry-forward — the test_episodic_session_scope module
docstring referenced this filename before it existed; this module
closes that gap and gives the helpers stand-alone coverage.
"""

from __future__ import annotations

import pytest

from agents.memory._session_filter import (
    SESSIONS_ALL,
    _resolve_session_list,
    session_in_clause,
    session_in_predicate,
)
from agents.session_id import LEGACY_SESSION_ID

# ─── _resolve_session_list ──────────────────────────────────


class TestResolveSessionListDefault:
    """``sessions=None`` → active session plus the carve-out."""

    def test_none_returns_active_plus_legacy(self) -> None:
        assert _resolve_session_list(None, "run-a") == ["run-a", "legacy"]

    def test_none_with_legacy_active_does_not_duplicate(self) -> None:
        # When the active session IS the carve-out, the list collapses
        # to a single element rather than ``["legacy", "legacy"]`` — a
        # duplicate would be semantically harmless but would waste an
        # SQL placeholder and trip a future "distinct ids" assertion.
        assert _resolve_session_list(None, LEGACY_SESSION_ID) == [LEGACY_SESSION_ID]


class TestResolveSessionListExplicitList:
    """``sessions=[...]`` → named list extended with the carve-out."""

    def test_single_session_extended_with_legacy(self) -> None:
        assert _resolve_session_list(["run-a"], "ignored") == ["run-a", "legacy"]

    def test_multiple_sessions_preserved_in_order_then_legacy(self) -> None:
        assert _resolve_session_list(
            ["run-a", "run-b"], "ignored",
        ) == ["run-a", "run-b", "legacy"]

    def test_legacy_already_in_list_not_duplicated(self) -> None:
        assert _resolve_session_list(
            ["legacy", "run-a"], "ignored",
        ) == ["legacy", "run-a"]

    def test_active_session_ignored_when_list_explicit(self) -> None:
        # ``sessions=["run-b"]`` from an active ``run-a`` honours the
        # explicit list verbatim — the active session is the default
        # when ``sessions=None``, not a forced floor.
        assert _resolve_session_list(["run-b"], "run-a") == ["run-b", "legacy"]


class TestResolveSessionListStar:
    """``sessions="*"`` → ``None`` (no-filter sentinel)."""

    def test_star_returns_none(self) -> None:
        assert _resolve_session_list(SESSIONS_ALL, "run-a") is None

    def test_star_constant_value(self) -> None:
        # Pin the sentinel value so external callers / docs cannot
        # silently drift from the literal ``"*"`` used by every caller.
        assert SESSIONS_ALL == "*"


class TestResolveSessionListGuards:
    """``sessions=[]`` and bad-type inputs raise at the boundary."""

    def test_empty_list_raises_value_error(self) -> None:
        # §D rejects the silent legacy-only collapse — an empty list is
        # never "no constraint", so the helper raises instead of
        # quietly returning ``["legacy"]``.
        with pytest.raises(ValueError, match="non-empty list"):
            _resolve_session_list([], "run-a")

    def test_non_list_non_string_raises_type_error(self) -> None:
        # The public type signature is ``list[str] | str | None``; a
        # tuple / dict / int slips past static checking when callers
        # build the value dynamically.  The runtime guard fires here.
        with pytest.raises(TypeError, match="must be None, '\\*', or list\\[str\\]"):
            _resolve_session_list(("run-a",), "ignored")  # type: ignore[arg-type]

    def test_arbitrary_string_other_than_star_raises_type_error(self) -> None:
        # A bare string like ``"run-a"`` (forgetting the list wrapper)
        # is the most likely caller mistake — must raise loudly rather
        # than silently filter to nothing.
        with pytest.raises(TypeError, match="must be None, '\\*', or list\\[str\\]"):
            _resolve_session_list("run-a", "ignored")  # type: ignore[arg-type]


# ─── session_in_clause ──────────────────────────────────────


class TestSessionInClauseNoFilter:
    """``session_list=None`` → empty fragment + empty params."""

    def test_none_returns_empty_fragment(self) -> None:
        clause, params = session_in_clause(None, column="session_id")
        assert clause == ""
        assert params == []


class TestSessionInClauseListMode:
    """``session_list=[...]`` → leading-space ``" AND col IN (?, ...)"``."""

    def test_single_id_one_placeholder(self) -> None:
        clause, params = session_in_clause(["run-a"], column="session_id")
        assert clause == " AND session_id IN (?)"
        assert params == ["run-a"]

    def test_multiple_ids_one_placeholder_each(self) -> None:
        clause, params = session_in_clause(
            ["run-a", "legacy"], column="session_id",
        )
        assert clause == " AND session_id IN (?,?)"
        assert params == ["run-a", "legacy"]

    def test_fragment_starts_with_leading_space_and_and(self) -> None:
        # Every caller appends this fragment after an existing WHERE
        # clause that already has at least one predicate; the leading
        # space + ``AND`` is load-bearing for the resulting SQL to
        # parse.  Drop the space or the ``AND`` and every caller
        # explodes.
        clause, _ = session_in_clause(["run-a"], column="x")
        assert clause.startswith(" AND ")

    def test_column_name_is_interpolated_verbatim(self) -> None:
        # ``column`` is a trusted internal literal — see the
        # ``_session_filter.session_in_clause`` docstring.  The
        # qualified-name shape (``"n.session_id"``) flows through
        # unchanged so callers can mix table aliases freely.
        clause, _ = session_in_clause(["run-a"], column="n.session_id")
        assert "n.session_id" in clause

    def test_params_are_a_fresh_list_not_aliased(self) -> None:
        # If the helper returned the caller's input list by reference,
        # a caller mutating its own copy (or the helper's later
        # extension) would clobber the other.  ``list(session_list)``
        # in the helper enforces a defensive copy — pin it.
        input_list = ["run-a", "run-b"]
        _, params = session_in_clause(input_list, column="session_id")
        assert params == input_list
        assert params is not input_list


# ─── session_in_predicate ───────────────────────────────────


class TestSessionInPredicate:
    """``session_in_predicate`` is the bare predicate (no leading
    ``" AND "``) that :func:`session_in_clause` wraps.  Exposed so
    callers that embed the session filter inside a larger boolean group
    — e.g. ``_notes_recall._notes_session_clause`` (F-7 contact-note
    widening) — can reuse the IN-clause shape without string-surgering
    the ``" AND "`` prefix back off ``session_in_clause``'s output.
    """

    def test_none_returns_empty(self) -> None:
        pred, params = session_in_predicate(None, column="session_id")
        assert pred == ""
        assert params == []

    def test_list_has_no_leading_and(self) -> None:
        pred, params = session_in_predicate(["run-a"], column="session_id")
        assert pred == "session_id IN (?)"
        assert params == ["run-a"]

    def test_multiple_ids_one_placeholder_each(self) -> None:
        pred, params = session_in_predicate(
            ["run-a", "legacy"], column="session_id",
        )
        assert pred == "session_id IN (?,?)"
        assert params == ["run-a", "legacy"]

    def test_clause_is_predicate_with_and_prefix(self) -> None:
        # The two helpers must not drift: ``session_in_clause`` is
        # exactly ``" AND " + session_in_predicate`` for any list input.
        clause, c_params = session_in_clause(["run-a", "legacy"], column="x")
        pred, p_params = session_in_predicate(["run-a", "legacy"], column="x")
        assert clause == f" AND {pred}"
        assert c_params == p_params


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
