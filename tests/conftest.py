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


def pytest_collection_modifyitems(config, items):
    """Deselect ``requires_compose`` tests unless the operator opts in.

    The default ``pytest`` invocation (``make test``, ``make test-integration``)
    must not depend on a live OTEL Collector / Jaeger / Prometheus / Loki
    stack.  Operators opt in with ``pytest -m requires_compose``; when the
    ``-m`` filter selects the marker, this hook is a no-op.

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
    if _markexpr_selects_requires_compose(selected_marker):
        return  # operator opted in explicitly
    skip_marker = pytest.mark.skip(reason="requires docker-compose observability stack — run with `pytest -m requires_compose`")
    for item in items:
        if "requires_compose" in item.keywords:
            item.add_marker(skip_marker)


def _markexpr_selects_requires_compose(expr: str) -> bool:
    """Return True iff the ``-m`` expression would select ``requires_compose``.

    Uses pytest's own expression compiler so that ``and`` / ``or`` / ``not``
    combinators are evaluated correctly (e.g. ``not requires_compose``
    returns False, ``slow and requires_compose`` returns True when the item
    also carries ``slow``).  Falls back to a conservative substring check
    if pytest's private API moves.
    """
    expr = expr.strip()
    if not expr:
        return False
    try:
        from _pytest.mark.expression import Expression  # type: ignore[import-not-found]

        compiled = Expression.compile(expr)
        # ``evaluate`` calls the matcher as ``matcher(name, **kwargs)``; we
        # only care about the name so accept and discard kwargs.
        def _matches(name: str, **_: object) -> bool:
            return name == "requires_compose"

        return bool(compiled.evaluate(_matches))  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — defensive fallback if the private API moves
        # Conservative fallback: treat bare ``requires_compose`` (no ``not``
        # prefix) as opt-in, anything else as opt-out.
        return expr == "requires_compose"

