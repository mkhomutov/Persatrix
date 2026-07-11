"""RFC 0052 PR 8 (Phase 4a) — the offline ``make demo-autonomous`` face.

The offline demo boots the RFC 0052 ``roundtable`` roster on the zero-cost
``mock`` provider (:class:`agents.llm_offline.MockProvider` + the curated
``config/offline_responses.yaml``), arms it, and convenes it — showing the
whole arc (convene → discuss → synthesize) with **no human turn, no API
key, and $0 spend**. ``make demo-autonomous`` is the booted operator face
and ``MT-AUTONOMOUS-001`` the live one; both rest on the same curated
replies producing a *readable, on-topic* synthesis at zero cost.

This suite is the **deterministic CI backbone** of the PR 8 checklist —
"``make demo-autonomous`` runs offline (mock) and produces a non-empty
synthesis; no keys; spend = 0". Docker (hence a live boot) is out of scope
for CI, so this stands in for it by composing the **exact orchestrator-side
directives** the booted demo dispatches — the Go ``composeConveneDirective`` /
``composeSynthesisDirective`` shapes rendered through the real receiver-side
envelope wrap (``convener.py`` / ``synthesis_turn.py``) — and feeding them
through the **real** mock provider for the SHIPPED ``roundtable`` topic/goal,
pinning that:

* the convener opens on the roundtable topic (monorepo adoption),
* the participants engage on that topic, and
* the chair produces a **non-empty, on-topic synthesized recommendation**.

The mock's persona-flavoured fallback is *always* non-empty, so a bare
"non-empty" check is a weak bar — a demo that fell through to
"…running in offline demo mode…" would pass it while showing a placeholder
instead of a real contribution. These asserts therefore require the
**curated** reply (no fallback sentinel, on-topic keywords) — exactly what
a viewer of the demo sees. The orchestration arc (convene → bounded close →
synthesis-turn dispatch) and the per-persona close-summary contract are
pinned separately by ``internal/channels/autonomous_acceptance_test.go`` and
``tests/unit/python/test_autonomous_phase1_acceptance.py``; what this adds is
the offline face — that the curated ``mock`` replies make that arc *read*.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from agents.llm_offline import MockProvider, reset_cache
from agents.llm_types import StopReason
from agents.persona_runtime.convener import format_convener_opening
from agents.persona_runtime.synthesis_turn import format_synthesis_turn

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHANNELS_YAML = _REPO_ROOT / "config" / "channels.yaml"
_OFFLINE_RESPONSES = _REPO_ROOT / "config" / "offline_responses.yaml"

# The mock's off-script fallback sentinel (``agents/llm_offline.py``
# ``_fallback_reply``): its presence in a reply means NO curated fixture
# matched, i.e. the demo would render a placeholder rather than a real,
# on-topic contribution. Every demo-path reply must stay clear of it.
_FALLBACK_SENTINEL = "deterministic placeholder"

# Open-floor dispositions (the ``roundtable`` uses the RFC 0030 vocabulary;
# ``always`` is the legacy alias). These are the members that answer an
# open-floor convene opener.
_OPEN_FLOOR = {"participant", "chair", "always"}


@pytest.fixture(autouse=True)
def _pin_offline_responses(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Force the mock provider to read the repo's curated replies.

    ``_load_responses`` is ``lru_cache``d and env-driven, so pin the path
    and clear the cache on both sides — otherwise a stale
    ``PERSATRIX_OFFLINE_RESPONSES`` from the ambient env (or a prior test's
    fixture file) would leak in.
    """
    monkeypatch.setenv("PERSATRIX_OFFLINE_RESPONSES", str(_OFFLINE_RESPONSES))
    reset_cache()
    yield
    reset_cache()


def _roundtable() -> dict[str, Any]:
    """The shipped RFC 0052 demo channel from ``config/channels.yaml``."""
    data = yaml.safe_load(_CHANNELS_YAML.read_text(encoding="utf-8"))
    for channel in data.get("channels", []):
        if channel.get("name") == "roundtable":
            return channel
    raise AssertionError("the `roundtable` demo channel is missing from config/channels.yaml")


def _convene_directive(rt: dict[str, Any]) -> str:
    """The EXACT convene stimulus the convener's mock sees on a convene forced
    turn — not a hand-written approximation. Mirrors the orchestrator-side
    ``internal/channels/convene.go`` ``composeConveneDirective`` (topic → agenda
    → goal) and renders it through the real receiver-side path
    (``prompt_assembly`` → ``convener.py`` ``format_convener_opening``, the RFC
    0009 ``<external_data>`` envelope). Composing the real stimulus makes this a
    faithful offline-face proof and a drift guard on the shipped topic wording."""
    auto = rt["autonomous"]
    parts = [f"Topic: {auto['topic']}\n"]
    agenda = auto.get("agenda", [])
    if agenda:
        parts.append("\nAgenda:\n")
        parts.extend(f"{i}. {item}\n" for i, item in enumerate(agenda, 1))
    if auto.get("goal"):
        parts.append(f"\nGoal: {auto['goal']}\n")
    return format_convener_opening("".join(parts).strip())


