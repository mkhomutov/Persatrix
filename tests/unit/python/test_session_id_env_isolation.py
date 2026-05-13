"""
Regression test for the autouse ``_isolate_session_env`` fixture
(see :file:`conftest.py`).

RFC 0031 Phase 1 makes :class:`agents.memory.facade.MemoryFacade` read
``PERSATRIX_SESSION_ID`` at construction.  Before this autouse fixture
existed, any test that constructed a ``MemoryFacade`` and assumed
``_session_id == "legacy"`` would silently pick up a shell-inherited
value if the developer happened to have ``PERSATRIX_SESSION_ID``
exported (or if CI ever exported it globally for any reason).

This file pins the contract that the fixture provides: at the **start**
of every test in this directory the env var is unset, regardless of the
caller's shell environment.  We simulate the "developer has it set"
scenario by setting the var *outside* the fixture's normal contract
(via :func:`os.environ` directly so the autouse fixture's pre-test
delete is what removes it) and confirming the facade still reads
``"legacy"`` on the next test.

PR 4 review follow-up F5.
"""

from __future__ import annotations

import os
from pathlib import Path

from agents.memory.facade import MemoryFacade


def test_autouse_fixture_removes_env_var_before_test() -> None:
    """The autouse fixture must run *before* the test body sees env state."""
    assert "PERSATRIX_SESSION_ID" not in os.environ, (
        "the autouse _isolate_session_env fixture in conftest.py must "
        "delete PERSATRIX_SESSION_ID before each test runs; if you see "
        "this fail, the fixture either was not invoked or some other "
        "fixture re-set the var after it ran"
    )


async def test_facade_default_is_legacy_under_autouse_fixture(
    tmp_path: Path,
) -> None:
    # No monkeypatch call here — relies entirely on the autouse fixture
    # to clear the env.  If the fixture is missing or broken, this test
    # is the canary: it would pick up a shell-inherited value and the
    # construction-time default would not be "legacy".
    fac = MemoryFacade(agent_id="ember-owl", db_path=str(tmp_path / "m.db"))
    try:
        assert fac._session_id == "legacy", (  # noqa: SLF001 — test inspection
            "MemoryFacade construction-time default must be 'legacy' when "
            "PERSATRIX_SESSION_ID is unset; the autouse "
            "_isolate_session_env fixture (conftest.py) is responsible "
            f"for that pre-condition. Got: {fac._session_id!r}"  # noqa: SLF001
        )
    finally:
        # Not initialised — no close() needed.
        pass


# The pollution test below sets the env var inside one test's body
# (no monkeypatch — using os.environ directly).  When pytest runs the
# next test, the autouse fixture must wipe it.  Without the autouse
# fixture this would leak across test ordering.
class TestEnvLeakIsBlocked:
    def test_a_pollute_env_directly(self) -> None:
        # Deliberate: write the env var via os.environ (NOT monkeypatch)
        # so it persists past this test's frame.  The autouse fixture
        # runs ``monkeypatch.delenv`` at the *start* of the next test;
        # monkeypatch.delenv on a var the test never set is a no-op
        # otherwise, but it still removes the os.environ entry we made
        # here.
        os.environ["PERSATRIX_SESSION_ID"] = "leak-canary"
        assert os.environ["PERSATRIX_SESSION_ID"] == "leak-canary"

    def test_b_autouse_fixture_blocks_leak(self, tmp_path: Path) -> None:
        # If this fails, the autouse fixture did not clean up
        # ``leak-canary`` between tests and the facade would tag its
        # writes with the prior test's polluted value.
        assert "PERSATRIX_SESSION_ID" not in os.environ
        fac = MemoryFacade(
            agent_id="x", db_path=str(tmp_path / "m.db"),
        )
        assert fac._session_id == "legacy"  # noqa: SLF001
