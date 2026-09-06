"""Pin ``scripts/release/sweep.py`` — the pre-tag gate sweep as one command.

Release-prep PR 4 runs the checklist's §1 gates by hand — fifteen commands —
and types the results table into the execution report. The sweep runs the
list, records pass/fail and duration, and prints the table ready to paste.
The judgement stays human; the typing stops. Dry-run by default.
"""

from __future__ import annotations

from scripts.release.sweep import GATES, Gate, GateResult, render_table, run_gates, select


def _fake_runner(fail: frozenset[str] = frozenset(), slow: frozenset[str] = frozenset()):
    def run(gate: Gate) -> GateResult:
        ok = gate.name not in fail
        return GateResult(gate=gate, ok=ok, seconds=42.0 if gate.name in slow else 1.5,
                          tail="" if ok else "boom")
    return run


def test_the_gate_list_mirrors_the_checklist() -> None:
    names = {g.name for g in GATES}
    for expected in ("make test", "cargo test", "make lint", "mypy trees", "make validate",
                     "proto", "sanitizer", "ui", "eval-replay", "licenses", "file size",
                     "doc gates"):
        assert any(expected in n for n in names), expected
    # The Docker smoke costs minutes and needs a daemon: opt-in, not default.
    assert all(not g.default for g in GATES if "smoke" in g.name)


def test_run_gates_records_every_result_in_order() -> None:
    gates = select(GATES, only=None, skip=None, include_optional=False)
    results = run_gates(gates, runner=_fake_runner(fail=frozenset({"make validate"})))
    assert [r.gate.name for r in results] == [g.name for g in gates]
    assert [r.ok for r in results if r.gate.name == "make validate"] == [False]


def test_select_filters_by_only_and_skip() -> None:
    only = select(GATES, only=["make lint", "make validate"], skip=None, include_optional=False)
    assert [g.name for g in only] == ["make lint", "make validate"]
    skipped = select(GATES, only=None, skip=["make test"], include_optional=False)
    assert "make test" not in {g.name for g in skipped}


def test_render_table_is_the_execution_report_shape() -> None:
    gates = select(GATES, only=["make lint", "make validate"], skip=None, include_optional=False)
    runner = _fake_runner(fail=frozenset({"make validate"}), slow=frozenset({"make lint"}))
    results = run_gates(gates, runner=runner)
    table = render_table(results)
    assert table.splitlines()[0] == "| Gate | Command | Result |"
    assert "| make lint | `make lint` | ✅ pass (42.0s) |" in table
    assert "| make validate | `make validate` | ❌ **FAIL** (1.5s) |" in table
    assert "boom" in table  # the failure tail is quoted under the table


def test_run_gates_exit_status_is_one_if_any_failed() -> None:
    from scripts.release.sweep import exit_code
    gates = select(GATES, only=["make lint"], skip=None, include_optional=False)
    assert exit_code(run_gates(gates, runner=_fake_runner())) == 0
    assert exit_code(run_gates(gates, runner=_fake_runner(fail=frozenset({"make lint"})))) == 1


def test_gate_env_reaches_make_through_makeflags() -> None:
    """The Makefile's ``PYTHON := python3`` ignores a plain env var; MAKEFLAGS wins."""
    from scripts.release.sweep import gate_env
    env = gate_env("/repo/.venv/bin/python", base={"PATH": "/usr/bin", "MAKEFLAGS": "-s"})
    assert env["PYTHON"] == "/repo/.venv/bin/python"
    assert env["MAKEFLAGS"] == "-s PYTHON=/repo/.venv/bin/python"
    assert env["PATH"].startswith("/repo/.venv/bin")


def test_select_rejects_an_unknown_gate_name() -> None:
    import pytest
    with pytest.raises(ValueError, match="unknown gate"):
        select(GATES, only=["make tset"], skip=None, include_optional=False)
