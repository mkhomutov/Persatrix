"""ISSUE-0081 PR 3 — the ``PERSATRIX_PRINCIPAL_ID`` tenant-axis leaf module.

:mod:`agents.principal_id` is the tenant/principal analogue of the
session-axis leaf :mod:`agents.session_id` (RFC 0031 §C amendment).  It
owns the env-var name, the single-tenant default, the gRPC + event
metadata keys, the task-local ``ContextVar``, and the
``principal_scope`` context manager.

The principal axis differs from the session axis in one load-bearing
way pinned here: **there is no carve-out**.  Session recall always
unions the ``legacy`` carve-out; the principal filter is strict
equality.  So this leaf has no ``LEGACY``-style "always visible"
constant — :data:`DEFAULT_PRINCIPAL_ID` is merely the principal every
single-tenant / pre-migration deployment uses, never a cross-tenant
bridge.

This file pins, by analogy with ``test_session_id_leaf_module.py``:

1. exported symbols + constant values (the cross-language contract),
2. resolution precedence (ContextVar → env → default),
3. ``principal_scope`` set/restore + task-local isolation,
4. ``principal_scope_from_metadata`` lift from an event-metadata mapping,
5. the leaf has zero ``logging`` / observability dependency.
"""

from __future__ import annotations

import ast as _ast
import asyncio
import sys
from pathlib import Path

import pytest


def test_leaf_exports_required_symbols() -> None:
    from agents.principal_id import (
        DEFAULT_PRINCIPAL_ID,
        EVENT_PRINCIPAL_METADATA_KEY,
        PRINCIPAL_ID_ENV_VAR,
        PRINCIPAL_METADATA_GRPC_KEY,
        current_principal_id,
        normalize_principal_id,
        principal_scope,
        principal_scope_from_metadata,
        resolve_principal_id_silent,
    )

    assert PRINCIPAL_ID_ENV_VAR == "PERSATRIX_PRINCIPAL_ID"
    assert DEFAULT_PRINCIPAL_ID == "local"
    # Wire header is HTTP/2 lower-case; in-process event key is distinct.
    assert PRINCIPAL_METADATA_GRPC_KEY == "persatrix-principal"
    assert EVENT_PRINCIPAL_METADATA_KEY == "persatrix_principal"
    assert callable(current_principal_id)
    assert callable(resolve_principal_id_silent)
    assert callable(normalize_principal_id)
    assert callable(principal_scope)
    assert callable(principal_scope_from_metadata)


def test_no_legacy_carveout_constant() -> None:
    """The principal axis must NOT grow a ``legacy``-style carve-out.

    A cross-tenant "always visible" id would defeat the entire tenant
    boundary.  If a future change adds such a constant it should trip
    this pin and force an explicit RFC §C/§D re-review.
    """
    import agents.principal_id as leaf

    assert not hasattr(leaf, "LEGACY_PRINCIPAL_ID")
    assert not hasattr(leaf, "PRINCIPAL_CARVE_OUT")


def test_resolves_unset_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents.principal_id import (
        DEFAULT_PRINCIPAL_ID,
        resolve_principal_id_silent,
    )

    monkeypatch.delenv("PERSATRIX_PRINCIPAL_ID", raising=False)
    assert resolve_principal_id_silent() == DEFAULT_PRINCIPAL_ID


def test_resolves_whitespace_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents.principal_id import (
        DEFAULT_PRINCIPAL_ID,
        resolve_principal_id_silent,
    )

    monkeypatch.setenv("PERSATRIX_PRINCIPAL_ID", "   ")
    assert resolve_principal_id_silent() == DEFAULT_PRINCIPAL_ID


