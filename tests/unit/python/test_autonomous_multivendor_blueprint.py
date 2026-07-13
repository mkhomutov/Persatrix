"""Guards the RFC 0052 PR 9 four-vendor headline blueprint.

``blueprints/autonomous-multivendor/blueprint.yaml`` is the design artifact
behind the v0.3.11 flagship: **four personas, each pinned by an RFC 0033
model alias to a *different* cloud vendor — Anthropic + OpenAI + Gemini +
watsonx.ai — brainstorming one topic in one channel with no human**
(RFC 0052 §Phase 4, the cross-vendor face; the offline `mock`-mapped face
is PR 8's ``autonomous-roundtable``). It is the vivid proof that the
conversation layer is provider-agnostic (RFC 0033 §H — a persona runs on
any vendor with no conversation-layer change) and the single best adoption
demo before v0.4.0.

Like ``autonomous-roundtable`` the blueprint is a **design** artifact —
``persatrix init --blueprint`` is not yet wired (``cli/src/main.rs`` ``Init``
is a stub), and ``blueprints/`` is not schema-validated by ``make validate``
(that walks ``config/`` only). So this test *is* the blueprint's validation
gate — the PR 9 checklist's "four-vendor blueprint validates". It pins the
two things a reader of the headline must be able to trust and that a silent
edit would break:

  1. **The roster is a well-formed, agent-only, capped autonomous channel** —
     four seats, one group channel, the mandatory positive cost cap
     (RFC 0052 Goal #4 — an uncapped autonomous channel is un-creatable),
     a convener distinct from the chair (OQ #1), no human member, and every
     ``extends`` anchor resolving to ``templates/personas.yaml`` (a dangling
     anchor would break a future ``init --blueprint`` — the exact PR #729
     review finding on the sibling ``autonomous-roundtable`` blueprint).

  2. **The four seats really do pin four *distinct cloud vendors*.** The
     blueprint carries its own ``model_aliases`` block (the
     ``config/optimization.yaml`` ``models.aliases`` shape), so the headline
     claim is checkable, not prose: each seat's alias is resolved through the
     **real** RFC 0033 resolver (``agents.model_aliases.resolve`` under the
     ``use_alias_map`` test seam) and the four providers must be exactly
     ``{anthropic, openai, gemini, watsonx}`` — four cloud vendors, no
     duplicate, no ``mock``/``ollama`` local seat sneaking into the headline.
     Every non-local seat is run through the fail-closed missing-price guard
     (``validate_alias_pricing``) so the demo cannot silently ship an
     unpriced alias that would zero the RFC 0023 budget gate for that vendor
     — the very gate the four-vendor mandatory cap is the second bound of
     (RFC 0053 §D).

The live cross-vendor run itself is ``MT-AUTONOMOUS-MULTIPROVIDER-001``
(master-plan Phase 3 / release-prep, all four vendors keyed); this test is
the deterministic CI backbone that keeps the blueprint honest in the
meantime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from agents.model_aliases import resolve, use_alias_map, validate_alias_pricing

# The four cloud vendors the headline pins — each a real ``create_provider``
# branch (``agents/llm_factory.py``) and an RFC 0053 first-class provider. The
# demo is worthless if a seat quietly resolves to the ``mock``/``ollama`` local
# providers, so the seat set must equal this exactly.
FOUR_CLOUD_VENDORS = frozenset({"anthropic", "openai", "gemini", "watsonx"})


def _repo_root() -> Path:
    # tests/unit/python/<file> → parents[3] is the repo root (mirrors
    # test_pyproject_provider_extras._pyproject_path).
    return Path(__file__).resolve().parents[3]


def _blueprint() -> dict[str, Any]:
    path = _repo_root() / "blueprints" / "autonomous-multivendor" / "blueprint.yaml"
    assert path.is_file(), f"four-vendor blueprint missing: {path}"
    with path.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    assert isinstance(doc, dict) and "blueprint" in doc, "malformed blueprint YAML"
    return doc["blueprint"]


def _persona_anchors() -> set[str]:
    """The persona-template keys an ``extends`` reference can resolve to."""
    path = _repo_root() / "templates" / "personas.yaml"
    with path.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    return set(doc["persona_templates"].keys())


def _agents(bp: dict[str, Any]) -> list[dict[str, Any]]:
    agents = bp.get("agents")
    assert isinstance(agents, list) and agents, "blueprint has no agents"
    return agents


def _the_channel(bp: dict[str, Any]) -> dict[str, Any]:
    channels = bp.get("channels")
    assert isinstance(channels, list) and len(channels) == 1, (
        "the four-vendor headline is ONE topic in ONE channel — expected "
        "exactly one channel"
    )
    return channels[0]


def _alias_map(bp: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The blueprint's self-contained ``models.aliases``-shaped block.

    Carried inline (not in a ``config/`` overlay) because the blueprint is a
    self-documenting design artifact: a reader sees the whole four-vendor
    mapping in one file, and this test resolves it through the production
    resolver.
    """
    aliases = bp.get("model_aliases")
    assert isinstance(aliases, dict) and aliases, (
        "blueprint must carry a `model_aliases` block pinning each seat to a "
        "vendor (the config/optimization.yaml models.aliases shape)"
    )
    return aliases


