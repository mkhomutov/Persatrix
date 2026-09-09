"""The persona's per-agent memory knobs, resolved once at construction.

Five values, five different RFCs, one shape: read a `memory.*` key off
the persona config, reject a bad value **loudly** rather than degrading
silently, and hand the result to ``_LLMPersonaAgent.__init__``. They
were resolved inline there until v0.3.16 PR A2 needed a sixth line in a
file already pinned at its size-cap headroom (ISSUE-0053) — and inline
resolution was always slightly wrong for them anyway: a reader of
``__init__`` had to know that three unrelated modules each own one
knob, and a reviewer had to check three call sites to answer "what does
this persona's memory config actually do?"

Loud rejection is the shared contract and the reason this is one seam:
a persona that silently ran a different posture from the one its
operator asked for would misreport what the deployment is doing, and
every one of these knobs exists to be *stated*.
"""

from __future__ import annotations

from dataclasses import dataclass

from .audience import resolve_memory_audience
from .cross_room import resolve_episodic_cross_room, resolve_facts_cross_room
from .facts_section import resolve_facts_config

__all__ = ["MemoryKnobs", "resolve_memory_knobs"]


@dataclass(frozen=True)
class MemoryKnobs:
    """One persona's resolved memory configuration."""

    #: RFC 0026 PR 3 — ``memory.facts.enabled`` / ``.budget_tokens``.
    facts_enabled: bool
    facts_budget_tokens: int
    #: RFC 0049 PR 2/PR 4 — ``memory.{facts,episodic}.cross_room``.
    facts_cross_room: str
    episodic_cross_room: str
    #: ISSUE-0132 (v0.3.16 A2) — ``memory.egress.audience``.
    audience: str


def resolve_memory_knobs(config: dict) -> MemoryKnobs:
    """Resolve every memory knob, raising on the first bad value.

    Order is the order they were resolved inline, so a config with two
    bad values reports the same one it always did.
    """
    facts_enabled, facts_budget_tokens = resolve_facts_config(config)
    return MemoryKnobs(
        facts_enabled=facts_enabled,
        facts_budget_tokens=facts_budget_tokens,
        facts_cross_room=resolve_facts_cross_room(config),
        episodic_cross_room=resolve_episodic_cross_room(config),
        audience=resolve_memory_audience(config),
    )
