"""Unit tests for ``agents.sub_agents.merge.MergeEngine`` (RFC 0008 PR 3).

Covers all four merge strategies, JSON Merge Patch corner cases, the
deterministic 6-step merge order, trust-ceiling downscaling, the
``max_memory_writes`` cap, ``source_agent`` spoof rejection, and the
intentional procedural-tier rejection with its dedicated reason.
"""

from __future__ import annotations

import pytest

from agents.sub_agents.delegation import (
    DelegationFailure,
    DelegationRequest,
    DelegationResult,
    MemoryWriteEntry,
)
from agents.sub_agents.merge import (
    REASON_CAP_EXCEEDED,
    REASON_CONFLICT,
    REASON_PROCEDURAL_TIER_REJECTED,
    REASON_SCHEMA_INVALID,
    REASON_SOURCE_AGENT_SET,
    REASON_TRUST_CEILING,
    MergeEngine,
    apply_json_merge_patch,
)


# ─── apply_json_merge_patch ─────────────────────────────────────


def test_merge_patch_overwrites_present_keys() -> None:
    assert apply_json_merge_patch({"a": 1, "b": 2}, {"a": 9}) == {"a": 9, "b": 2}


def test_merge_patch_null_deletes_key() -> None:
    assert apply_json_merge_patch({"a": 1, "b": 2}, {"a": None}) == {"b": 2}


def test_merge_patch_recursive_objects() -> None:
    target = {"outer": {"x": 1, "y": 2}}
    patch = {"outer": {"y": 99, "z": 3}}
    assert apply_json_merge_patch(target, patch) == {"outer": {"x": 1, "y": 99, "z": 3}}


def test_merge_patch_non_object_replaces() -> None:
    assert apply_json_merge_patch({"a": 1}, [1, 2]) == [1, 2]


# ─── MergeEngine.merge_artifacts (caller-aggregation API) ───────


def _engine() -> MergeEngine:
    return MergeEngine()


def test_artifacts_replace_overwrites() -> None:
    assert _engine().merge_artifacts({"a": 1}, {"b": 2}, "replace") == {"b": 2}


def test_artifacts_append_concatenates_lists() -> None:
    out = _engine().merge_artifacts([1, 2], [3, 4], "append")
    assert out == [1, 2, 3, 4]


def test_artifacts_append_rejects_non_list() -> None:
    with pytest.raises(Exception, match="append"):
        _engine().merge_artifacts({"a": 1}, {"b": 2}, "append")


def test_artifacts_patch_applies_json_merge_patch_on_objects() -> None:
    out = _engine().merge_artifacts(
        {"code_review": {"issues": 3, "blocking": True}},
        {"code_review": {"issues": 1, "blocking": None}},
        "patch",
    )
    assert out == {"code_review": {"issues": 1}}


def test_artifacts_patch_unions_tag_lists_order_preserving() -> None:
    out = _engine().merge_artifacts(["a", "b"], ["b", "c"], "patch")
    assert out == ["a", "b", "c"]


def test_artifacts_patch_replaces_strings() -> None:
    assert _engine().merge_artifacts("old", "new", "patch") == "new"


def test_artifacts_reject_on_conflict_admits_when_existing_is_none() -> None:
    assert _engine().merge_artifacts(None, {"a": 1}, "reject_on_conflict") == {"a": 1}


def test_artifacts_reject_on_conflict_raises_on_existing() -> None:
    with pytest.raises(Exception, match="reject_on_conflict"):
        _engine().merge_artifacts({"a": 1}, {"b": 2}, "reject_on_conflict")


def test_artifacts_unknown_strategy_rejected() -> None:
    with pytest.raises(Exception, match="strategy"):
        _engine().merge_artifacts({}, {}, "merge-everything")


# ─── MergeEngine.merge_result — happy path ─────────────────────


def _request(**overrides) -> DelegationRequest:
    base: dict = {
        "objective": "obj",
        "trust_ceiling": 0.8,
        "max_memory_writes": 20,
    }
    base.update(overrides)
    return DelegationRequest(**base)


def _entry(**overrides) -> MemoryWriteEntry:
    base: dict = {"tier": "episodic", "key": "k", "content": "c", "importance": 0.5}
    base.update(overrides)
    return MemoryWriteEntry(**base)


def test_merge_admits_valid_entry_and_injects_source_agent() -> None:
    res = DelegationResult(
        summary="ok", status="completed", memory_writes=(_entry(key="k1"),),
    )
    out = _engine().merge_result(res, _request(), source_agent="child-1")
    assert len(out.admitted) == 1
    assert out.admitted[0].source_agent == "child-1"
    assert out.admitted[0].key == "k1"
    assert out.rejected == []


def test_merge_failure_on_invalid_status_raises_delegation_failure() -> None:
    res = DelegationResult(summary="x", status="bogus")
    with pytest.raises(DelegationFailure, match="schema invalid"):
        _engine().merge_result(res, _request(), source_agent="c")


# ─── Deterministic merge order — per-step rejections ────────────


def test_cap_exceeded_rejects_extras_in_input_order() -> None:
    entries = tuple(_entry(key=f"k{i}") for i in range(25))
    res = DelegationResult(summary="s", status="completed", memory_writes=entries)
    out = _engine().merge_result(res, _request(max_memory_writes=20), source_agent="c")
    assert len(out.admitted) == 20
    assert len(out.rejected) == 5
    assert all(r.reason == REASON_CAP_EXCEEDED for r in out.rejected)
    assert [r.index for r in out.rejected] == [20, 21, 22, 23, 24]


