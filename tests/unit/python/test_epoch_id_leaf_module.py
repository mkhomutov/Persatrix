"""ISSUE-0085 — the ``PERSATRIX_EPOCH`` test/run-isolation leaf module.

:mod:`agents.epoch_id` is the run-isolation analogue of the tenant-axis
leaf :mod:`agents.principal_id` (scope-axes reframing — see
``docs/memory-scope-axes.md``).  It owns the env-var name, the
single-world default, the gRPC + event metadata keys, the task-local
``ContextVar``, and the ``epoch_scope`` context manager.

The epoch axis shares the **principal** axis's load-bearing property,
not the session axis's: **there is no carve-out**.  Session recall
always unions the ``legacy`` carve-out (it exists *for* continuity); the
epoch filter is strict equality so a fresh epoch sees nothing — exactly
what a clean test run needs.  So this leaf has no ``LEGACY``-style
"always visible" constant and no ``"*"`` "all epochs" sentinel;
:data:`DEFAULT_EPOCH_ID` is merely the epoch every production / untagged
deployment uses, never a cross-epoch bridge.

This file pins, by analogy with ``test_principal_id_leaf_module.py``:

1. exported symbols + constant values (the cross-language contract),
2. resolution precedence (ContextVar → env → default ``live``),
3. ``epoch_scope`` set/restore + task-local isolation,
4. ``epoch_scope_from_metadata`` lift from an event-metadata mapping,
5. the leaf has zero ``logging`` / observability dependency.
"""

from __future__ import annotations

import ast as _ast
import asyncio
import sys
from pathlib import Path

import pytest


def test_leaf_exports_required_symbols() -> None:
    from agents.epoch_id import (
        DEFAULT_EPOCH_ID,
        EPOCH_ID_ENV_VAR,
        EPOCH_METADATA_GRPC_KEY,
        EVENT_EPOCH_METADATA_KEY,
        current_epoch_id,
        epoch_scope,
        epoch_scope_from_metadata,
        normalize_epoch_id,
        resolve_epoch_id_silent,
    )

    assert EPOCH_ID_ENV_VAR == "PERSATRIX_EPOCH"
    assert DEFAULT_EPOCH_ID == "live"
    # Wire header is HTTP/2 lower-case; in-process event key is distinct.
    assert EPOCH_METADATA_GRPC_KEY == "persatrix-epoch"
    assert EVENT_EPOCH_METADATA_KEY == "persatrix_epoch"
    assert callable(current_epoch_id)
    assert callable(resolve_epoch_id_silent)
    assert callable(normalize_epoch_id)
    assert callable(epoch_scope)
    assert callable(epoch_scope_from_metadata)


def test_no_carveout_or_wildcard_constant() -> None:
    """The epoch axis must NOT grow a ``legacy``-style carve-out or a
    ``"*"`` "all epochs" sentinel.

    Either would defeat run isolation — a fresh epoch must see nothing.
    If a future change adds such a constant it should trip this pin and
    force an explicit re-review against the scope-axes reframing.
    """
    import agents.epoch_id as leaf

    assert not hasattr(leaf, "LEGACY_EPOCH_ID")
    assert not hasattr(leaf, "EPOCH_CARVE_OUT")
    assert not hasattr(leaf, "ALL_EPOCHS")


def test_resolves_unset_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents.epoch_id import DEFAULT_EPOCH_ID, resolve_epoch_id_silent

    monkeypatch.delenv("PERSATRIX_EPOCH", raising=False)
    assert resolve_epoch_id_silent() == DEFAULT_EPOCH_ID


def test_resolves_whitespace_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents.epoch_id import DEFAULT_EPOCH_ID, resolve_epoch_id_silent

    monkeypatch.setenv("PERSATRIX_EPOCH", "   ")
    assert resolve_epoch_id_silent() == DEFAULT_EPOCH_ID


