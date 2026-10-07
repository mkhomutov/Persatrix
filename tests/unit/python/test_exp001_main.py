"""EXP-001 harness — the practice run and the scored run from the command
line (PRs 6b and 6c).

``python -m evaluators.exp001 practice ROOT --provider anthropic`` makes real
model calls, so the provider is never a default: ``offline`` holds the
meetings on the mock provider at no cost and judges nothing. ``scored``
holds the scored run the same way. The runs themselves are in
``test_exp001_practice.py`` and ``test_exp001_scored.py``; these tests hold
the command to what it passes each run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agents.llm_offline import MockProvider
from agents.llm_providers import AnthropicProvider
from evaluators.exp001 import __main__ as command
from evaluators.exp001 import practice, practice_report, scored, scored_report
from evaluators.exp001.attempts import HarnessFault
from evaluators.exp001.costs import ARMS
from evaluators.exp001.deployment import ARMS_ALIAS
from evaluators.exp001.judge import JudgePrompts
from evaluators.exp001.materials import PRACTICE_SERIES, SCORED_SERIES


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """What each practice run was asked to do, in place of running it."""
    calls: list[dict[str, Any]] = []

    async def run_practice(root: Path, arms: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"root": root, "arms": arms, **kwargs})
        return {"the": "report"}

    monkeypatch.setattr(practice, "run_practice", run_practice)
    monkeypatch.setattr(practice_report, "summary", lambda report: f"SUMMARY of {report}\n")
    return calls


@pytest.fixture
def binary(tmp_path: Path) -> Path:
    built = tmp_path / "persatrix-server"
    built.write_text("")
    return built


def test_offline_holds_on_the_mock_provider_and_judges_nothing(
    ran: list[dict[str, Any]], tmp_path: Path, binary: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    code = command.main(["practice", str(tmp_path / "run"), "--provider", "offline",
                         "--binary", str(binary)])
    assert code == 0
    (call,) = ran
    assert (call["root"], call["arms"]) == (tmp_path / "run", ARMS)
    assert (call["alias"].provider, call["alias"].model) == ("mock", "offline")
    assert call["prompts"] is None
    assert isinstance(call["client"]._provider, MockProvider)
    assert call["series"].id == PRACTICE_SERIES
    assert [s.id for s in call["scored"]] == list(SCORED_SERIES)  # for the judge's projection
    assert call["binary"] == binary
    assert "SUMMARY of {'the': 'report'}" in capsys.readouterr().out


def test_on_anthropic_the_arms_model_is_held_and_the_judge_asked(
    ran: list[dict[str, Any]], tmp_path: Path, binary: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-key")
    command.main(["practice", str(tmp_path), "--provider", "anthropic", "--binary", str(binary),
                  "--arms", "D-prime", "A"])
    (call,) = ran
    assert call["arms"] == ("D-prime", "A")
    assert call["alias"] == ARMS_ALIAS
    assert isinstance(call["prompts"], JudgePrompts)
    assert isinstance(call["client"]._provider, AnthropicProvider)


def test_the_provider_is_never_a_default(ran: list[dict[str, Any]], tmp_path: Path) -> None:
    """A run on the real provider spends money, so it is always asked for by name."""
    with pytest.raises(SystemExit) as stopped:
        command.main(["practice", str(tmp_path)])
    assert stopped.value.code == 2 and ran == []


def test_an_arm_named_twice_is_a_usage_error(ran: list[dict[str, Any]], tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as stopped:
        command.main(["practice", str(tmp_path), "--provider", "offline", "--arms", "A", "A"])
    assert stopped.value.code == 2 and ran == []


def test_a_practice_run_that_cannot_go_on_says_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, binary: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def run_practice(root: Path, arms: Any, **kwargs: Any) -> dict[str, Any]:
        raise practice.RefusedError(f"{root}: a harness fault closed this practice run")

    monkeypatch.setattr(practice, "run_practice", run_practice)
    code = command.main(["practice", str(tmp_path), "--provider", "offline",
                         "--binary", str(binary)])
    assert code == 2
    assert "closed this practice run" in capsys.readouterr().err


def test_any_other_error_keeps_its_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, binary: Path,
) -> None:
    async def run_practice(root: Path, arms: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("a bug")

    monkeypatch.setattr(practice, "run_practice", run_practice)
    with pytest.raises(RuntimeError, match="a bug"):
        command.main(["practice", str(tmp_path), "--provider", "offline",
                      "--binary", str(binary)])


def test_a_channel_arm_without_the_orchestrator_binary_is_refused(
    ran: list[dict[str, Any]], tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    code = command.main(["practice", str(tmp_path), "--provider", "offline", "--arms", "B",
                         "--binary", str(tmp_path / "missing")])
    assert code == 2 and ran == []
    assert "make build-orchestrator" in capsys.readouterr().err


def test_arm_a_alone_needs_no_binary(ran: list[dict[str, Any]], tmp_path: Path) -> None:
    code = command.main(["practice", str(tmp_path), "--provider", "offline", "--arms", "A",
                         "--binary", str(tmp_path / "missing")])
    assert code == 0 and len(ran) == 1


def test_a_harness_fault_stops_it_with_what_went_wrong(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, binary: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def run_practice(root: Path, arms: Any, **kwargs: Any) -> dict[str, Any]:
        raise HarnessFault("C, practice, practice-plan-1, attempt 1, try 1: unreadable")

    monkeypatch.setattr(practice, "run_practice", run_practice)
    code = command.main(["practice", str(tmp_path), "--provider", "offline",
                         "--binary", str(binary)])
    assert code == 2
    assert "harness fault: C, practice, practice-plan-1" in capsys.readouterr().err


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """What each scored run was asked to do, in place of holding it."""
    calls: list[dict[str, Any]] = []

    async def run_scored(root: Path, **kwargs: Any) -> dict[str, Any]:
        calls.append({"root": root, **kwargs})
        return {"the": "scored report"}

    monkeypatch.setattr(scored, "run_scored", run_scored)
    monkeypatch.setattr(scored_report, "summary", lambda report: f"SCORED SUMMARY of {report}\n")
    return calls


class TestTheScoredRun:
    def test_offline_it_is_a_rehearsal_on_the_mock_provider(
        self, held: list[dict[str, Any]], tmp_path: Path, binary: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        code = command.main(["scored", str(tmp_path / "run"), "--provider", "offline",
                             "--binary", str(binary)])
        assert code == 0
        (call,) = held
        assert call["root"] == tmp_path / "run"
        assert (call["alias"].provider, call["alias"].model) == ("mock", "offline")
        assert call["prompts"] is None
        assert isinstance(call["client"]._provider, MockProvider)
        assert [s.id for s in call["series"]] == list(SCORED_SERIES)
        assert (call["binary"], call["fixed_by"]) == (binary, None)
        assert "SCORED SUMMARY of {'the': 'scored report'}" in capsys.readouterr().out

    def test_on_anthropic_it_is_judged_and_a_new_window_names_its_fix(
        self, held: list[dict[str, Any]], tmp_path: Path, binary: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-key")
        command.main(["scored", str(tmp_path), "--provider", "anthropic", "--binary", str(binary),
                      "--fixed-by", "#2001"])
        (call,) = held
        assert call["alias"] == ARMS_ALIAS
        assert isinstance(call["prompts"], JudgePrompts)
        assert isinstance(call["client"]._provider, AnthropicProvider)
        assert call["fixed_by"] == "#2001"

    def test_the_provider_is_never_a_default(
        self, held: list[dict[str, Any]], tmp_path: Path,
    ) -> None:
        with pytest.raises(SystemExit) as stopped:
            command.main(["scored", str(tmp_path)])
        assert stopped.value.code == 2 and held == []

    def test_it_needs_the_orchestrator_binary(
        self, held: list[dict[str, Any]], tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        code = command.main(["scored", str(tmp_path), "--provider", "offline",
                             "--binary", str(tmp_path / "missing")])
        assert code == 2 and held == []
        assert "make build-orchestrator" in capsys.readouterr().err

    def test_a_run_that_cannot_go_on_says_why(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, binary: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        async def run_scored(root: Path, **kwargs: Any) -> dict[str, Any]:
            raise scored.RefusedError(f"{root}: start window 2 with --fixed-by")

        monkeypatch.setattr(scored, "run_scored", run_scored)
        code = command.main(["scored", str(tmp_path), "--provider", "offline",
                             "--binary", str(binary)])
        assert code == 2
        assert "start window 2 with --fixed-by" in capsys.readouterr().err

    def test_a_harness_fault_names_what_went_wrong_and_what_the_report_says(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, binary: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        async def run_scored(root: Path, **kwargs: Any) -> dict[str, Any]:
            (root / scored.REPORT).write_text('{"the": "stopped report"}')
            raise HarnessFault("C, series-1, series-1-plan-1, attempt 1, try 1: unreadable")

        monkeypatch.setattr(scored, "run_scored", run_scored)
        monkeypatch.setattr(scored_report, "summary",
                            lambda report: f"SCORED SUMMARY of {report}\n")
        code = command.main(["scored", str(tmp_path), "--provider", "offline",
                             "--binary", str(binary)])
        assert code == 2
        captured = capsys.readouterr()
        assert "harness fault: C, series-1, series-1-plan-1" in captured.err
        assert "SCORED SUMMARY of {'the': 'stopped report'}" in captured.out
