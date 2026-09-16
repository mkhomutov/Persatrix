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

**The floor, not just the replies (ISSUE-0160).** The first two classes feed
each participant the opener directly, so they passed while the booted demo
showed the convener talking to an empty room: in the real arc a participant
only replies after its open-floor bid says so, and the mock answered every bid
with prose the gate could not parse. ``TestOfflineRoundtableFloor`` therefore
plays the floor through the real gate (:func:`agents.salience_bid.evaluate_salience`,
``reasoning.mode: bid`` as the roundtable resolves it): who bids, what they say,
whether anyone repeats themselves, and whether the convener's agenda advances
and the chair's escalation read as their own turns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from agents.llm_client import LLMClient
from agents.llm_offline import MockProvider, reset_cache
from agents.llm_types import StopReason
from agents.model_aliases import use_alias_map
from agents.persona_runtime.convener import format_convener_opening
from agents.persona_runtime.prompt_assembly import format_chair_escalation
from agents.persona_runtime.synthesis_turn import format_synthesis_turn
from agents.salience_bid import evaluate_salience

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
        # In the real arc the room reacts to the convener's PUBLISHED opener,
        # not to the raw config topic — so drive the participants off that
        # opener (the same curated convener turn asserted above), a closer
        # proxy than the bare topic string. This also pins that the opener the
        # convener actually emits still carries the topic the room engages on.
        convener = MockProvider(agent_id=rt["autonomous"]["convener"])
        opener_text = (await _reply(convener, _convene_directive(rt))).text

        # Every open-floor member other than the convener answers the opener.
        responders = [
            m["id"]
            for m in rt["members"]
            if m.get("respond") in _OPEN_FLOOR and m["id"] != rt["autonomous"]["convener"]
        ]
        assert len(responders) >= 2, "the opener needs an open-floor audience to discuss"

        for agent_id in responders:
            reply = await _reply(MockProvider(agent_id=agent_id), opener_text)
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


# ─── The floor, played through the real open-floor gate (ISSUE-0160) ─────────

_AGENTS_YAML = _REPO_ROOT / "config" / "agents.yaml"

# The `fast` alias the bid resolves, pointed at the mock the demo runs on.
_FAST_MOCK: dict[str, dict[str, Any]] = {
    "fast": {"provider": "mock", "model": "offline", "input_per_1m_tokens": 0.0,
             "output_per_1m_tokens": 0.0},
}

# A runaway guard for the floor loop, far above any scripted discussion: the
# real bound (`autonomous.max_rounds`) closes the arc long before this.
_FLOOR_CAP = 40


def _persona(agent_id: str) -> tuple[str, str]:
    data = yaml.safe_load(_AGENTS_YAML.read_text(encoding="utf-8"))
    for agent in data["agents"]:
        if agent["id"] == agent_id:
            return agent["name"], (agent.get("persona") or {}).get("title", "")
    raise AssertionError(f"{agent_id} is missing from config/agents.yaml")


def _window(posts: list[tuple[str, str]], agent_id: str) -> list[dict[str, Any]]:
    """The conversation window as the runtime replays it to ``agent_id``: its
    own posts as ``assistant`` turns, every peer's as a ``[peer]:`` user turn
    (``conversation_window.py`` §C)."""
    return [
        {"role": "assistant", "content": text} if sender == agent_id
        else {"role": "user", "content": f"[{sender}]: {text}"}
        for sender, text in posts
    ]


async def _floor(
    rt: dict[str, Any], posts: list[tuple[str, str]], answered: dict[int, int] | None = None,
) -> list[tuple[str, str]]:
    """Fan the last post out to the open floor until every bid is silent.

    Each member other than the sender bids on each new post through the real
    gate; a member that bids to speak composes against the same window, and
    its post fans out in turn. The window is the posts *before* the one being
    answered, as the runtime replays it — a reply published after that post is
    newer than it, so it is not there. Returns the posts the floor added, and
    records in ``answered`` which post each of them replied to."""
    floor = [m["id"] for m in rt["members"] if m.get("respond") in _OPEN_FLOOR]
    start = len(posts)
    pending = [len(posts) - 1]
    while pending and len(posts) < _FLOOR_CAP:
        index = pending.pop(0)
        sender, text = posts[index]
        for agent_id in floor:
            if agent_id == sender:
                continue
            name, title = _persona(agent_id)
            history = _window(posts[:index], agent_id)
            with use_alias_map(_FAST_MOCK):
                decision = await evaluate_salience(
                    llm_client=LLMClient(MockProvider(agent_id=agent_id)),
                    content=f"[{sender}]: {text}", transcript=history, agent_id=agent_id,
                    persona_name=name, persona_role=title, threshold=None, mode="bid",
                )
            assert decision.reason != "parse_failure", f"{agent_id}'s bid did not parse"
            if not decision.speak:
                continue
            turn = [*history, {"role": "user", "content": f"[{sender}]: {text}"}]
            response = await MockProvider(agent_id=agent_id).create_message(
                model="offline", messages=turn, system="", tools=[],
                max_tokens=512, temperature=0.2,
            )
            reply = (response.text or "").strip()
            posts.append((agent_id, reply))
            pending.append(len(posts) - 1)
            if answered is not None:
                answered[len(posts) - 1] = index
    return posts[start:]


