"""ISSUE-0081 PR 3 — the strict-equality principal recall predicate.

:mod:`agents.memory._principal_filter` is the principal-axis sibling of
:mod:`agents.memory._session_filter`.  Where the session helper builds an
``IN (?, ?, ...)`` clause that always unions the ``legacy`` carve-out,
the principal helper builds an unconditional ``= ?`` clause with **no
carve-out and no "*" bypass** — the strict tenant boundary.

Pins:

* :func:`resolve_active_principal` precedence (task-local scope →
  construction snapshot),
* :func:`principal_eq_clause` fragment + params shape,
* the verbatim-column-interpolation contract (trusted-literal only),
* the absence of any all-tenants sentinel.
"""

from __future__ import annotations

import pytest

from agents.memory._principal_filter import (
    principal_eq_clause,
    resolve_active_principal,
)
from agents.principal_id import principal_scope


def test_resolve_uses_snapshot_when_no_scope() -> None:
    assert resolve_active_principal("snapshot-tenant") == "snapshot-tenant"


def test_resolve_scope_wins_over_snapshot() -> None:
    with principal_scope("scope-tenant"):
        assert resolve_active_principal("snapshot-tenant") == "scope-tenant"


def test_eq_clause_shape() -> None:
    frag, params = principal_eq_clause("tenant-a", column="principal_id")
    assert frag == " AND principal_id = ?"
    assert params == ["tenant-a"]


def test_eq_clause_qualified_column() -> None:
    frag, params = principal_eq_clause("tenant-a", column="e.principal_id")
    assert frag == " AND e.principal_id = ?"
    assert params == ["tenant-a"]


def test_eq_clause_never_empty_no_star_bypass() -> None:
    """Unlike the session helper there is no ``None``/``"*"`` path that
    drops the predicate — the principal filter is always present.
    """
    frag, params = principal_eq_clause("any", column="principal_id")
    assert frag.strip().startswith("AND")
    assert len(params) == 1


def test_no_all_principals_sentinel() -> None:
    import agents.memory._principal_filter as mod

    assert not hasattr(mod, "PRINCIPALS_ALL")
    assert not hasattr(mod, "SESSIONS_ALL")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
