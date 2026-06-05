"""Shared RFC 0031 §D recall-filter helpers.

Single source of truth for the ``sessions=`` parameter shape used by
the four persona-memory tiers (``episodes`` / ``relationships`` /
``facts`` / ``notes``).  Centralised so a future refactor cannot drift
the predicate across tiers — when each tier owned its own predicate
the F-3 risk was that one tier silently lost the ``legacy`` carve-out
or the empty-list guard while the others kept it.

Two functions:

* :func:`_resolve_session_list` — validate ``sessions``, extend with
  the always-visible ``legacy`` carve-out, and return either a
  resolved list of session ids or ``None`` for the ``"*"`` "no filter"
  sentinel.  Call once at the public-API boundary; pass the resolved
  list down through the recall helpers.
* :func:`session_in_clause` — given a resolved list (or ``None``) and
  a column reference, return the ``" AND col IN (?, ?, ...)"`` SQL
  fragment + params to append to a query.  ``None`` → empty fragment.

The carve-out (``legacy`` always visible in modes ``None`` / list) is
load-bearing for the "ship Phase 2 with no backfill" property: pre-RFC
rows persist with ``session_id = 'legacy'`` and remain visible from
every session.

Where resolution happens (PR 451 deep-review L2 follow-up):
:func:`_resolve_session_list` may be called either at the **facade**
layer or at the **tier** layer depending on whether the recall path
holds its own per-instance active-session snapshot.

* When the recall implementation is a class method that already owns
  an ``_active_session_id`` (e.g. :meth:`EpisodicMemory.recall`), the
  facade is a pass-through — it forwards ``sessions`` unchanged and
  the tier calls :func:`_resolve_session_list` against its own
  snapshot.  This is the
  :meth:`~agents.memory.facade.MemoryStore.retrieve_relevant` shape.
* When the recall implementation is a free function with no session
  state (e.g. :func:`agents.memory.episodic_procedural.recall_procedures`),
  the facade resolves with its own ``_session_id`` snapshot and
  passes the resulting ``session_list`` down to the free function.
  This is the
  :meth:`~agents.memory.facade_procedural.ProceduralFacadeMixin.retrieve_procedures`
  shape.

Both shapes produce equivalent behaviour today because
:meth:`MemoryStore.__init__` resolves :envvar:`PERSATRIX_SESSION_ID`
into its ``_session_id`` and into the embedded
:attr:`EpisodicMemory._active_session_id` from the same env-var with
no intervening await — pinned by
``test_session_recall_default_path.py::TestFacadeAndTierSessionSnapshotsAgreeOnConstruction``.
A future fifth read method should pick the shape that matches whether
its leaf recall holds its own snapshot; both are correct.

Internal helper (leading underscore module name); callers inside
:mod:`agents.memory` import the public names directly.
"""

from __future__ import annotations

from typing import Final

from ..session_id import LEGACY_SESSION_ID, current_session_id

__all__ = [
    "SESSIONS_ALL",
    "_resolve_session_list",
    "session_in_clause",
    "session_in_predicate",
]

#: The ``sessions="*"`` sentinel — CLI/debug mode only.  The
#: persona-runtime default context path is pinned in PR 4 never to
#: reach this value (`RFC 0031 §Security Considerations
#: <../../docs/rfcs/0031-per-session-namespacing-channels.md#security-considerations>`_).
SESSIONS_ALL: Final[str] = "*"