def _advance_directive(rt: dict[str, Any], item: int) -> str:
    """The convener's agenda-advance stimulus — ``convener_cadence.go``
    ``composeAgendaAdvanceDirective`` rendered through the same convene framing."""
    auto = rt["autonomous"]
    directive = (
        f"Topic: {auto['topic']}\n\nNext agenda item:\n1. {auto['agenda'][item]}\n\n"
        f"Goal: {auto['goal']}"
    )
    return format_convener_opening(directive)


class TestOfflineRoundtableFloor:
    """The booted demo's floor: the room answers the convener, nobody repeats
    themselves, and the convener's and chair's forced turns read as their own."""

    async def _opened(self) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        rt = _roundtable()
        convener = rt["autonomous"]["convener"]
        opener = (await _reply(MockProvider(agent_id=convener), _convene_directive(rt))).text
        posts = [(convener, opener.strip())]
        await _floor(rt, posts)
        return rt, posts

    async def test_the_open_floor_answers_the_convener(self) -> None:
        rt, posts = await self._opened()
        convener = rt["autonomous"]["convener"]
        speakers = {sender for sender, _ in posts if sender != convener}
        assert len(speakers) >= 2, f"only {speakers or 'nobody'} answered the opener"

    async def test_nobody_repeats_themselves_and_the_floor_goes_quiet(self) -> None:
        _, posts = await self._opened()
        assert len(posts) < _FLOOR_CAP, "the scripted floor never went quiet"
        assert len(set(posts)) == len(posts), "a persona posted the same text twice"

    async def test_each_agenda_advance_poses_its_own_item_and_draws_a_reply(self) -> None:
        rt, posts = await self._opened()
        convener = rt["autonomous"]["convener"]
        seen = {text for _, text in posts}
        for item, keyword in ((1, "coupling"), (2, "migration")):
            directive = _advance_directive(rt, item)
            advance = (await _reply(MockProvider(agent_id=convener), directive)).text.strip()
            assert advance not in seen, f"agenda item {item} re-posted an earlier turn"
            assert keyword in advance.lower(), f"agenda item {item} does not pose {keyword!r}"
            posts.append((convener, advance))
            seen.add(advance)
            added = await _floor(rt, posts)
            assert added, f"nobody answered agenda item {item}"
            seen.update(text for _, text in added)
        assert len(set(posts)) == len(posts), "a persona posted the same text twice"

    async def test_a_turn_handed_over_without_a_bid_never_reposts(self) -> None:
        """A reply @-mentions the persona it answers, and under floor control
        the orchestrator re-fans a round's last reply — both hand a member a
        turn with no bid. On the booted demo that turn re-posted each persona's
        first paragraph; it must make a new point instead. A point counts as
        already made when the member posted it in answer to a *different* post
        — including one its window cannot show."""
        rt = _roundtable()
        convener = rt["autonomous"]["convener"]
        opener = (await _reply(MockProvider(agent_id=convener), _convene_directive(rt))).text
        posts = [(convener, (opener or "").strip())]
        answered: dict[int, int] = {}
        await _floor(rt, posts, answered)
        floor = [m["id"] for m in rt["members"] if m.get("respond") in _OPEN_FLOOR]
        for index, (sender, text) in enumerate(posts):
            for agent_id in floor:
                if agent_id == sender:
                    continue
                turn = [*_window(posts[:index], agent_id),
                        {"role": "user", "content": f"Message from {sender}: {text}"}]
                response = await MockProvider(agent_id=agent_id).create_message(
                    model="offline", messages=turn, system="", tools=[],
                    max_tokens=512, temperature=0.2,
                )
                reply = (response.text or "").strip()
                made_elsewhere = {
                    t for i, (s, t) in enumerate(posts)
                    if s == agent_id and answered.get(i) != index
                }
                assert reply not in made_elsewhere, (
                    f"{agent_id} re-posted its own earlier point on {sender}'s post #{index}"
                )

    async def test_a_second_convening_holds_the_discussion_again(self) -> None:
        """The console's Convene button re-runs the demo on the same channel,
        whose history still holds the first discussion. The chair's closing
        synthesis ends that discussion, so the room answers the opener again."""
        rt, posts = await self._opened()
        chair, convener = rt["escalation_chair_id"], rt["autonomous"]["convener"]
        synthesis = (await _reply(MockProvider(agent_id=chair), _synthesis_directive(rt))).text
        posts.append((chair, (synthesis or "").strip()))
        first = len(posts)
        convene = {"role": "user", "content": _convene_directive(rt)}
        response = await MockProvider(agent_id=convener).create_message(
            model="offline", system="", tools=[], max_tokens=512, temperature=0.2,
            messages=[*_window(posts, convener), convene],
        )
        assert (response.text or "").strip() == posts[0][1], "the second opener is not the opener"
        posts.append((convener, (response.text or "").strip()))
        await _floor(rt, posts)
        speakers = {sender for sender, _ in posts[first + 1:]}
        assert len(speakers) >= 2, f"only {speakers or 'nobody'} answered the second opener"

    async def test_the_chair_escalation_is_not_the_closing_synthesis(self) -> None:
        rt = _roundtable()
        chair = MockProvider(agent_id=rt["escalation_chair_id"])
        convener = rt["autonomous"]["convener"]
        opener = (await _reply(MockProvider(agent_id=convener), _convene_directive(rt))).text
        stalled = format_chair_escalation(f"Message from {convener}:\n\n{opener}")
        escalation = (await _reply(chair, stalled)).text
        synthesis = (await _reply(chair, _synthesis_directive(rt))).text
        assert escalation.strip() != synthesis.strip()
        assert not escalation.lower().startswith("synthesis"), (
            "the stall escalation posted the closing synthesis early"
        )