def test_trust_ceiling_downscales_importance() -> None:
    res = DelegationResult(
        summary="s",
        status="completed",
        memory_writes=(_entry(importance=0.95),),
    )
    out = _engine().merge_result(res, _request(trust_ceiling=0.8), source_agent="c")
    assert len(out.admitted) == 1
    assert out.admitted[0].importance == pytest.approx(0.8)


def test_source_agent_spoof_rejected() -> None:
    res = DelegationResult(
        summary="s",
        status="completed",
        memory_writes=(_entry(source_agent="impostor"),),
    )
    out = _engine().merge_result(res, _request(), source_agent="legit")
    assert out.admitted == []
    assert len(out.rejected) == 1
    assert out.rejected[0].reason == REASON_SOURCE_AGENT_SET


def test_procedural_tier_uses_dedicated_reason() -> None:
    res = DelegationResult(
        summary="s",
        status="completed",
        memory_writes=(_entry(tier="procedural"),),
    )
    out = _engine().merge_result(res, _request(), source_agent="c")
    assert out.admitted == []
    assert out.rejected[0].reason == REASON_PROCEDURAL_TIER_REJECTED


def test_unknown_tier_uses_schema_invalid() -> None:
    res = DelegationResult(
        summary="s",
        status="completed",
        memory_writes=(_entry(tier="cosmic"),),
    )
    out = _engine().merge_result(res, _request(), source_agent="c")
    assert out.rejected[0].reason == REASON_SCHEMA_INVALID


def test_empty_key_or_content_rejected_with_schema_invalid() -> None:
    res = DelegationResult(
        summary="s",
        status="completed",
        memory_writes=(_entry(key="   "), _entry(content="")),
    )
    out = _engine().merge_result(res, _request(), source_agent="c")
    assert len(out.rejected) == 2
    assert {r.reason for r in out.rejected} == {REASON_SCHEMA_INVALID}


def test_importance_out_of_range_rejected() -> None:
    res = DelegationResult(
        summary="s",
        status="completed",
        memory_writes=(_entry(importance=1.5),),
    )
    out = _engine().merge_result(res, _request(), source_agent="c")
    assert out.rejected[0].reason == REASON_SCHEMA_INVALID


def test_negative_ttl_rejected() -> None:
    res = DelegationResult(
        summary="s",
        status="completed",
        memory_writes=(_entry(ttl_seconds=-1),),
    )
    out = _engine().merge_result(res, _request(), source_agent="c")
    assert out.rejected[0].reason == REASON_SCHEMA_INVALID


def test_unknown_merge_strategy_rejected() -> None:
    res = DelegationResult(
        summary="s",
        status="completed",
        memory_writes=(_entry(merge_strategy="explode"),),
    )
    out = _engine().merge_result(res, _request(), source_agent="c")
    assert out.rejected[0].reason == REASON_SCHEMA_INVALID


def test_reject_on_conflict_with_existing_key_rejects() -> None:
    res = DelegationResult(
        summary="s",
        status="completed",
        memory_writes=(_entry(key="known", merge_strategy="reject_on_conflict"),),
    )
    out = _engine().merge_result(
        res, _request(), source_agent="c", existing_keys=["known"],
    )
    assert out.rejected[0].reason == REASON_CONFLICT


def test_reject_on_conflict_with_in_merge_duplicate_rejects_second() -> None:
    res = DelegationResult(
        summary="s",
        status="completed",
        memory_writes=(
            _entry(key="dup", merge_strategy="reject_on_conflict"),
            _entry(key="dup", merge_strategy="reject_on_conflict"),
        ),
    )
    out = _engine().merge_result(res, _request(), source_agent="c")
    assert len(out.admitted) == 1
    assert out.rejected[0].reason == REASON_CONFLICT
    assert out.rejected[0].index == 1


# ─── Metric callback ────────────────────────────────────────────


def test_metric_callback_invoked_with_labels() -> None:
    captured: list[tuple[str, dict, int]] = []

    def sink(metric: str, labels: dict, value: int) -> None:
        captured.append((metric, dict(labels), value))

    engine = MergeEngine(on_metric=sink)
    res = DelegationResult(
        summary="s",
        status="completed",
        memory_writes=(_entry(importance=0.95),),
    )
    engine.merge_result(res, _request(trust_ceiling=0.8), source_agent="c")
    metric_names = [m for m, _, _ in captured]
    assert "delegation_merge_outcome" in metric_names
    assert "delegation_memory_writes_admitted" in metric_names
    # Trust-ceiling downscale emitted under admitted with reason label.
    trust = [
        labels for m, labels, _ in captured
        if m == "delegation_memory_writes_admitted" and labels.get("reason") == REASON_TRUST_CEILING
    ]
    assert trust


def test_artifacts_merged_via_json_merge_patch_when_existing_supplied() -> None:
    res = DelegationResult(
        summary="s",
        status="completed",
        artifacts={"a": 1, "b": None},
    )
    out = _engine().merge_result(
        res, _request(), source_agent="c", existing_artifacts={"a": 0, "b": 5, "c": 3},
    )
    assert out.artifacts == {"a": 1, "c": 3}
