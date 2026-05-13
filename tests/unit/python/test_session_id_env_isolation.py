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

import pytest

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
    #
    # PR #337 review L6: use the public ``session_id`` property
    # rather than reaching for ``_session_id`` so the test pins the
    # public contract.
    fac = MemoryFacade(agent_id="ember-owl", db_path=str(tmp_path / "m.db"))
    try:
        assert fac.session_id == "legacy", (
            "MemoryFacade construction-time default must be 'legacy' when "
            "PERSATRIX_SESSION_ID is unset; the autouse "
            "_isolate_session_env fixture (conftest.py) is responsible "
            f"for that pre-condition. Got: {fac.session_id!r}"
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
        # writes with the prior test's polluted value.  PR #337 L6:
        # use the public ``session_id`` property.
        assert "PERSATRIX_SESSION_ID" not in os.environ
        fac = MemoryFacade(
            agent_id="x", db_path=str(tmp_path / "m.db"),
        )
        assert fac.session_id == "legacy"


class TestSessionIdPropertyContract:
    """PR #337 review L6 — :attr:`MemoryFacade.session_id` is the
    public read-only contract for the construction-time env snapshot.
    Tests that previously poked ``_session_id`` directly with
    ``noqa: SLF001`` now use this property; pin the contract here so
    a future refactor that removes the property fails loudly.
    """

    def test_property_returns_construction_time_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PERSATRIX_SESSION_ID", "run-x")
        fac = MemoryFacade(agent_id="x", db_path=str(tmp_path / "m.db"))
        assert fac.session_id == "run-x"

    def test_property_is_read_only(self, tmp_path: Path) -> None:
        fac = MemoryFacade(agent_id="x", db_path=str(tmp_path / "m.db"))
        with pytest.raises(AttributeError):
            fac.session_id = "tampered"  # type: ignore[misc]

    def test_property_matches_private_attr(self, tmp_path: Path) -> None:
        # During the L6 transition both still exist; pin that the
        # property is a true view onto the same value (not a divergent
        # cache or copy).  If the private attribute is eventually
        # removed, this test should be deleted.
        fac = MemoryFacade(agent_id="x", db_path=str(tmp_path / "m.db"))
        assert fac.session_id == fac._session_id  # noqa: SLF001 — transition guard
