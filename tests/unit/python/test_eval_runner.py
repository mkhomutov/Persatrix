"""Unit tests for the RFC 0044 Phase 1 eval runner + report artifact (PR 3).

The runner is the orchestration half of Phase 1: it loads a recipe, builds the
right LLM provider for the mode (``replay`` from a recorded golden, ``record``
wrapping a live provider, ``drift`` live), drives the recipe's interactions/turns
through a :class:`~evaluators.runner.PersonaDriver` to produce an
:class:`~evaluators.assertions.EvalRun`, and calls ``evaluate`` to get a report.
It then serializes a structured, per-assertion artifact (RFC 0044 §F).

This suite pins the orchestration and serialization contracts *without* the
persona runtime — a deterministic ``_FakeDriver`` stands in for the runtime so
these tests stay pure. The real runtime adapter (``PersonaRuntimeDriver``) is
covered end-to-end in ``test_eval_persona_driver.py``.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from evaluators.assertions import EvalRun
from evaluators.eval_set import EvalSet, load_eval_set
from evaluators.replay_llm_client import ReplayProvider
from evaluators.report import report_to_dict, suite_report, write_report
from evaluators.runner import (
    EvalMode,
    build_provider,
    discover_recipes,
    golden_path_for,
    main,
    parse_elapsed,
    run_eval,
)

# A complete, valid recipe (state-only + final_transcript, no event assertions —
# the Phase 1 subset that does not require the unlanded RFC 0041 typed events).
_RECIPE = textwrap.dedent(
    """
    id: EVAL-MEMORY-001
    title: Recall across interactions
    tier: stable
    setup:
      persona: ember-owl
      user: alice
      seed_state:
        persona:ember-owl:trust.scores.alice: 0.0
    interactions:
      - id: i1
        turns:
          - user: "Hi Ember — I'm Alice, I work on the data-platform team."
          - assistant: {match: contains, value: "Alice"}
      - id: i2
        elapsed: 5m
        turns:
          - user: "What do you remember about me?"
          - assistant: {match: must_reference, values: ["Alice", "data-platform"]}
    assertions:
      terminal_state:
        persona:ember-owl:trust.scores.alice: {match: gte, value: 0.0}
      final_transcript:
        must_reference: ["Alice", "data-platform"]
        must_not_reference: ["I don't recall"]
    """
).strip()


def _write(tmp_path: Path, body: str, name: str = "EVAL-MEMORY-001.yaml") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def _passing_run() -> EvalRun:
    return EvalRun(
        turn_outputs=[
            "Nice to meet you, Alice!",
            "You're Alice from the data-platform team.",
        ],
        terminal_state={"persona:ember-owl:trust.scores.alice": 0.4},
        events=[],
    )


class _FakeDriver:
    """A deterministic ``PersonaDriver`` — returns a canned run, records what it
    was asked to drive so orchestration wiring can be asserted."""

    def __init__(self, run: EvalRun) -> None:
        self._run = run
        self.seen: list[tuple[str, object]] = []

    async def run(self, eval_set: EvalSet, provider: object) -> EvalRun:
        self.seen.append((eval_set.id, provider))
        return self._run


# ─── parse_elapsed ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "spec,seconds",
    [("0s", 0.0), ("30s", 30.0), ("5m", 300.0), ("2h", 7200.0), ("1d", 86400.0),
     ("120s", 120.0), ("48h", 172800.0)],
)
def test_parse_elapsed_units(spec: str, seconds: float) -> None:
    assert parse_elapsed(spec) == seconds


@pytest.mark.parametrize("bad", ["", "5", "m", "5x", "5 m", " 5m", "5.5m", "-5m", "5min"])
def test_parse_elapsed_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_elapsed(bad)


# ─── run_eval (orchestration via a fake driver) ──────────────────────────────


async def test_run_eval_passes_run_through_evaluate(tmp_path: Path) -> None:
    es = load_eval_set(_write(tmp_path, _RECIPE))
    driver = _FakeDriver(_passing_run())
    provider = object()
    report = await run_eval(es, provider=provider, driver=driver)
    assert report.eval_id == "EVAL-MEMORY-001"
    assert report.passed is True, report.failures()
    # The runner handed the driver the recipe + the provider it built.
    assert driver.seen == [("EVAL-MEMORY-001", provider)]


async def test_run_eval_reports_failure_from_run(tmp_path: Path) -> None:
    es = load_eval_set(_write(tmp_path, _RECIPE))
    run = _passing_run()
    run.turn_outputs[1] = "You're Alice."  # drops "data-platform" → must_reference fails
    report = await run_eval(es, provider=object(), driver=_FakeDriver(run))
    assert report.passed is False
    assert any("final_transcript" in f.name for f in report.failures())


# ─── report artifact serialization ───────────────────────────────────────────


async def test_report_to_dict_shape(tmp_path: Path) -> None:
    es = load_eval_set(_write(tmp_path, _RECIPE))
    report = await run_eval(es, provider=object(), driver=_FakeDriver(_passing_run()))
    d = report_to_dict(report, tier=es.tier, mode=EvalMode.REPLAY)
    assert d["eval_id"] == "EVAL-MEMORY-001"
    assert d["tier"] == "stable"
    assert d["mode"] == "replay"
    assert d["passed"] is True
    assert d["summary"]["total"] == len(report.results)
    assert d["summary"]["failed"] == 0
    # Every assertion row carries a stable name + pass flag + detail string.
    assert all({"name", "passed", "detail"} <= set(a) for a in d["assertions"])
    json.dumps(d)  # the artifact is JSON-safe


async def test_report_to_dict_counts_failures(tmp_path: Path) -> None:
    es = load_eval_set(_write(tmp_path, _RECIPE))
    run = _passing_run()
    run.turn_outputs[0] = "Nice to meet you!"  # turn[0] contains "Alice" fails
    report = await run_eval(es, provider=object(), driver=_FakeDriver(run))
    d = report_to_dict(report, tier=es.tier, mode=EvalMode.REPLAY)
    assert d["passed"] is False
    assert d["summary"]["failed"] >= 1
    assert d["summary"]["passed"] + d["summary"]["failed"] == d["summary"]["total"]


def test_suite_report_aggregates_and_writes(tmp_path: Path) -> None:
    passing = {"eval_id": "EVAL-A-001", "passed": True, "assertions": [],
               "summary": {"total": 1, "passed": 1, "failed": 0}}
    failing = {"eval_id": "EVAL-B-001", "passed": False, "assertions": [],
               "summary": {"total": 2, "passed": 1, "failed": 1}}
    suite = suite_report([passing, failing])
    assert suite["summary"]["evals"] == 2
    assert suite["summary"]["passed"] == 1
    assert suite["summary"]["failed"] == 1
    assert suite["summary"]["passed_all"] is False
    out = tmp_path / "report.json"
    write_report(out, suite)
    assert json.loads(out.read_text(encoding="utf-8"))["summary"]["evals"] == 2


# ─── recipe discovery + golden sidecar path ──────────────────────────────────


def test_golden_path_for_is_sidecar() -> None:
    p = golden_path_for(Path("evaluators/eval_sets/EVAL-MEMORY-001.yaml"))
    assert p.name == "EVAL-MEMORY-001.golden.yaml"


def test_discover_recipes_excludes_goldens(tmp_path: Path) -> None:
    (tmp_path / "EVAL-MEMORY-001.yaml").write_text(_RECIPE, encoding="utf-8")
    (tmp_path / "EVAL-MEMORY-001.golden.yaml").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    found = discover_recipes(tmp_path)
    names = sorted(p.name for p in found)
    assert names == ["EVAL-MEMORY-001.yaml"]  # golden sidecar + non-yaml excluded


def test_discover_recipes_excludes_non_recipe_yaml(tmp_path: Path) -> None:
    """Only ``EVAL-*.yaml`` recipes are discovered (ISSUE — the PR 4c
    ``offline_responses.eval.yaml`` fixture lives beside the recipes, and the
    no-target ``make eval-replay`` sweep must not try to load it as one)."""
    (tmp_path / "EVAL-MEMORY-001.yaml").write_text(_RECIPE, encoding="utf-8")
    (tmp_path / "offline_responses.eval.yaml").write_text(
        "responses: []\n", encoding="utf-8"
    )
    found = discover_recipes(tmp_path)
    assert [p.name for p in found] == ["EVAL-MEMORY-001.yaml"]


def test_discover_recipes_target_filter(tmp_path: Path) -> None:
    (tmp_path / "EVAL-MEMORY-001.yaml").write_text(_RECIPE, encoding="utf-8")
    (tmp_path / "EVAL-RECALL-001.yaml").write_text(
        _RECIPE.replace("EVAL-MEMORY-001", "EVAL-RECALL-001"), encoding="utf-8"
    )
    found = discover_recipes(tmp_path, target="EVAL-MEMORY-001")
    assert [p.name for p in found] == ["EVAL-MEMORY-001.yaml"]


def test_discover_recipes_missing_dir_is_empty(tmp_path: Path) -> None:
    # Phase 1: the eval_sets/ dir may be empty/absent (seed recipes land in PR 4);
    # discovery must degrade to an empty list, not raise.
    assert discover_recipes(tmp_path / "does-not-exist") == []


# ─── provider building per mode ──────────────────────────────────────────────


def test_build_provider_replay_from_golden(tmp_path: Path) -> None:
    # A minimal cassette on disk → replay mode yields a ReplayProvider bound to it.
    golden = tmp_path / "EVAL-MEMORY-001.golden.yaml"
    golden.write_text("{}\n", encoding="utf-8")
    provider = build_provider(EvalMode.REPLAY, golden_path=golden)
    assert isinstance(provider, ReplayProvider)


def test_build_provider_replay_missing_golden_raises(tmp_path: Path) -> None:
    # Replay against a recipe whose golden was never recorded must fail loudly,
    # not silently pass (RFC 0044 §D).
    with pytest.raises(FileNotFoundError):
        build_provider(EvalMode.REPLAY, golden_path=tmp_path / "nope.golden.yaml")


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_main_no_recipes_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    # `make eval-replay` with an empty eval_sets/ dir (Phase 1 reality) is a
    # no-op success, not an error.
    code = main(["--mode", "replay", "--eval-sets-dir", str(tmp_path)])
    assert code == 0
    assert "no eval sets" in capsys.readouterr().out.lower()