def _resolve_session_list(
    sessions: list[str] | str | None,
    active_session_id: str,
) -> list[str] | None:
    """Resolve a ``sessions=`` argument into the SQL-side filter list.

    Returns ``None`` for the ``"*"`` "no filter" sentinel; otherwise a
    list of session ids extended with :data:`LEGACY_SESSION_ID` for
    the always-visible carve-out.

    Call-time active session (ISSUE-0081): for the ``sessions=None``
    default path the active session is resolved as
    ``current_session_id() or active_session_id`` — a per-request
    ``session_scope`` (task-local ContextVar) wins, and the
    construction-time ``active_session_id`` snapshot passed by the tier
    is the fallback seed.  This is the single seam that makes **every**
    tier's default recall (episodes / relationships / facts / notes /
    procedures) honour the per-conversation scope without each call site
    re-implementing the precedence — the same single-source-of-truth
    rationale this module exists for.  When no scope is active the
    snapshot is used verbatim, so behaviour is unchanged for the
    single-session CLI / test / boot paths.

    De-duplication: if :data:`LEGACY_SESSION_ID` is already present in
    the resolved list (either as the active session under
    ``sessions=None`` or supplied explicitly in the list form), the
    carve-out is not appended a second time.  Keeps placeholder count
    minimal and avoids tripping a hypothetical future "distinct ids"
    assertion.  Pinned at :class:`TestResolveSessionListDefault` /
    :class:`TestResolveSessionListExplicitList` in
    :file:`tests/unit/python/test_session_id_session_filter.py`.

    Raises
    ------
    ValueError
        ``sessions=[]`` — §D rejects the silent legacy-only collapse.
    TypeError
        ``sessions`` is not ``None``, ``"*"``, or a list.
    """
    if sessions == SESSIONS_ALL:
        return None
    if sessions is None:
        ids: list[str] = [current_session_id() or active_session_id]
    elif isinstance(sessions, list):
        if not sessions:
            raise ValueError(
                "sessions must be None, '*', or a non-empty list",
            )
        ids = list(sessions)
    else:
        raise TypeError(
            f"sessions must be None, '*', or list[str]; got {type(sessions).__name__}",
        )
    if LEGACY_SESSION_ID not in ids:
        ids = [*ids, LEGACY_SESSION_ID]
    return ids


def session_in_predicate(
    session_list: list[str] | None,
    *,
    column: str,
) -> tuple[str, list[str]]:
    """Build the bare ``"col IN (?, ?, ...)"`` predicate + params.

    No leading ``" AND "`` — :func:`session_in_clause` adds that for the
    common "append after an existing WHERE predicate" case, while
    callers that embed the session filter inside a larger boolean group
    (e.g. ``_notes_recall._notes_session_clause``, which ORs it with a
    contact-topic carve-out) consume this shape directly rather than
    string-surgering the prefix back off.  ``session_list=None`` →
    ``("", [])`` (no filter — ``"*"`` mode).

    SECURITY: ``column`` is interpolated directly into the returned
    SQL fragment via f-string — it must be a **trusted internal
    literal** (a column or qualified ``alias.column`` reference fixed
    at the call site), never user input.  Every in-tree caller passes
    a string constant (``"session_id"`` / ``"n.session_id"`` /
    ``"e.session_id"``); a pin in
    :file:`tests/unit/python/test_session_id_session_filter.py` asserts
    the verbatim-interpolation contract.  If this helper is ever
    re-exported beyond :mod:`agents.memory`, gate ``column`` against a
    known-good set before relaxing the contract.
    """
    if session_list is None:
        return "", []
    placeholders = ",".join("?" for _ in session_list)
    return f"{column} IN ({placeholders})", list(session_list)


def session_in_clause(
    session_list: list[str] | None,
    *,
    column: str,
) -> tuple[str, list[str]]:
    """Build the ``" AND col IN (?, ?, ...)"`` fragment + params.

    ``session_list=None`` → ``("", [])`` (no filter — ``"*"`` mode);
    a resolved list → an IN-clause with one placeholder per id.  Thin
    wrapper over :func:`session_in_predicate` — the leading ``" AND "``
    is the only difference, so the two can never drift on the IN-clause
    shape (pinned in
    :file:`tests/unit/python/test_session_id_session_filter.py`).  See
    that helper for the ``column`` interpolation security contract.
    """
    pred, params = session_in_predicate(session_list, column=column)
    if not pred:
        return "", []
    return f" AND {pred}", params
