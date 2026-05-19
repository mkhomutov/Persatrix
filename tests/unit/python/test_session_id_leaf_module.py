"""
PR #337 deep review finding **M2** — the env-var read logic for
``PERSATRIX_SESSION_ID`` must live in a single leaf module so the
``MemoryFacade`` constructor and the persona-runtime logger wrapper
cannot drift.

Before this fix, two modules each inlined the silent
``os.environ.get("PERSATRIX_SESSION_ID", "").strip() or "legacy"``
sequence:

* :mod:`agents.memory.facade` — needed a silent read at construction
  time but cannot import :mod:`agents.persona_runtime.session_id`
  without re-introducing the
  ``persona_runtime → persona → base → memory.facade`` import cycle.
* :mod:`agents.persona_runtime.session_id` — the canonical reader with
  the INFO/WARN log lines that mirror the Go orchestrator.

The fix is a true leaf module ``agents/session_id.py`` that owns:

* the env-var name (:data:`SESSION_ID_ENV_VAR`)
* the legacy carve-out constant (:data:`LEGACY_SESSION_ID`)
* the silent reader (:func:`resolve_session_id_silent`)

Both call sites import from the leaf — the facade gets a single
function call; the persona-runtime wrapper delegates to it and adds
the log line.  This file pins:

1. The leaf module has zero ``logging`` / observability dependencies
   (otherwise a future log line at construction-time would
   double-emit alongside :func:`agents.persona_runtime.session_id.resolve_session_id_and_log`).
2. ``MemoryFacade``'s construction-time session id matches the leaf's
   :func:`resolve_session_id_silent` output exactly.
3. The persona-runtime wrapper re-exports the leaf's constants so
   existing imports of ``SESSION_ID_ENV_VAR`` / ``LEGACY_SESSION_ID``
   from :mod:`agents.persona_runtime.session_id` keep working.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


def test_leaf_module_exports_required_symbols() -> None:
    """The three names the facade and persona-runtime both need."""
    from agents.session_id import (
        LEGACY_SESSION_ID,
        SESSION_ID_ENV_VAR,
        resolve_session_id_silent,
    )

    assert SESSION_ID_ENV_VAR == "PERSATRIX_SESSION_ID"
    assert LEGACY_SESSION_ID == "legacy"
    assert callable(resolve_session_id_silent)


def test_leaf_resolves_unset_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents.session_id import LEGACY_SESSION_ID, resolve_session_id_silent

    monkeypatch.delenv("PERSATRIX_SESSION_ID", raising=False)
    assert resolve_session_id_silent() == LEGACY_SESSION_ID


def test_leaf_resolves_whitespace_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-strip the value so an operator's stray spaces still hit the
    legacy carve-out (matches the inlined facade logic and the
    persona-runtime resolver pre-fix)."""
    from agents.session_id import LEGACY_SESSION_ID, resolve_session_id_silent

    monkeypatch.setenv("PERSATRIX_SESSION_ID", "   ")
    assert resolve_session_id_silent() == LEGACY_SESSION_ID


