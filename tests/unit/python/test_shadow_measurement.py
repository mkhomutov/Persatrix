"""RFC 0049 PR 4 — the shadow→live promotion measurement consumer.

Pins :mod:`evaluators.shadow_measurement`: tier partitioning of the merged
trace stream, per-tier summaries that read BOTH withhold fields (the 0031
amendment trace-shape contract), the three verdict criteria — each proven
able to go red — and the CLI over a suite report artifact. The
``DEFAULT_TIER_BOUNDS`` drift pin keeps the pure module's defaults equal to
the live runtime recall limits without the module importing ``agents``.
"""

from __future__ import annotations

import json
from pathlib import Path

from evaluators.shadow_measurement import (
    DEFAULT_TIER_BOUNDS,
    partition_traces,
    promotion_verdict,
    summarize_tier,
)
from evaluators.shadow_measurement import main as measurement_main


def _facts_trace(**overrides) -> dict:
    trace = {
        "tier": "facts",
        "agent_id": "ember-owl",
        "acting": "internal",
        "candidates": [
            {
                "fact_id": "f-1",
                "subject": "atlas",
                "predicate": "topic.has_deadline",
                "protection_level": "internal",
                "session_id": "room-a",
                "source_channel_id": None,
            },
        ],
        "withheld": 0,
        "unknown_label": 0,
    }
    trace.update(overrides)
    return trace


def _episodic_trace(**overrides) -> dict:
    trace = {
        "tier": "episodic",
        "agent_id": "ember-owl",
        "acting": "internal",
        "candidates": [
            {
                "episode_id": "e-1",
                "rank": 2,
                "protection_level": "internal",
                "session_id": "room-a",
                "source_channel_id": None,
            },
        ],
        "withheld": 0,
        "unknown_label": 0,
    }
    trace.update(overrides)
    return trace


# ─── partitioning ────────────────────────────────────────────────────────────


class TestPartitionTraces:
    def test_partitions_on_tier_key(self):
        parts = partition_traces([_facts_trace(), _episodic_trace(), _facts_trace()])
        assert sorted(parts) == ["episodic", "facts"]
        assert len(parts["facts"]) == 2
        assert len(parts["episodic"]) == 1

    def test_missing_tier_surfaces_as_unknown(self):
        """A tier-less trace (a shape regression, or a future third tier) is
        surfaced under ``unknown`` — never silently uncounted."""
        parts = partition_traces([{"candidates": []}, _facts_trace(tier="")])
        assert sorted(parts) == ["unknown"]
        assert len(parts["unknown"]) == 2


# ─── per-tier summaries ──────────────────────────────────────────────────────


class TestSummarizeTier:
    def test_counts_candidates_and_both_withhold_fields(self):
        traces = [
            _facts_trace(candidates=[{"fact_id": "f-1"}, {"fact_id": "f-2"}]),
            _facts_trace(withheld=3, unknown_label=1, candidates=[]),
        ]
        s = summarize_tier("facts", traces)
        assert s.trace_count == 2
        assert s.candidate_count == 2
        assert s.max_candidates_per_turn == 2
        assert s.withheld == 3
        assert s.unknown_label == 1

    def test_acting_null_counted_as_floored_not_dropped(self):
        """#783 note 4: ``acting: null`` = unstamped-floored-to-public. The
        summary counts it; nothing about the trace is discarded."""
        s = summarize_tier("facts", [_facts_trace(acting=None)])
        assert s.acting_floored == 1
        assert s.candidate_count == 1

    def test_max_rank_tracks_episodic_displacement(self):
        traces = [
            _episodic_trace(),
            _episodic_trace(candidates=[{"episode_id": "e-2", "rank": 4}]),
        ]
        assert summarize_tier("episodic", traces).max_rank == 4

    def test_max_rank_none_when_no_candidate_carries_rank(self):
        assert summarize_tier("facts", [_facts_trace()]).max_rank is None


