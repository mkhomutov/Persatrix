"""Strict-equality principal recall predicate (ISSUE-0081 PR 3).

The principal-axis sibling of :mod:`agents.memory._session_filter`.
Single source of truth for the ``principal_id`` predicate shared by the
persona-memory tiers (``episodes`` / ``relationships`` / ``facts`` /
``notes`` / ``interactions``), so a future refactor cannot drift the
tenant filter across tiers.

Two functions:

* :func:`resolve_active_principal` — apply the call-time precedence
  (task-local ``principal_scope`` → tier construction snapshot).  Call
  once at the tier's public-API boundary; pass the resolved id down to
  the recall helpers *and* use it on the write path so a row is tagged
  with the same principal its reader will filter on.
* :func:`principal_eq_clause` — given a resolved principal and a column
  reference, return the ``" AND col = ?"`` SQL fragment + params.

Why this is **not** :mod:`_session_filter`'s shape:

* The session helper returns ``None`` for the ``"*"`` sentinel (no
  filter — CLI/debug only) and always unions :data:`LEGACY_SESSION_ID`
  so pre-RFC rows stay visible from every session.
* The principal helper has **no** ``"*"`` bypass and **no** carve-out:
  the predicate is unconditional strict equality.  A row owned by one
  tenant must never be visible to another, so there is deliberately no
  way to spell "all principals" here (RFC 0031 §C amendment; ISSUE-0081
  strict-isolation decision).  A cross-tenant admin/debug view, if ever
  needed, must be an explicit out-of-band query — never this default
  recall path.

Internal helper (leading-underscore module name); callers inside
:mod:`agents.memory` import the public names directly.
"""

from __future__ import annotations

from ..principal_id import current_principal_id

__all__ = [
    "principal_eq_clause",
    "resolve_active_principal",
]


def resolve_active_principal(snapshot: str) -> str:
    """Resolve the active principal for the current call.

    Precedence: a per-request :func:`~agents.principal_id.principal_scope`
    (task-local ContextVar) wins; otherwise the tier's construction-time
    ``snapshot`` (seeded from :envvar:`PERSATRIX_PRINCIPAL_ID`) is used.
    When no scope is active the snapshot is returned verbatim, so the
    single-tenant CLI / test / boot paths are unchanged.

    The same single seam every tier uses on both its recall and write
    paths — recall filters on the resolved principal and writes tag the
    row with it, so a row is always readable by the principal that wrote
    it.
    """
    return current_principal_id() or snapshot


def principal_eq_clause(
    principal_id: str,
    *,
    column: str,
) -> tuple[str, list[str]]:
    """Build the ``" AND col = ?"`` fragment + ``[principal_id]`` params.

    Always returns a non-empty fragment: the principal predicate is
    unconditional (no ``"*"`` bypass, no carve-out).

    SECURITY: ``column`` is interpolated directly into the returned SQL
    via f-string — it must be a **trusted internal literal** (a column or
    qualified ``alias.column`` reference fixed at the call site), never
    user input.  Identical contract to
    :func:`agents.memory._session_filter.session_in_clause`; every
    in-tree caller passes a string constant.
    """
    return f" AND {column} = ?", [principal_id]