def test_leaf_preserves_canonical_value(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents.session_id import resolve_session_id_silent

    monkeypatch.setenv("PERSATRIX_SESSION_ID", "run-a")
    assert resolve_session_id_silent() == "run-a"


def test_leaf_preserves_non_canonical_value_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leaf is the silent reader.  Canonical-regex enforcement +
    WARN lives in :mod:`agents.persona_runtime.session_id`; the leaf
    must not pre-filter values or the WARN message would never fire
    on the persona-runtime path.
    """
    from agents.session_id import resolve_session_id_silent

    monkeypatch.setenv("PERSATRIX_SESSION_ID", "my session")
    assert resolve_session_id_silent() == "my session"


def test_leaf_has_no_logging_dependency() -> None:
    """The leaf must NOT pull in ``logging`` — that was the whole point
    of breaking the env reader out of the persona-runtime wrapper.

    If a future contributor adds ``import logging`` and a
    ``logger.info`` call at construction-time, the operator would see
    two INFO lines for the same env-resolution decision: one from the
    facade's :class:`MemoryFacade.__init__` (every task agent) and one
    from :func:`agents.persona_runtime.session_id.resolve_session_id_and_log`
    (the persona-runtime boot path).  The original PR 4 fix-up
    explicitly cited "silent by design" as the rationale for inlining
    the env read into the facade; this leaf module preserves that
    rationale across the dedup refactor.
    """
    # Reload fresh so a cached import from another test does not mask
    # the property under test.
    sys.modules.pop("agents.session_id", None)
    import agents.session_id as leaf  # noqa: F401

    # The module under test must not have a module-level ``logging``
    # attribute (which is how ``import logging`` would manifest).
    assert not hasattr(leaf, "logging"), (
        "agents/session_id.py must not import logging — see this "
        "test's docstring for the double-emit rationale.  If a "
        "future change genuinely needs to log from this module, "
        "first delete this test and document why the double-emit "
        "concern no longer applies."
    )

    # Belt-and-suspenders: AST-parse the module so a future
    # contributor cannot pass this test by aliasing the module (e.g.
    # ``import logging as _l``).  Docstring text containing the words
    # ``import logging`` is fine — only real import statements
    # against any logging / observability submodule are rejected.
    import ast as _ast
    src = Path(leaf.__file__).read_text(encoding="utf-8")
    tree = _ast.parse(src)
    forbidden = {"logging"}
    # Also forbid the observability subtree — any pull-in there
    # is a sign the leaf is no longer a true leaf.
    forbidden_prefixes = ("agents.observability", "opentelemetry")
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                base = alias.name.split(".", 1)[0]
                assert base not in forbidden, (
                    f"agents/session_id.py must not ``import {alias.name}``"
                )
                assert not any(
                    alias.name.startswith(p) for p in forbidden_prefixes
                ), (
                    f"agents/session_id.py must not ``import {alias.name}`` "
                    "(observability subtree forbidden in the leaf)"
                )
        elif isinstance(node, _ast.ImportFrom) and node.module:
            base = node.module.split(".", 1)[0]
            assert base not in forbidden, (
                f"agents/session_id.py must not ``from {node.module} import``"
            )
            assert not any(
                node.module.startswith(p) for p in forbidden_prefixes
            ), (
                f"agents/session_id.py must not ``from {node.module} "
                "import`` (observability subtree forbidden in the leaf)"
            )


def test_facade_session_id_matches_leaf_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The construction-time read in :class:`MemoryFacade` must
    delegate to :func:`resolve_session_id_silent` (or behave identically
    to it).  This pins the dedup contract: as the carve-out semantics
    evolve in Phase 3 (tighter validation), the two read paths cannot
    drift.
    """
    from agents.memory.facade import MemoryFacade
    from agents.session_id import resolve_session_id_silent

    for env_value, label in [
        (None, "unset"),
        ("", "empty"),
        ("   ", "whitespace"),
        ("run-a", "canonical"),
        ("my session", "non-canonical (kept verbatim by leaf)"),
    ]:
        if env_value is None:
            monkeypatch.delenv("PERSATRIX_SESSION_ID", raising=False)
        else:
            monkeypatch.setenv("PERSATRIX_SESSION_ID", env_value)

        expected = resolve_session_id_silent()
        fac = MemoryFacade(
            agent_id="x", db_path=str(tmp_path / f"m-{label.split()[0]}.db"),
        )
        assert fac._session_id == expected, (  # noqa: SLF001 — dedup contract
            f"MemoryFacade._session_id must equal resolve_session_id_silent() "
            f"for env={env_value!r} ({label}); got "
            f"{fac._session_id!r} vs {expected!r}"  # noqa: SLF001
        )


def test_persona_runtime_reexports_leaf_constants() -> None:
    """Existing callers import :data:`SESSION_ID_ENV_VAR` and
    :data:`LEGACY_SESSION_ID` from :mod:`agents.persona_runtime.session_id`
    (see :file:`tests/unit/python/test_session_id_resolve.py`).  The
    dedup refactor must preserve that import path — re-export the
    leaf's constants from the persona-runtime module so the existing
    suite keeps passing without churn.
    """
    from agents.persona_runtime import session_id as wrapper
    from agents.session_id import (
        LEGACY_SESSION_ID as LEAF_LEGACY,
    )
    from agents.session_id import (
        SESSION_ID_ENV_VAR as LEAF_ENV_VAR,
    )

    assert wrapper.SESSION_ID_ENV_VAR == LEAF_ENV_VAR
    assert wrapper.LEGACY_SESSION_ID == LEAF_LEGACY


def test_persona_runtime_wrapper_still_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Belt-and-suspenders: the dedup refactor must not silence the
    WARN log that the existing ``test_session_id_resolve.py`` suite
    pins for the persona-runtime path.  A regression here would be
    caught by that suite but reproducing the contract here makes the
    refactor's safety property explicit at one file.
    """
    # Reload after monkeypatch so a stale module-level state cannot
    # mask a regression.
    import agents.persona_runtime.session_id as mod
    importlib.reload(mod)

    import logging as _logging
    test_logger = _logging.getLogger("test.persona_runtime.session_id.leaf")
    monkeypatch.setenv("PERSATRIX_SESSION_ID", "my session")
    with caplog.at_level(_logging.WARNING, logger=test_logger.name):
        result = mod.resolve_session_id_and_log(test_logger)
    assert result == "my session"
    warn_msgs = [
        r.getMessage() for r in caplog.records if r.levelno == _logging.WARNING
    ]
    assert any("[A-Za-z0-9_-]" in m for m in warn_msgs), (
        "WARN must still cite the canonical regex post-dedup; "
        f"got: {warn_msgs!r}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