# ─── the verdict criteria (each can go red) ──────────────────────────────────


class TestPromotionVerdict:
    def test_green_on_clean_traces(self):
        verdict = promotion_verdict(
            [_facts_trace(), _episodic_trace()], goldens_green=True,
        )
        assert verdict.green
        assert verdict.criteria == {
            "label_integrity": True,
            "bounded_volume": True,
            "continuity": True,
        }

    def test_unknown_label_fails_label_integrity(self):
        """Clean above-rank withholds are the gate WORKING and do not fail
        promotion; an unparseable stored label does."""
        clean_withhold = promotion_verdict(
            [_facts_trace(withheld=5)], goldens_green=True,
        )
        assert clean_withhold.green
        corrupt = promotion_verdict(
            [_facts_trace(unknown_label=1)], goldens_green=True,
        )
        assert not corrupt.green
        assert corrupt.criteria["label_integrity"] is False

    def test_over_bound_turn_fails_bounded_volume(self):
        flood = [_episodic_trace(
            candidates=[{"episode_id": f"e-{i}", "rank": i} for i in range(9)],
        )]
        verdict = promotion_verdict(flood, goldens_green=True)
        assert not verdict.green
        assert verdict.criteria["bounded_volume"] is False

    def test_unbounded_tier_fails_bounded_volume(self):
        """A tier this module has no bound for (incl. ``unknown``) is a flood
        by definition — the honest default."""
        verdict = promotion_verdict([{"candidates": []}], goldens_green=True)
        assert verdict.criteria["bounded_volume"] is False

    def test_red_goldens_fail_continuity(self):
        verdict = promotion_verdict([_facts_trace()], goldens_green=False)
        assert not verdict.green
        assert verdict.criteria["continuity"] is False

    def test_empty_traces_verdict_rides_on_continuity(self):
        """No traces = no cross-room deltas observed; the trace criteria are
        vacuously satisfiable, so the verdict reduces to the golden replay."""
        assert promotion_verdict([], goldens_green=True).green
        assert not promotion_verdict([], goldens_green=False).green

    def test_notes_carry_the_783_carry_ins(self):
        verdict = promotion_verdict([], goldens_green=True)
        joined = " ".join(verdict.notes)
        assert "pessimistic" in joined  # #783 note 1
        assert "floored" in joined  # #783 note 4


# ─── the default bounds cannot drift off the runtime constants ───────────────


def test_default_bounds_pin_runtime_recall_limits():
    """The pure module cannot import ``agents``; this pin holds its defaults
    equal to the live recall limits from the outside."""
    from agents.persona_runtime.episodic_section import EPISODIC_RECALL_LIMIT
    from agents.persona_runtime.facts_section import FACTS_RECALL_LIMIT

    assert DEFAULT_TIER_BOUNDS == {
        "episodic": EPISODIC_RECALL_LIMIT,
        "facts": FACTS_RECALL_LIMIT,
    }


# ─── the CLI over a suite report artifact ────────────────────────────────────


class TestCLI:
    def _report(self, tmp_path: Path, *, passed_all: bool, traces: list) -> str:
        report = {
            "summary": {"passed_all": passed_all},
            "evals": [{"eval_id": "EVAL-MEMORY-002", "shadow_traces": traces}],
        }
        p = tmp_path / "report.json"
        p.write_text(json.dumps(report), encoding="utf-8")
        return str(p)

    def test_green_report_exits_zero(self, tmp_path: Path, capsys):
        rc = measurement_main([self._report(
            tmp_path, passed_all=True, traces=[_facts_trace()],
        )])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["green"] is True
        assert out["tiers"][0]["tier"] == "facts"

    def test_red_report_exits_one(self, tmp_path: Path, capsys):
        rc = measurement_main([self._report(
            tmp_path, passed_all=False, traces=[_facts_trace()],
        )])
        assert rc == 1
        assert json.loads(capsys.readouterr().out)["green"] is False
