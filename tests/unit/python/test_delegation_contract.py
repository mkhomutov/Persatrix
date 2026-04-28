"""Unit tests for ``agents.sub_agents.delegation`` (RFC 0008 PR 3).

Covers the contract:

- ``DelegationRequest`` schema validation + JSON round-trip
- ``DelegationResult`` schema validation + JSON round-trip
- ``MemoryWriteEntry`` framework-injected ``source_agent`` (rejected on wire)
- ``BudgetEnvelope`` non-negativity invariants
- Closed-set ``status`` validation
"""

from __future__ import annotations

import pytest

from agents.sub_agents.delegation import (
    DEFAULT_MAX_MEMORY_WRITES,
    DEFAULT_TRUST_CEILING,
    BudgetEnvelope,
    DelegationContractError,
    DelegationRequest,
    DelegationResult,
    MemoryWriteEntry,
)


# ─── BudgetEnvelope ─────────────────────────────────────────────


def test_budget_envelope_defaults_are_unbounded() -> None:
    env = BudgetEnvelope()
    assert env.tokens == 0
    assert env.timeout_seconds == 0.0
    assert env.max_llm_calls == 0


def test_budget_envelope_rejects_negative_tokens() -> None:
    with pytest.raises(DelegationContractError, match="tokens"):
        BudgetEnvelope(tokens=-1)


def test_budget_envelope_rejects_negative_timeout() -> None:
    with pytest.raises(DelegationContractError, match="timeout_seconds"):
        BudgetEnvelope(timeout_seconds=-0.5)


def test_budget_envelope_rejects_negative_max_llm_calls() -> None:
    with pytest.raises(DelegationContractError, match="max_llm_calls"):
        BudgetEnvelope(max_llm_calls=-3)


# ─── DelegationRequest ──────────────────────────────────────────


def test_request_defaults_match_rfc() -> None:
    req = DelegationRequest(objective="do x")
    assert req.trust_ceiling == DEFAULT_TRUST_CEILING == 0.8
    assert req.max_memory_writes == DEFAULT_MAX_MEMORY_WRITES == 20
    assert req.allowed_tools == frozenset()
    assert isinstance(req.allowed_tools, frozenset)
    assert req.acceptance_criteria == ()


def test_request_validate_rejects_empty_objective() -> None:
    with pytest.raises(DelegationContractError, match="objective"):
        DelegationRequest(objective="").validate()


def test_request_validate_rejects_trust_ceiling_out_of_range() -> None:
    with pytest.raises(DelegationContractError, match="trust_ceiling"):
        DelegationRequest(objective="x", trust_ceiling=1.5).validate()
    with pytest.raises(DelegationContractError, match="trust_ceiling"):
        DelegationRequest(objective="x", trust_ceiling=-0.1).validate()


def test_request_validate_rejects_negative_max_writes() -> None:
    with pytest.raises(DelegationContractError, match="max_memory_writes"):
        DelegationRequest(objective="x", max_memory_writes=-1).validate()


def test_request_round_trips_through_json() -> None:
    req = DelegationRequest(
        objective="implement feature foo",
        acceptance_criteria=("tests pass", "lint clean"),
        context_package={"version": 1, "step_outputs": []},
        budget=BudgetEnvelope(tokens=4000, timeout_seconds=30.0, max_llm_calls=10),
        allowed_tools=frozenset({"file_read", "file_write"}),
        output_schema={"type": "object"},
        trust_ceiling=0.6,
        max_memory_writes=5,
    )
    raw = req.to_json()
    decoded = DelegationRequest.from_context_value(raw)
    assert decoded.objective == req.objective
    assert decoded.acceptance_criteria == req.acceptance_criteria
    assert decoded.context_package == req.context_package
    assert decoded.budget == req.budget
    assert decoded.allowed_tools == req.allowed_tools
    assert decoded.output_schema == req.output_schema
    assert decoded.trust_ceiling == 0.6
    assert decoded.max_memory_writes == 5


def test_request_from_context_rejects_garbage_json() -> None:
    with pytest.raises(DelegationContractError, match="JSON"):
        DelegationRequest.from_context_value("{ not json")


def test_request_from_context_rejects_array_payload() -> None:
    with pytest.raises(DelegationContractError, match="object"):
        DelegationRequest.from_context_value("[1, 2, 3]")


