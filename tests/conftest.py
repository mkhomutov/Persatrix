"""Root conftest for pytest.

Configures pytest-asyncio mode so async tests run correctly when
invoked from the repo root (the asyncio_mode setting in agents/pyproject.toml
is not discovered by pytest at this level).
"""

import sys
from pathlib import Path

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