def _synthesis_directive(rt: dict[str, Any]) -> str:
    """The EXACT synthesis stimulus the chair's mock sees at the §D bounded
    close — the load-bearing offline-face claim. Mirrors ``internal/channels/
    synthesis_close.go`` ``composeSynthesisDirective`` (goal leads, topic
    follows) and renders it through the real receiver-side path
    (``prompt_assembly`` → ``synthesis_turn.py`` ``format_synthesis_turn``,
    which prepends the ``synthesis-turn`` framing snippet before the envelope).
    Because this is the true stimulus, the assertions below pin that the chair
    fires its ``synthes`` §D synthesis fixture — not the ``monorepo`` discussion
    fixture — exactly as the booted ``make demo-autonomous`` would."""
    auto = rt["autonomous"]
    parts: list[str] = []
    if auto.get("goal"):
        parts.append(f"Goal: {auto['goal']}\n")
    if auto.get("topic"):
        parts.append(f"\nTopic: {auto['topic']}\n")
    return format_synthesis_turn("".join(parts).strip())


async def _reply(provider: MockProvider, user_text: str) -> Any:
    return await provider.create_message(
        model="offline",
        messages=[{"role": "user", "content": user_text}],
        system="",
        tools=[],
        max_tokens=512,
        temperature=0.2,
    )


def _assert_zero_cost_turn(response: Any) -> None:
    """Every mock turn ends cleanly with SYNTHETIC usage — no SDK, no
    network, no spend — so the wallet/OTel paths stay populated at $0."""
    assert response.stop_reason == StopReason.END_TURN
    assert response.usage.output_tokens >= 1
    assert response.usage.input_tokens >= 1


class TestOfflineAutonomousDemoSynthesis:
    """The curated offline replies produce a readable convene→synthesize
    transcript for the shipped ``roundtable`` topic — the offline face of
    ``make demo-autonomous``, at $0."""

    async def test_convener_opens_on_the_roundtable_topic(self) -> None:
        rt = _roundtable()
        convener = MockProvider(agent_id=rt["autonomous"]["convener"])

        opener = await _reply(convener, _convene_directive(rt))

        text = opener.text.strip()
        assert text, "the convener must open the discussion with a non-empty turn"
        assert _FALLBACK_SENTINEL not in text.lower(), (
            "the opener must be a CURATED reply, not the mock fallback placeholder"
        )
        assert "monorepo" in text.lower(), (
            "the opener must be on the roundtable topic (monorepo adoption)"
        )
        _assert_zero_cost_turn(opener)

    async def test_participants_engage_on_topic(self) -> None:
        rt = _roundtable()
        # Every open-floor member other than the convener answers the opener.
        responders = [
            m["id"]
            for m in rt["members"]
            if m.get("respond") in _OPEN_FLOOR and m["id"] != rt["autonomous"]["convener"]
        ]
        assert len(responders) >= 2, "the opener needs an open-floor audience to discuss"

        topic = rt["autonomous"]["topic"]
        for agent_id in responders:
            reply = await _reply(MockProvider(agent_id=agent_id), topic)
            text = reply.text.strip()
            assert text, f"{agent_id} must contribute a non-empty turn"
            assert _FALLBACK_SENTINEL not in text.lower(), (
                f"{agent_id}'s discussion turn must be a curated reply, not the fallback"
            )
            assert "monorepo" in text.lower(), f"{agent_id} must engage on the monorepo topic"
            _assert_zero_cost_turn(reply)

    async def test_chair_produces_a_nonempty_synthesis(self) -> None:
        """The headline PR 8 assertion: the offline demo produces a
        non-empty, on-topic synthesized recommendation."""
        rt = _roundtable()
        chair = MockProvider(agent_id=rt["escalation_chair_id"])

        synthesis = await _reply(chair, _synthesis_directive(rt))

        text = synthesis.text.strip()
        assert text, "the demo MUST produce a non-empty synthesis"
        assert _FALLBACK_SENTINEL not in text.lower(), (
            "the synthesis must be a CURATED recommendation, not the mock fallback"
        )
        assert "monorepo" in text.lower(), "the synthesis must be on the roundtable topic"
        assert any(kw in text.lower() for kw in ("recommend", "adopt", "synthes")), (
            "the synthesis must read as a synthesized recommendation (the §D goal)"
        )
        _assert_zero_cost_turn(synthesis)


class TestRoundtableDemoRoster:
    """The shipped ``roundtable`` channel is a coherent, convene-eligible
    roster once armed — a config-drift guard for ``make demo-autonomous``
    (e.g. a persona rename must not silently break the demo)."""

    def test_roundtable_ships_disarmed_but_convene_eligible(self) -> None:
        rt = _roundtable()
        auto = rt["autonomous"]

        assert auto["enabled"] is False, (
            "the bundled roundtable ships DISARMED (safety); the demo arms it at runtime"
        )
        convener = auto["convener"]
        chair = rt["escalation_chair_id"]
        assert convener and chair and convener != chair, (
            "convener owns the agenda, chair owns synthesis — distinct roles (OQ #1)"
        )

        members = {m["id"] for m in rt["members"]}
        assert convener in members, "the convener must be a declared member"
        assert chair in members, "the chair must be a declared member"

        open_floor = [m for m in rt["members"] if m.get("respond") in _OPEN_FLOOR]
        assert len(open_floor) >= 2, (
            "an open-floor convene opener needs an audience (convener + ≥1 responder)"
        )
        assert rt["interaction_budget_tokens"] > 0, (
            "cap-required: an armed autonomous channel must carry a positive cost cap"
        )
