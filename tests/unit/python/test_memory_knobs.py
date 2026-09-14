"""The persona's memory-knob resolution seam (v0.3.16 PR A2).

Six knobs — five per-agent ``memory.*`` keys from three RFCs plus, since
v0.3.16 K1, the fleet-wide ``memory_budget.tokens`` off
``optimization.yaml`` — resolved once at agent construction. The
extraction is pure structure — what this module pins is that the
aggregate agrees with each individual resolver, that a bad value is
still rejected **loudly** (silent degradation is what every one of these
knobs exists to prevent), and that the defaults are the shipped posture.
The suite-wide ``isolate_optimization_config`` fixture (``tests/_test_infra``)
unsets ``PERSATRIX_OPTIMIZATION_CONFIG`` first, so the budget here is
always the shipped one; the configured path is pinned in
``test_optimization_memory_budget.py``.
"""

from __future__ import annotations

import pytest

from agents.persona_runtime.audience import (
    AUDIENCE_LIVE,
    AUDIENCE_SHADOW,
    resolve_memory_audience,
)
from agents.persona_runtime.cross_room import (
    CROSS_ROOM_LIVE,
    CROSS_ROOM_SHADOW,
    resolve_episodic_cross_room,
    resolve_facts_cross_room,
)
from agents.persona_runtime.facts_section import resolve_facts_config
from agents.persona_runtime.memory_budget import MEMORY_BUDGET_TOKENS
from agents.persona_runtime.memory_knobs import resolve_memory_knobs

_CONFIGS: list[dict] = [
    {},
    {"memory": {}},
    {"memory": {"facts": {"enabled": False, "budget_tokens": 42,
                          "cross_room": "shadow"}}},
    {"memory": {"episodic": {"cross_room": "off"},
                "egress": {"audience": "live"}}},
]


@pytest.mark.parametrize("config", _CONFIGS)
def test_the_aggregate_agrees_with_every_individual_resolver(
    config: dict,
) -> None:
    knobs = resolve_memory_knobs(config)
    assert (knobs.facts_enabled, knobs.facts_budget_tokens) == (
        resolve_facts_config(config)
    )
    assert knobs.facts_cross_room == resolve_facts_cross_room(config)
    assert knobs.episodic_cross_room == resolve_episodic_cross_room(config)
    assert knobs.audience == resolve_memory_audience(config)
    # The fleet-wide value is not a function of the persona config.
    assert knobs.budget_tokens == MEMORY_BUDGET_TOKENS


def test_the_defaults_are_the_shipped_posture() -> None:
    knobs = resolve_memory_knobs({})
    assert knobs.facts_enabled is True
    assert knobs.budget_tokens == MEMORY_BUDGET_TOKENS == 1500
    assert knobs.facts_cross_room == CROSS_ROOM_LIVE
    assert knobs.episodic_cross_room == CROSS_ROOM_LIVE
    # Shadow for the whole v0.3.16 cycle (scope lock 1).
    assert knobs.audience == AUDIENCE_SHADOW


def test_a_bad_value_is_still_rejected_at_construction() -> None:
    """The shared contract: a persona that silently ran a different
    posture from the one its operator asked for would misreport what the
    deployment is doing."""
    with pytest.raises(ValueError, match="memory.egress.audience"):
        resolve_memory_knobs({"memory": {"egress": {"audience": "on"}}})
    with pytest.raises(ValueError, match="memory.facts.cross_room"):
        resolve_memory_knobs({"memory": {"facts": {"cross_room": "maybe"}}})


def test_knobs_are_independent_of_each_other() -> None:
    knobs = resolve_memory_knobs({"memory": {
        "facts": {"cross_room": "shadow"},
        "egress": {"audience": "live"},
    }})
    assert knobs.facts_cross_room == CROSS_ROOM_SHADOW
    assert knobs.episodic_cross_room == CROSS_ROOM_LIVE
    assert knobs.audience == AUDIENCE_LIVE