# ─── Roster shape ─────────────────────────────────────────────


def test_blueprint_parses_and_is_identified() -> None:
    bp = _blueprint()
    assert bp.get("id") == "autonomous-multivendor"
    assert isinstance(bp.get("description"), str) and bp["description"].strip()


def test_exactly_four_distinct_seats() -> None:
    agents = _agents(_blueprint())
    ids = [a.get("id") for a in agents]
    assert len(agents) == 4, "the headline is a FOUR-vendor brainstorm — four seats"
    assert all(ids), "every seat needs an id"
    assert len(set(ids)) == 4, f"seat ids must be distinct, got {ids}"


def test_every_extends_anchor_resolves() -> None:
    # The PR #729 review lesson: a dangling `extends` anchor would break a
    # future `init --blueprint`. Every anchor must exist in personas.yaml.
    anchors = _persona_anchors()
    for agent in _agents(_blueprint()):
        extends = agent.get("extends", "")
        assert extends.startswith("templates/personas.yaml#"), (
            f"seat {agent.get('id')!r} must extend a personas.yaml anchor, "
            f"got {extends!r}"
        )
        anchor = extends.split("#", 1)[1]
        assert anchor in anchors, (
            f"seat {agent.get('id')!r} extends unknown anchor {anchor!r} "
            f"(known: {sorted(anchors)})"
        )


def test_single_autonomous_group_channel_with_mandatory_cap() -> None:
    ch = _the_channel(_blueprint())
    assert ch.get("type") == "group", "autonomous convening is a group concept"
    autonomous = ch.get("autonomous")
    assert isinstance(autonomous, dict) and autonomous.get("enabled") is True, (
        "the channel must be armed (`autonomous.enabled: true`)"
    )
    # RFC 0052 Goal #4 — the mandatory cost cap. A single SHARED per-interaction
    # ceiling (not a per-seat cap), and it must be positive: an uncapped
    # autonomous channel is un-creatable (there is no human to stop a runaway).
    cap = ch.get("interaction_budget_tokens")
    assert isinstance(cap, int) and cap > 0, (
        "an autonomous channel requires a positive interaction_budget_tokens cap"
    )


def test_distinct_convener_and_chair_roles() -> None:
    # RFC 0052 OQ #1 — the convener owns the agenda lifecycle; the chair authors
    # the §D closing synthesis. They are DISTINCT roles.
    bp = _blueprint()
    ch = _the_channel(bp)
    convener = ch["autonomous"].get("convener")
    chair = ch.get("escalation_chair_id")
    seat_ids = {a["id"] for a in _agents(bp)}
    assert convener in seat_ids, f"convener {convener!r} is not a seat"
    assert chair in seat_ids, f"escalation_chair_id {chair!r} is not a seat"
    assert convener != chair, "convener and chair must be distinct (OQ #1)"