def test_resolves_canonical_value(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents.epoch_id import resolve_epoch_id_silent

    monkeypatch.setenv("PERSATRIX_EPOCH", "ci-run-1234")
    assert resolve_epoch_id_silent() == "ci-run-1234"


def test_normalize_blank_and_none_to_default() -> None:
    from agents.epoch_id import DEFAULT_EPOCH_ID, normalize_epoch_id

    assert normalize_epoch_id(None) == DEFAULT_EPOCH_ID
    assert normalize_epoch_id("") == DEFAULT_EPOCH_ID
    assert normalize_epoch_id("   ") == DEFAULT_EPOCH_ID
    assert normalize_epoch_id("  ci-run-1234 ") == "ci-run-1234"


def test_current_epoch_id_is_override_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``current_epoch_id`` consults ONLY the ContextVar, never env — so
    call-time seams can spell ``current_epoch_id() or snapshot``.
    """
    from agents.epoch_id import current_epoch_id

    monkeypatch.setenv("PERSATRIX_EPOCH", "ci-run-1234")
    assert current_epoch_id() is None


def test_epoch_scope_sets_and_restores() -> None:
    from agents.epoch_id import current_epoch_id, epoch_scope

    assert current_epoch_id() is None
    with epoch_scope("ci-run-x") as resolved:
        assert resolved == "ci-run-x"
        assert current_epoch_id() == "ci-run-x"
    assert current_epoch_id() is None


def test_epoch_scope_normalizes_blank_to_default() -> None:
    from agents.epoch_id import (
        DEFAULT_EPOCH_ID,
        current_epoch_id,
        epoch_scope,
    )

    with epoch_scope("   ") as resolved:
        assert resolved == DEFAULT_EPOCH_ID
        assert current_epoch_id() == DEFAULT_EPOCH_ID


def test_resolve_precedence_scope_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.epoch_id import epoch_scope, resolve_epoch_id_silent

    monkeypatch.setenv("PERSATRIX_EPOCH", "env-epoch")
    assert resolve_epoch_id_silent() == "env-epoch"
    with epoch_scope("scope-epoch"):
        assert resolve_epoch_id_silent() == "scope-epoch"
    assert resolve_epoch_id_silent() == "env-epoch"


def test_scope_is_task_local_isolated() -> None:
    """Two concurrent tasks under different epoch scopes must not observe
    each other's value — the per-run isolation a process env var cannot
    give (the same property that makes CI bump epoch per job safe under
    concurrency).
    """
    from agents.epoch_id import current_epoch_id, epoch_scope

    async def worker(eid: str, hold: float) -> str | None:
        with epoch_scope(eid):
            await asyncio.sleep(hold)
            return current_epoch_id()

    async def run() -> tuple[str | None, str | None]:
        a, b = await asyncio.gather(
            worker("epoch-a", 0.02),
            worker("epoch-b", 0.0),
        )
        return a, b

    a, b = asyncio.run(run())
    assert a == "epoch-a"
    assert b == "epoch-b"


def test_scope_from_metadata_binds_present_value() -> None:
    from agents.epoch_id import (
        EVENT_EPOCH_METADATA_KEY,
        current_epoch_id,
        epoch_scope_from_metadata,
    )

    md = {EVENT_EPOCH_METADATA_KEY: "epoch-z"}
    with epoch_scope_from_metadata(md):
        assert current_epoch_id() == "epoch-z"
    assert current_epoch_id() is None


def test_scope_from_metadata_nullcontext_when_absent_or_blank() -> None:
    from agents.epoch_id import (
        EVENT_EPOCH_METADATA_KEY,
        current_epoch_id,
        epoch_scope_from_metadata,
    )

    # Missing key → nullcontext → no override.
    with epoch_scope_from_metadata({}):
        assert current_epoch_id() is None
    # Blank value → nullcontext (not the default-epoch scope) so call-time
    # resolution falls back to the construction snapshot.
    with epoch_scope_from_metadata({EVENT_EPOCH_METADATA_KEY: ""}):
        assert current_epoch_id() is None
    # Non-string value → nullcontext.
    with epoch_scope_from_metadata({EVENT_EPOCH_METADATA_KEY: 123}):
        assert current_epoch_id() is None


def test_leaf_has_no_logging_dependency() -> None:
    """Same "silent by design" property as the session / principal leaves
    — no ``logging`` / observability import at module scope.
    """
    sys.modules.pop("agents.epoch_id", None)
    import agents.epoch_id as leaf  # noqa: F401

    assert not hasattr(leaf, "logging"), (
        "agents/epoch_id.py must not import logging — mirror the session "
        "/ principal leaves' silent-by-design rationale."
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
