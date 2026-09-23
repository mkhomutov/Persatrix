"""EXP-001 harness — the materials loader.

The loader reads the pre-registered series files and refuses any file whose
shape the run could not use: a meeting out of order, a key naming a fact the
briefing never gave, a date off the story calendar. The run then reads each
meeting's story date from here to pin the advisers' clocks (check 7).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml

from evaluators.exp001.materials import (
    MaterialsError,
    MeetingKind,
    load_materials,
    load_series,
)

_REAL_DIR = Path(__file__).resolve().parents[3] / "evaluators" / "experiments" / "EXP-001"


def _meeting(mid: str, kind: str, day: str, key: Any = None) -> dict[str, Any]:
    m: dict[str, Any] = {"id": mid, "kind": kind, "message": f"Today is {day}.\n\nHello.\n"}
    if key is not None:
        m["key"] = key
    return m


def _plan_key(
    facts: dict[str, Any] | None = None, unsound: list[str] | None = None
) -> dict[str, Any]:
    return {
        "problems": {"K1": "one", "K2": "two", "K3": "three"},
        "earlier_facts": facts or {},
        "unsound_options": unsound or [],
    }


def _series(**overrides: Any) -> dict[str, Any]:
    fact_use = {"F1": {"implication": "why", "must_state": ["detail"]}}
    doc: dict[str, Any] = {
        "id": "series-9",
        "organisation": "Test Org, a shop",
        "facts": {"F1": "a fact", "F2": "another"},
        "meetings": [
            _meeting("series-9-briefing", "briefing", "Monday 6 October 2036"),
            _meeting(
                "series-9-plan-1", "plan", "Monday 13 October 2036", _plan_key(fact_use, ["A"])
            ),
            _meeting("series-9-plan-2", "control", "Monday 20 October 2036", _plan_key()),
            _meeting(
                "series-9-plan-3", "plan", "Monday 27 October 2036", _plan_key(fact_use, ["B"])
            ),
            _meeting(
                "series-9-plan-4", "plan", "Monday 3 November 2036", _plan_key(fact_use, ["A", "C"])
            ),
            _meeting(
                "series-9-recall",
                "recall",
                "Monday 10 November 2036",
                {"R1": {"fact": "F2", "answer": "yes", "must_include": ["yes"]}},
            ),
        ],
    }
    doc.update(overrides)
    return doc


def _shift_one_week(doc: dict[str, Any]) -> None:
    """Move every meeting one week later, keeping them a week apart."""
    for m in doc["meetings"]:
        first, rest = m["message"].split("\n", 1)
        date = dt.datetime.strptime(first, "Today is %A %d %B %Y.").date() + dt.timedelta(days=7)
        day = f"{date.strftime('%A')} {date.day} {date.strftime('%B %Y')}"
        m["message"] = f"Today is {day}.\n{rest}"


def _write(tmp_path: Path, doc: dict[str, Any], name: str = "series-9.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def test_loads_a_well_formed_series(tmp_path: Path) -> None:
    series = load_series(_write(tmp_path, _series()))

    assert series.id == "series-9"
    assert [m.kind for m in series.meetings] == [
        MeetingKind.BRIEFING,
        MeetingKind.PLAN,
        MeetingKind.CONTROL,
        MeetingKind.PLAN,
        MeetingKind.PLAN,
        MeetingKind.RECALL,
    ]
    assert series.meetings[0].story_date == dt.date(2036, 10, 6)
    assert [m.id for m in series.scored_meetings] == [
        "series-9-plan-1",
        "series-9-plan-2",
        "series-9-plan-3",
        "series-9-plan-4",
    ]
    plan = series.meetings[1]
    assert plan.plan_key is not None
    assert plan.plan_key.unsound_options == ("A",)
    assert plan.plan_key.earlier_facts["F1"].must_state == ("detail",)
    recall = series.meetings[-1]
    assert recall.recall_key is not None
    assert recall.recall_key["R1"].must_include == ("yes",)


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda d: d["meetings"].pop(0), "briefing"),
        (lambda d: d["meetings"].pop(), "recall"),
        (lambda d: d["meetings"][2].update(kind="recall"), "kind"),
        (lambda d: d["meetings"][1].update(kind="lecture"), "kind"),
        (
            lambda d: d["meetings"][1]["key"]["earlier_facts"].update(
                F7={"implication": "x", "must_state": ["y"]}
            ),
            "F7",
        ),
        (lambda d: d["meetings"][1]["key"].update(unsound_options=["D"]), "unsound"),
        (lambda d: d["meetings"][2]["key"].update(unsound_options=["A"]), "control"),
        (
            lambda d: d["meetings"][1]["key"]["earlier_facts"]["F1"].update(must_state=[]),
            "must_state",
        ),
        (lambda d: d["meetings"][-1]["key"]["R1"].update(fact="F9"), "F9"),
        (lambda d: d["meetings"][3].update(message="No date here.\n"), "Today is"),
        (
            lambda d: d["meetings"][3].update(message="Today is Monday 28 October 2036.\n"),
            "weekday",
        ),
        (
            lambda d: d["meetings"][3].update(message="Today is Tuesday 28 October 2036.\n"),
            "one week",
        ),
        (lambda d: d["meetings"][4].update(id="series-9-plan-3"), "duplicate"),
        (lambda d: d["meetings"][1]["key"].update(unsound_options=[]), "unsound"),
        (
            lambda d: d["meetings"][3].update(message="Today is Monday 31 Sept 2036.\n"),
            "series-9-plan-3",
        ),
        (lambda d: d["meetings"][1].pop("key"), "series-9-plan-1: .*key"),
        (lambda d: d["meetings"][-1].pop("key"), "series-9-recall: .*key"),
        (
            lambda d: [
                m.update(message=m["message"].replace("October", "Octobre")) for m in d["meetings"]
            ],
            "Octobre",
        ),
        (lambda d: _shift_one_week(d), "6 October 2036"),
    ],
)
def test_rejects_a_series_the_run_could_not_use(tmp_path: Path, mutate: Any, needle: str) -> None:
    doc = _series()
    mutate(doc)
    with pytest.raises(MaterialsError, match=needle):
        load_series(_write(tmp_path, doc))


def test_the_real_materials_match_the_preregistered_shape() -> None:
    """Pre-registration §1: 5 series, 20 scored plans, 5 of them controls."""
    materials = load_materials(_REAL_DIR)

    assert [s.id for s in materials.series] == [f"series-{n}" for n in range(1, 6)]
    scored = [m for s in materials.series for m in s.scored_meetings]
    assert len(scored) == 20
    assert sum(m.kind is MeetingKind.CONTROL for m in scored) == 5
    fact_plans = [m for m in scored if m.kind is MeetingKind.PLAN]
    assert all(m.plan_key and m.plan_key.earlier_facts for m in fact_plans)
    assert all(len(s.meetings) == 6 for s in materials.series)
    assert all(s.meetings[0].story_date == dt.date(2036, 10, 6) for s in materials.series)
    assert materials.practice.id == "practice"
    assert len(materials.practice.scored_meetings) == 2


def test_real_materials_reject_a_missing_series(tmp_path: Path) -> None:
    for src in _REAL_DIR.glob("*.yaml"):
        if src.name != "series-3.yaml":
            (tmp_path / src.name).write_text(src.read_text())
    with pytest.raises(MaterialsError, match="series-3"):
        load_materials(tmp_path)
