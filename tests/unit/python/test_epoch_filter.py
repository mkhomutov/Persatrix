"""ISSUE-0085 PR 3 — the strict-equality epoch recall predicate.

:mod:`agents.memory._epoch_filter` is the epoch-axis sibling of
:mod:`agents.memory._principal_filter`.  Where the session helper builds
an ``IN (?, ?, ...)`` clause that always unions the ``legacy`` carve-out,
the epoch helper (like the principal helper) builds an unconditional
``= ?`` clause with **no carve-out and no "*" bypass** — the strict
run/test-isolation boundary.

Pins:

* :func:`resolve_active_epoch` precedence (task-local scope →
  construction snapshot),
* :func:`epoch_eq_clause` fragment + params shape,
* the verbatim-column-interpolation contract (trusted-literal only),
* the absence of any all-epochs sentinel.
"""

from __future__ import annotations

import pytest

from agents.epoch_id import epoch_scope
from agents.memory._epoch_filter import (
    epoch_eq_clause,
    resolve_active_epoch,
)


def test_resolve_uses_snapshot_when_no_scope() -> None:
    assert resolve_active_epoch("snapshot-epoch") == "snapshot-epoch"


def test_resolve_scope_wins_over_snapshot() -> None:
    with epoch_scope("scope-epoch"):
        assert resolve_active_epoch("snapshot-epoch") == "scope-epoch"


def test_eq_clause_shape() -> None:
    frag, params = epoch_eq_clause("run-1", column="epoch_id")
    assert frag == " AND epoch_id = ?"
    assert params == ["run-1"]


def test_eq_clause_qualified_column() -> None:
    frag, params = epoch_eq_clause("run-1", column="e.epoch_id")
    assert frag == " AND e.epoch_id = ?"
    assert params == ["run-1"]


def test_eq_clause_never_empty_no_star_bypass() -> None:
    """Unlike the session helper there is no ``None``/``"*"`` path that
    drops the predicate — the epoch filter is always present.
    """
    frag, params = epoch_eq_clause("any", column="epoch_id")
    assert frag.strip().startswith("AND")
    assert len(params) == 1


def test_no_all_epochs_sentinel() -> None:
    import agents.memory._epoch_filter as mod

    assert not hasattr(mod, "EPOCHS_ALL")
    assert not hasattr(mod, "SESSIONS_ALL")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