def test_resolves_canonical_value(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents.principal_id import resolve_principal_id_silent

    monkeypatch.setenv("PERSATRIX_PRINCIPAL_ID", "tenant-a")
    assert resolve_principal_id_silent() == "tenant-a"


def test_normalize_blank_and_none_to_default() -> None:
    from agents.principal_id import (
        DEFAULT_PRINCIPAL_ID,
        normalize_principal_id,
    )

    assert normalize_principal_id(None) == DEFAULT_PRINCIPAL_ID
    assert normalize_principal_id("") == DEFAULT_PRINCIPAL_ID
    assert normalize_principal_id("   ") == DEFAULT_PRINCIPAL_ID
    assert normalize_principal_id("  tenant-b ") == "tenant-b"


def test_current_principal_id_is_override_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``current_principal_id`` consults ONLY the ContextVar, never env —
    so call-time seams can spell ``current_principal_id() or snapshot``.
    """
    from agents.principal_id import current_principal_id

    monkeypatch.setenv("PERSATRIX_PRINCIPAL_ID", "tenant-a")
    assert current_principal_id() is None


def test_principal_scope_sets_and_restores() -> None:
    from agents.principal_id import current_principal_id, principal_scope

    assert current_principal_id() is None
    with principal_scope("tenant-x") as resolved:
        assert resolved == "tenant-x"
        assert current_principal_id() == "tenant-x"
    assert current_principal_id() is None


def test_principal_scope_normalizes_blank_to_default() -> None:
    from agents.principal_id import (
        DEFAULT_PRINCIPAL_ID,
        current_principal_id,
        principal_scope,
    )

    with principal_scope("   ") as resolved:
        assert resolved == DEFAULT_PRINCIPAL_ID
        assert current_principal_id() == DEFAULT_PRINCIPAL_ID


def test_resolve_precedence_scope_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.principal_id import principal_scope, resolve_principal_id_silent

    monkeypatch.setenv("PERSATRIX_PRINCIPAL_ID", "env-tenant")
    assert resolve_principal_id_silent() == "env-tenant"
    with principal_scope("scope-tenant"):
        assert resolve_principal_id_silent() == "scope-tenant"
    assert resolve_principal_id_silent() == "env-tenant"


def test_scope_is_task_local_isolated() -> None:
    """Two concurrent tasks under different principal scopes must not
    observe each other's value — the per-tenant isolation a process
    env var cannot give.
    """
    from agents.principal_id import current_principal_id, principal_scope

    async def worker(pid: str, hold: float) -> str | None:
        with principal_scope(pid):
            await asyncio.sleep(hold)
            return current_principal_id()

    async def run() -> tuple[str | None, str | None]:
        a, b = await asyncio.gather(
            worker("tenant-a", 0.02),
            worker("tenant-b", 0.0),
        )
        return a, b

    a, b = asyncio.run(run())
    assert a == "tenant-a"
    assert b == "tenant-b"


def test_scope_from_metadata_binds_present_value() -> None:
    from agents.principal_id import (
        EVENT_PRINCIPAL_METADATA_KEY,
        current_principal_id,
        principal_scope_from_metadata,
    )

    md = {EVENT_PRINCIPAL_METADATA_KEY: "tenant-z"}
    with principal_scope_from_metadata(md):
        assert current_principal_id() == "tenant-z"
    assert current_principal_id() is None


def test_scope_from_metadata_nullcontext_when_absent_or_blank() -> None:
    from agents.principal_id import (
        EVENT_PRINCIPAL_METADATA_KEY,
        current_principal_id,
        principal_scope_from_metadata,
    )

    # Missing key → nullcontext → no override.
    with principal_scope_from_metadata({}):
        assert current_principal_id() is None
    # Blank value → nullcontext (not the default-principal scope) so
    # call-time resolution falls back to the construction snapshot.
    with principal_scope_from_metadata({EVENT_PRINCIPAL_METADATA_KEY: ""}):
        assert current_principal_id() is None
    # Non-string value → nullcontext.
    with principal_scope_from_metadata({EVENT_PRINCIPAL_METADATA_KEY: 123}):
        assert current_principal_id() is None


def test_leaf_has_no_logging_dependency() -> None:
    """Same "silent by design" property as the session leaf — no
    ``logging`` / observability import at module scope.
    """
    sys.modules.pop("agents.principal_id", None)
    import agents.principal_id as leaf  # noqa: F401

    assert not hasattr(leaf, "logging"), (
        "agents/principal_id.py must not import logging — mirror the "
        "session leaf's silent-by-design rationale."
    )

    src = Path(leaf.__file__).read_text(encoding="utf-8")
    tree = _ast.parse(src)
    forbidden = {"logging"}
    forbidden_prefixes = ("agents.observability", "opentelemetry")
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                base = alias.name.split(".", 1)[0]
                assert base not in forbidden
                assert not any(
                    alias.name.startswith(p) for p in forbidden_prefixes
                )
        elif isinstance(node, _ast.ImportFrom) and node.module:
            base = node.module.split(".", 1)[0]
            assert base not in forbidden
            assert not any(
                node.module.startswith(p) for p in forbidden_prefixes
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