def test_roster_is_agent_only_and_matches_the_seats() -> None:
    # Autonomous = agent-only. Every channel member is one of the four persona
    # seats — no human/operator member — and the convener + chair are members.
    bp = _blueprint()
    ch = _the_channel(bp)
    seat_ids = {a["id"] for a in _agents(bp)}
    members = ch.get("members")
    assert isinstance(members, list) and members, "channel needs members"
    member_ids = {m["id"] for m in members}
    assert member_ids == seat_ids, (
        f"members {sorted(member_ids)} must be exactly the seats "
        f"{sorted(seat_ids)} (agent-only, no human)"
    )
    assert ch["autonomous"]["convener"] in member_ids
    assert ch["escalation_chair_id"] in member_ids


def test_topic_agenda_and_goal_present() -> None:
    autonomous = _the_channel(_blueprint())["autonomous"]
    assert isinstance(autonomous.get("topic"), str) and autonomous["topic"].strip()
    agenda = autonomous.get("agenda")
    assert isinstance(agenda, list) and agenda, "a brainstorm needs an agenda"
    assert isinstance(autonomous.get("goal"), str) and autonomous["goal"].strip()


# ─── The headline: four seats, four distinct cloud vendors ────


def test_each_seat_pins_a_declared_alias() -> None:
    bp = _blueprint()
    alias_map = _alias_map(bp)
    seat_aliases = [a.get("model") for a in _agents(bp)]
    assert all(seat_aliases), "every seat must pin a `model` alias"
    assert len(set(seat_aliases)) == 4, (
        f"the four seats must pin four DISTINCT aliases, got {seat_aliases}"
    )
    for alias in seat_aliases:
        assert alias in alias_map, (
            f"seat alias {alias!r} is not declared in the blueprint's "
            f"model_aliases block"
        )


def test_four_seats_resolve_to_four_distinct_cloud_vendors() -> None:
    # THE headline invariant, checked through the production RFC 0033 resolver.
    bp = _blueprint()
    alias_map = _alias_map(bp)
    seat_aliases = [a["model"] for a in _agents(bp)]
    with use_alias_map(alias_map):
        providers = [resolve(alias).provider for alias in seat_aliases]
    assert len(set(providers)) == 4, (
        f"each seat must run on a DIFFERENT vendor, got {providers}"
    )
    assert set(providers) == FOUR_CLOUD_VENDORS, (
        f"the four seats must pin exactly {sorted(FOUR_CLOUD_VENDORS)} "
        f"(no local mock/ollama seat in the cross-vendor headline), "
        f"got {sorted(set(providers))}"
    )


def test_every_seat_alias_is_priced_fail_closed() -> None:
    # RFC 0053 §D / RFC 0033 §F — a non-local alias with no price zeroes the
    # derived Go cost table and silently disables the RFC 0023 budget gate for
    # that vendor. The guard fails closed (SystemExit) on any such alias, so a
    # clean return proves every cloud seat is priced.
    alias_map = _alias_map(_blueprint())
    validate_alias_pricing(alias_map)  # raises SystemExit on an unpriced seat


def test_close_summarizer_alias_present_and_priced() -> None:
    # The RFC 0020 close summary (every persona's readable artifact) resolves
    # the `summarizer` alias (context_management.summarization.model). The
    # blueprint declares it so the four-vendor close path is complete, and it
    # must resolve to a real cloud vendor and be priced.
    alias_map = _alias_map(_blueprint())
    assert "summarizer" in alias_map, (
        "the blueprint must declare a `summarizer` alias for the RFC 0020 "
        "close-summary path"
    )
    with use_alias_map(alias_map):
        summ = resolve("summarizer")
    assert summ.provider in FOUR_CLOUD_VENDORS
    # ...and priced. `resolve()` under the `use_alias_map` seam bypasses the
    # fail-closed price guard (it fires only on the config-backed map), so
    # assert the resolved prices directly — an unpriced summarizer would zero
    # its vendor's RFC 0023 budget gate. (The whole-map guard in
    # test_every_seat_alias_is_priced_fail_closed also covers it; this keeps
    # the per-alias claim honest to the test's name.)
    assert summ.input_per_1m_tokens > 0 and summ.output_per_1m_tokens > 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
