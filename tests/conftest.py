"""Root conftest for pytest.

Configures pytest-asyncio mode so async tests run correctly when
invoked from the repo root (the asyncio_mode setting in agents/pyproject.toml
is not discovered by pytest at this level).
"""

import sys
from pathlib import Path

import pytest

# protoc-generated *_grpc.py files use bare `import task_pb2` (not relative),
# so agents/generated/ must be on sys.path for imports to resolve.
_generated = str(Path(__file__).parent.parent / "agents" / "generated")
if _generated not in sys.path:
    sys.path.insert(0, _generated)

# Make sibling helper module ``_test_infra`` importable.
_tests_dir = str(Path(__file__).parent)
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

# Ensure leaked aiosqlite connections cannot hang interpreter shutdown.
# See tests/_test_infra.py for the rationale.
from _test_infra import daemonize_aiosqlite_workers  # noqa: E402

daemonize_aiosqlite_workers()


def pytest_configure(config):
    """Set asyncio_mode=auto so async tests don't need explicit markers."""
    config.option.asyncio_mode = "auto"
    # Register the requires_compose marker for the RFC 0019 PR 4 e2e suite.
    # The marker is also declared in agents/pyproject.toml; registering it
    # here too ensures `pytest --strict-markers` runs from the repo root pass.
    config.addinivalue_line(
        "markers",
        "requires_compose: integration tests that require the docker-compose "
        "observability stack (otel-collector, jaeger, prometheus, loki). "
        "Opt-in via `pytest -m requires_compose`.",
    )
    # Register the requires_orchestrator marker for the RFC 0018 PR 6
    # CLI/E2E suite.  Same dual-registration rationale as above.
    config.addinivalue_line(
        "markers",
        "requires_orchestrator: integration tests that spawn the built "
        "`bin/persatrix-server` and `cli/target/release/persatrix` binaries. "
        "Opt-in via `pytest -m requires_orchestrator` after "
        "`make build-orchestrator build-cli`.",
    )


def pytest_collection_modifyitems(config, items):
    """Deselect ``requires_*`` opt-in tests unless the operator opts in.

    The default ``pytest`` invocation (``make test``, ``make test-integration``)
    must not depend on a live OTEL Collector / Jaeger / Prometheus / Loki
    stack, nor on built CLI/orchestrator binaries.  Operators opt in with
    ``pytest -m requires_compose`` or ``pytest -m requires_orchestrator``;
    when the ``-m`` filter selects the marker, the corresponding skip is a
    no-op.

    Review-fix (PR #171, Should-Fix #5):
    - Use the supported option name ``markexpr`` (``-m`` is the short form
      and ``getoption("-m", …)`` happens to work today only because pytest
      registers it as a short option).
    - Detect the opt-in via the parsed mark expression rather than a naive
      substring match.  The previous ``"requires_compose" in selected_marker``
      check returned True for ``-m "not requires_compose"``, which only
      worked correctly because pytest's own ``-m`` filter then deselected
      the items the hook had just left in.  We now compile the expression
      and ask it whether ``requires_compose`` would be selected by it,
      which gives the right answer for ``and``/``or``/``not`` combinators.
    """
    selected_marker = config.getoption("markexpr", default="") or ""
    compose_opted_in = _markexpr_selects(selected_marker, "requires_compose")
    orch_opted_in = _markexpr_selects(selected_marker, "requires_orchestrator")
    skip_compose = pytest.mark.skip(
        reason="requires docker-compose observability stack — run with `pytest -m requires_compose`"
    )
    skip_orch = pytest.mark.skip(
        reason="requires built orchestrator + CLI binaries — run with `pytest -m requires_orchestrator`"
    )
    for item in items:
        if "requires_compose" in item.keywords and not compose_opted_in:
            item.add_marker(skip_compose)
        if "requires_orchestrator" in item.keywords and not orch_opted_in:
            item.add_marker(skip_orch)


def _markexpr_selects(expr: str, name: str) -> bool:
    """Return True iff the ``-m`` expression would select ``name``.

    Uses pytest's own expression compiler so that ``and`` / ``or`` / ``not``
    combinators are evaluated correctly (e.g. ``not requires_compose``
    returns False).  Falls back to a conservative substring check if
    pytest's private API moves.
    """
    expr = expr.strip()
    if not expr:
        return False
    try:
        from _pytest.mark.expression import Expression  # type: ignore[import-not-found]

        compiled = Expression.compile(expr)

        def _matches(candidate: str, **_: object) -> bool:
            return candidate == name

        return bool(compiled.evaluate(_matches))  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — defensive fallback if the private API moves
        return expr == name


# Back-compat alias retained for any external callers.
def _markexpr_selects_requires_compose(expr: str) -> bool:
    return _markexpr_selects(expr, "requires_compose")
    expr = expr.strip()
    if not expr:
        return False
    try:
        from _pytest.mark.expression import Expression  # type: ignore[import-not-found]

        compiled = Expression.compile(expr)

        def _matches(candidate: str, **_: object) -> bool:
            return candidate == name

        return bool(compiled.evaluate(_matches))  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — defensive fallback if the private API moves
        return expr == name


# Back-compat alias retained for any external callers.
def _markexpr_selects_requires_compose(expr: str) -> bool:
    return _markexpr_selects(expr, "requires_compose")