def test_request_from_context_revalidates_on_deserialise() -> None:
    """PR #222 deep review S4: ``from_context_value`` must re-run
    :meth:`DelegationRequest.validate` so a payload that bypasses the
    spawner (e.g. constructed directly in the sub-agent process from
    ``task.context[DELEGATION_REQUEST_KEY]``) cannot smuggle an empty
    ``objective`` or out-of-range ``trust_ceiling`` past the contract.
    Symmetric with :meth:`DelegationResult.from_metadata_value`, which
    already validates closed-set enums on receipt."""
    bad = (
        '{"version":1,"objective":"  ","acceptance_criteria":[],'
        '"context_package":{},"budget":{"tokens":0,"timeout_seconds":0.0,'
        '"max_llm_calls":0},"allowed_tools":[],"output_schema":{},'
        '"trust_ceiling":0.5,"max_memory_writes":1}'
    )
    with pytest.raises(DelegationContractError, match="objective"):
        DelegationRequest.from_context_value(bad)

    over_ceiling = (
        '{"version":1,"objective":"x","acceptance_criteria":[],'
        '"context_package":{},"budget":{"tokens":0,"timeout_seconds":0.0,'
        '"max_llm_calls":0},"allowed_tools":[],"output_schema":{},'
        '"trust_ceiling":1.5,"max_memory_writes":1}'
    )
    with pytest.raises(DelegationContractError, match="trust_ceiling"):
        DelegationRequest.from_context_value(over_ceiling)


# ─── DelegationResult ───────────────────────────────────────────


def test_result_validate_accepts_closed_set_status() -> None:
    for status in ("completed", "partial", "failed"):
        DelegationResult(summary="s", status=status).validate()


def test_result_validate_rejects_unknown_status() -> None:
    with pytest.raises(DelegationContractError, match="status"):
        DelegationResult(summary="s", status="ok").validate()


def test_result_validate_rejects_non_dict_artifacts() -> None:
    # Frozen dataclass accepts anything at construction; validate catches.
    bad = DelegationResult(summary="s", status="completed", artifacts="not-a-dict")  # type: ignore[arg-type]
    with pytest.raises(DelegationContractError, match="artifacts"):
        bad.validate()


def test_result_round_trips_with_memory_writes() -> None:
    entry = MemoryWriteEntry(
        tier="episodic",
        key="design-decision-1",
        content="Pick async over sync IO for the dispatcher.",
        importance=0.7,
        ttl_seconds=3600.0,
        tags=("decision", "performance"),
        merge_strategy="replace",
    )
    res = DelegationResult(
        summary="Decision recorded",
        status="completed",
        artifacts={"choice": "async"},
        decisions=("use async IO",),
        memory_writes=(entry,),
        risks=("requires Python 3.11+",),
    )
    raw = res.to_json()
    decoded = DelegationResult.from_metadata_value(raw)
    assert decoded.summary == res.summary
    assert decoded.status == "completed"
    assert decoded.artifacts == {"choice": "async"}
    assert decoded.decisions == ("use async IO",)
    assert decoded.risks == ("requires Python 3.11+",)
    assert len(decoded.memory_writes) == 1
    out = decoded.memory_writes[0]
    assert out.tier == "episodic"
    assert out.key == "design-decision-1"
    assert out.tags == ("decision", "performance")
    assert out.ttl_seconds == 3600.0


def test_result_from_metadata_rejects_non_object_payload() -> None:
    with pytest.raises(DelegationContractError, match="object"):
        DelegationResult.from_metadata_value('"just a string"')


def test_result_from_metadata_rejects_bad_memory_writes_shape() -> None:
    with pytest.raises(DelegationContractError, match="memory_writes"):
        DelegationResult.from_metadata_value(
            '{"summary": "s", "status": "completed", "memory_writes": "x"}',
        )


def test_result_from_metadata_revalidates_on_deserialise() -> None:
    """PR #224 (RFC 0008 PR 3a) — S6: ``from_metadata_value`` must
    re-run :meth:`DelegationResult.validate` so a payload reconstructed
    from a replay/audit path (log buffer, persisted task metadata, etc.)
    cannot smuggle an out-of-set ``status`` or non-dict ``artifacts``
    past the contract.  Symmetric with the S4 fix on
    :meth:`DelegationRequest.from_context_value`."""
    bad_status = (
        '{"version":1,"summary":"s","status":"ok",'
        '"artifacts":{},"decisions":[],"memory_writes":[],"risks":[]}'
    )
    with pytest.raises(DelegationContractError, match="status"):
        DelegationResult.from_metadata_value(bad_status)


# ─── MemoryWriteEntry ──────────────────────────────────────────


def test_memory_write_entry_normalises_tags_to_tuple() -> None:
    entry = MemoryWriteEntry(
        tier="notes", key="k", content="c", tags=["a", "b"],  # type: ignore[arg-type]
    )
    assert entry.tags == ("a", "b")
    assert isinstance(entry.tags, tuple)


def test_memory_write_entry_with_source_agent_returns_copy() -> None:
    entry = MemoryWriteEntry(tier="episodic", key="k", content="c")
    stamped = entry.with_source_agent("child-1")
    assert entry.source_agent is None  # original unchanged (frozen)
    assert stamped.source_agent == "child-1"
    assert stamped.key == entry.key
