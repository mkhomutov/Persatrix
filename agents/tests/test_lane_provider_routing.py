"""ISSUE-0113 — cross-provider lane routing through ``LLMClient``.

A persona holds ONE client built from its own seat alias, but the shared
role lanes (``fast`` bid / ``summarizer`` close / critic / memory
compression) resolve *their* alias per-call and pass it as ``model_alias``.
Pre-fix, a lane alias declaring a different vendor than the persona's own
still rode the persona's client — a non-matching seat sent the lane's model
ID to its own vendor and 404'd, muting governed bidding on mixed-vendor
rosters (the first live four-vendor run went all-silent through every
round).

These tests pin the routing contract in
:meth:`agents.llm_client.LLMClient._provider_for_alias` and the cached
builder :func:`agents.llm_factory.provider_for_resolved`:

* no alias / test-double primary / unresolvable alias / same-vendor alias
  → the primary provider, byte-for-byte (single-vendor overlays unchanged);
* a cross-vendor alias → a provider built from the RESOLVED alias record
  (RFC 0033 §D as stated), cached process-wide per resolved record;
* a lane provider that cannot be built raises
  :class:`~agents.llm_factory.LaneProviderError` — a plain ``Exception``,
  never ``SystemExit`` — so lane callers fail closed per their own
  contracts;
* end-to-end: the Tier B salience bid runs on the lane provider and the
  persona's own (wrong-vendor) client is never contacted.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.llm_client import LLMClient
from agents.llm_factory import (
    LaneProviderError,
    _lane_cache_key,
    _lane_provider_cache,
    clear_lane_provider_cache,
    provider_for_resolved,
)
from agents.llm_offline import MockProvider
from agents.llm_types import LLMResponse, StopReason, Usage
from agents.model_aliases import resolve, use_alias_map
from agents.salience_bid import evaluate_salience

# ─── Helpers ─────────────────────────────────────────────────


def _response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        stop_reason=StopReason.END_TURN,
        usage=Usage(input_tokens=1, output_tokens=1),
    )


class _FakeProvider:
    """Minimal Protocol-satisfying provider that records its calls."""

    def __init__(self, name: str, text: str = "primary-reply") -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []
        self._text = text

    async def create_message(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return _response(self._text)

    def format_tool_definitions(self, tools: list) -> list:
        return tools

    def append_tool_round(self, messages: list, response: Any, tool_results: list) -> list:
        return messages


class _NamelessProvider(_FakeProvider):
    """A test-double-shaped provider with no usable ``name`` (routing must
    stay on the legacy single-provider path for these)."""

    def __init__(self) -> None:
        super().__init__(name="placeholder")
        del self.name  # instance attr gone; no class attr either


# Alias maps for the seam. ``mock`` is a local provider, so entries need no
# pricing (the guard exempts local, and the seam skips per-resolve checks).
_CROSS_VENDOR_MAP = {"fast": {"provider": "mock", "model": "bid-mock"}}
_SAME_VENDOR_MAP = {
    "fast": {
        "provider": "anthropic",
        "model": "haiku-x",
        "input_per_1m_tokens": 1,
        "output_per_1m_tokens": 5,
    },
}


async def _call(client: LLMClient, model_alias: str | None) -> LLMResponse:
    return await client.create_message(
        model="whatever-physical",
        model_alias=model_alias,
        messages=[{"role": "user", "content": "hi"}],
        system="",
        tools=[],
        max_tokens=16,
        temperature=0.0,
    )


@pytest.fixture(autouse=True)
def _fresh_lane_cache():
    clear_lane_provider_cache()
    yield
    clear_lane_provider_cache()


# ─── Primary-provider paths (behaviour unchanged) ────────────


async def test_no_alias_uses_primary() -> None:
    primary = _FakeProvider("anthropic")
    response = await _call(LLMClient(primary), model_alias=None)
    assert len(primary.calls) == 1
    assert response.text == "primary-reply"


async def test_same_vendor_alias_stays_on_primary() -> None:
    primary = _FakeProvider("anthropic")
    with use_alias_map(_SAME_VENDOR_MAP):
        await _call(LLMClient(primary), model_alias="fast")
    assert len(primary.calls) == 1
    assert not _lane_provider_cache


async def test_unresolvable_alias_falls_back_to_primary() -> None:
    # Every lane bails on its own resolve failure before calling; this arm
    # is defensive — and must swallow the resolver's SystemExit.
    primary = _FakeProvider("anthropic")
    with use_alias_map(_SAME_VENDOR_MAP):
        await _call(LLMClient(primary), model_alias="no-such-alias")
    assert len(primary.calls) == 1


async def test_test_double_primary_skips_routing() -> None:
    primary = _NamelessProvider()
    with use_alias_map(_CROSS_VENDOR_MAP):
        await _call(LLMClient(primary), model_alias="fast")
    assert len(primary.calls) == 1
    assert not _lane_provider_cache


# ─── Cross-vendor routing ────────────────────────────────────


async def test_cross_vendor_alias_routes_to_lane_provider() -> None:
    primary = _FakeProvider("openai")
    with use_alias_map(_CROSS_VENDOR_MAP):
        response = await _call(LLMClient(primary), model_alias="fast")
    # The persona's own client is never contacted; the reply came from the
    # MockProvider the ``fast`` record declares.
    assert primary.calls == []
    assert response.text != "primary-reply"
    assert len(_lane_provider_cache) == 1
    assert isinstance(next(iter(_lane_provider_cache.values())), MockProvider)


async def test_lane_provider_cached_per_resolved_record() -> None:
    with use_alias_map(
        {
            "fast": {"provider": "mock", "model": "bid-mock"},
            "summarizer": {"provider": "mock", "model": "sum-mock"},
        },
    ):
        fast = resolve("fast")
        summarizer = resolve("summarizer")
        assert provider_for_resolved(fast) is provider_for_resolved(fast)
        assert provider_for_resolved(fast) is not provider_for_resolved(summarizer)
    assert len(_lane_provider_cache) == 2


async def test_repointed_alias_misses_stale_cache_entry() -> None:
    with use_alias_map(_CROSS_VENDOR_MAP):
        first = provider_for_resolved(resolve("fast"))
    with use_alias_map({"fast": {"provider": "mock", "model": "other-mock"}}):
        second = provider_for_resolved(resolve("fast"))
    assert first is not second


# ─── Construction failure posture ────────────────────────────


async def test_construction_failure_raises_regular_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # watsonx without a project/space id is create_provider's loud startup
    # SystemExit; the lane path must convert it to a catchable Exception.
    for var in ("WATSONX_PROJECT_ID", "WATSONX_SPACE_ID"):
        monkeypatch.delenv(var, raising=False)
    lane_map = {
        "fast": {
            "provider": "watsonx",
            "model": "granite-x",
            "input_per_1m_tokens": 1,
            "output_per_1m_tokens": 1,
        },
    }
    with use_alias_map(lane_map):
        record = resolve("fast")
        with pytest.raises(LaneProviderError) as excinfo:
            provider_for_resolved(record)
        assert isinstance(excinfo.value, Exception)
        assert not isinstance(excinfo.value, SystemExit)
        # And through the client: the same regular exception propagates so
        # lane callers' ``except Exception`` arms degrade fail-closed.
        primary = _FakeProvider("openai")
        with pytest.raises(LaneProviderError):
            await _call(LLMClient(primary), model_alias="fast")
        assert primary.calls == []


# ─── End-to-end: the Tier B bid on a mixed-vendor roster ─────


async def test_salience_bid_runs_on_lane_provider_cross_vendor() -> None:
    # The persona's own client is a wrong-vendor seat: pre-fix the bid rode
    # it and 404'd to silence. Pre-seed the lane cache with a scripted
    # provider so the full gate → route → parse path is deterministic.
    primary = _FakeProvider("openai")
    with use_alias_map(_CROSS_VENDOR_MAP):
        record = resolve("fast")
        lane = _FakeProvider("mock", text="speak: yes\nscore: 0.95")
        _lane_provider_cache[_lane_cache_key(record)] = lane
        decision = await evaluate_salience(
            llm_client=LLMClient(primary),
            content="What should we do about the rollout?",
            transcript=[],
            agent_id="agent-1",
            persona_name="Iron Fox",
            persona_role="skeptic",
            threshold=0.5,
        )
    assert decision.speak is True
    assert decision.reason == "salient"
    assert primary.calls == []
    assert len(lane.calls) == 1
    # The lane call carries the alias record's physical model, never the
    # persona's own.
    assert lane.calls[0]["model"] == "bid-mock"
