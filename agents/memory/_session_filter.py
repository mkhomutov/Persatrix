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

Internal helper (leading underscore module name); callers inside
:mod:`agents.memory` import the public names directly.
"""

from __future__ import annotations

from typing import Final

from ..session_id import LEGACY_SESSION_ID

__all__ = [
    "SESSIONS_ALL",
    "_resolve_session_list",
    "session_in_clause",
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
        ids: list[str] = [active_session_id]
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


def session_in_clause(
    session_list: list[str] | None,
    *,
    column: str,
) -> tuple[str, list[str]]:
    """Build the ``" AND col IN (?, ?, ...)"`` fragment + params.

    ``session_list=None`` → ``("", [])`` (no filter — ``"*"`` mode);
    a resolved list → an IN-clause with one placeholder per id.

    ``column`` is interpolated directly into the returned SQL fragment
    via f-string — it must be a **trusted internal literal** (a column
    or qualified ``alias.column`` reference fixed at the call site),
    never user input.  Every in-tree caller passes a string constant
    (``"session_id"`` / ``"n.session_id"`` / ``"e.session_id"``); a
    pin in :file:`tests/unit/python/test_session_id_session_filter.py`
    asserts the verbatim-interpolation contract.  If this helper is
    ever re-exported beyond :mod:`agents.memory`, gate ``column``
    against a known-good set before relaxing the contract.
    """
    if session_list is None:
        return "", []
    placeholders = ",".join("?" for _ in session_list)
    return f" AND {column} IN ({placeholders})", list(session_list)
