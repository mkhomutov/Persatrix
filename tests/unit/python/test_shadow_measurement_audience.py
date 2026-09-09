"""ISSUE-0132 (v0.3.16 PR A2) — the audience criterion in the verdict.

Lock 1's fourth criterion.  The first three can only go red on a defect;
this one exists because in shadow *nothing else moves*, so a
defect-only verdict would go green on a run where no disjoint audience
ever occurred.  What is pinned here is that the criterion is **not**
satisfiable by an empty or same-room-only sample, that the delta and its
per-cause / per-room-shape breakdown are reported, and that asking for
it is explicit — inferring it from the traces would reintroduce exactly
the vacuous pass it guards against.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluators.shadow_measurement import (
    NOTE_FETCH_FAILED_IS_LIVE_ONLY,
    main,
    promotion_verdict,
    room_shape,
    summarize_audience,
)

DM = "dm:alice:iron-fox"
STANDUP = "group:standup"


def _trace(acting_channel_id: str, verdicts: dict[str, int], **extra) -> dict:
    return {
        "tier": "audience",
        "agent_id": "iron-fox",
        "acting": "internal",
        "acting_channel_id": acting_channel_id,
        "mode": "shadow",
        "candidates": [],
        "verdicts": {
            "admit": 0, "withhold-disjoint": 0,
            "withhold-unknown-fetch-failed": 0,
            "withhold-unknown-no-provenance": 0,
            **verdicts,
        },
        "withheld": 0,
        "unknown_label": 0,
        "source_rooms": extra.get("source_rooms", 1),
        "fetches": extra.get("fetches", 1),
    }


def test_room_shape_reads_the_channel_prefix_vocabulary() -> None:
    assert room_shape(DM) == "dm"
    assert room_shape(STANDUP) == "group"
    assert room_shape("thread:abc") == "thread"
    assert room_shape("weird") == "other"
    assert room_shape(None) == "unknown"


def test_the_delta_is_disjoint_over_judged() -> None:
    summary = summarize_audience([
        _trace(STANDUP, {"withhold-disjoint": 3, "admit": 1}),
        _trace(DM, {"admit": 4}),
    ])
    assert summary.judged == 8
    assert summary.withhold_share == pytest.approx(3 / 8)
    assert summary.unknown_share == 0.0
    assert summary.verdicts["withhold-disjoint"] == 3


def test_the_delta_is_reported_per_cause_and_per_room_shape() -> None:
    """Lock 1: 'reported per cause and per acting-room shape' — a DM with
    the wrong person and a three-persona standup fail differently, and a
    single share would hide which."""
    summary = summarize_audience([
        _trace(STANDUP, {"withhold-disjoint": 2}),
        _trace(DM, {"withhold-unknown-no-provenance": 1, "admit": 1}),
    ])
    assert summary.by_room_shape == {
        "group": {"withhold-disjoint": 2},
        "dm": {"withhold-unknown-no-provenance": 1, "admit": 1},
    }
    assert summary.unknown_share == pytest.approx(1 / 4)


def test_the_cost_bound_is_reported_not_asserted() -> None:
    summary = summarize_audience([
        _trace(STANDUP, {"admit": 1}, fetches=2, source_rooms=3),
    ])
    assert (summary.fetches, summary.source_rooms) == (2, 3)


# ─── the criterion itself ──────────────────────────────────


def _verdict(traces: list[dict], **kwargs):
    return promotion_verdict(
        traces, goldens_green=True, audience_expected=True, **kwargs,
    )


def test_a_disjoint_withhold_makes_the_criterion_green() -> None:
    verdict = _verdict([_trace(STANDUP, {"withhold-disjoint": 1, "admit": 2})])
    assert verdict.criteria["audience_delta_measured"] is True
    assert verdict.green


def test_no_audience_traces_at_all_is_red_not_silent() -> None:
    verdict = _verdict([])
    assert verdict.criteria["audience_delta_measured"] is False
    assert not verdict.green


def test_an_admit_only_run_is_vacuous_not_passing() -> None:
    """The risk row this criterion answers: 'the shadow verdict goes green
    on data that never exercised a disjoint audience'."""
    verdict = _verdict([_trace(DM, {"admit": 12})])
    assert verdict.criteria["audience_delta_measured"] is False


def test_unknown_causes_alone_do_not_satisfy_it() -> None:
    """A run whose every entry lost its roster measured the harness, not
    the feature."""
    verdict = _verdict([
        _trace(STANDUP, {
            "withhold-unknown-fetch-failed": 5,
            "withhold-unknown-no-provenance": 5,
        }),
    ])
    assert verdict.criteria["audience_delta_measured"] is False


def test_the_criterion_is_absent_unless_asked_for() -> None:
    """The RFC 0049 verdict must keep rendering exactly as it did — and a
    caller who forgot the flag must not silently get a three-criterion
    green labelled as four."""
    verdict = promotion_verdict([], goldens_green=True)
    assert "audience_delta_measured" not in verdict.criteria
    assert verdict.audience is None
    assert verdict.green


def test_the_live_only_caveat_rides_the_verdict() -> None:
    verdict = _verdict([_trace(STANDUP, {"withhold-disjoint": 1})])
    assert NOTE_FETCH_FAILED_IS_LIVE_ONLY in verdict.notes
    assert "fetch-failed" in " ".join(verdict.notes)


def test_the_audience_summary_rides_to_dict() -> None:
    verdict = _verdict([_trace(STANDUP, {"withhold-disjoint": 1, "admit": 1})])
    payload = verdict.to_dict()
    assert payload["audience"]["withhold_share"] == pytest.approx(0.5)
    assert payload["audience"]["by_room_shape"]["group"]["withhold-disjoint"] == 1


# ─── the CLI flag ──────────────────────────────────────────


def _report(tmp_path: Path, traces: list[dict]) -> str:
    path = tmp_path / "report.json"
    path.write_text(json.dumps({
        "summary": {"passed_all": True},
        "evals": [{"id": "EVAL-MEMORY-005", "shadow_traces": traces}],
    }), encoding="utf-8")
    return str(path)


def test_cli_audience_flag_renders_the_fourth_criterion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    report = _report(tmp_path, [_trace(STANDUP, {"withhold-disjoint": 1})])
    assert main([report, "--audience"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["criteria"]["audience_delta_measured"] is True
    assert payload["audience"]["judged"] == 1


def test_cli_exits_nonzero_on_a_vacuous_audience_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    report = _report(tmp_path, [_trace(DM, {"admit": 3})])
    assert main([report, "--audience"]) == 1
    assert json.loads(capsys.readouterr().out)["green"] is False
