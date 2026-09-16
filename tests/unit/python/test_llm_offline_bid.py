"""The offline mock answers the open-floor bid in the grammar the gate parses (ISSUE-0160).

``make demo-autonomous`` convened the ``roundtable`` on the mock provider and only
the convener ever spoke. Every open-floor bid came back as the persona's canned
discussion paragraph, the gate found no ``should_post:`` (or ``speak:``) line in
it, and each bid resolved to silence as ``parse_failure``. The mock then had
nothing to stop it repeating itself either: the same keyword picked the same
paragraph every turn.

These tests drive the **real** gate — :func:`agents.salience_bid.evaluate_salience`,
which builds the bid prompt and parses the answer — with the mock as its
provider, in all three ``reasoning.mode`` values, so the mock's answer and the
gate's grammar cannot drift apart unnoticed. The contract they pin:

* a persona with a scripted point it has **not yet made** bids to speak;
* one with nothing scripted for the message, or only points it already made,
  stays silent — with a real reason code, never ``parse_failure``;
* when it speaks, it makes that next point instead of repeating itself — and a
  turn it is handed without a bid (a reply that @-mentions it, or the floor
  re-fanning a round's last reply) with no new point left gets its catch-all
  line once before any paragraph is repeated.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agents.llm_client import LLMClient
from agents.llm_offline import MockProvider, reset_cache
from agents.model_aliases import use_alias_map
from agents.salience_bid import SalienceDecision, evaluate_salience

_FIRST_POINT = "A monorepo buys atomic cross-team changes."
_SECOND_POINT = "The coupling risk is shared libraries changing under you."
_CATCH_ALL = "Short version, please."
_CLOSING = "Synthesis: stage the monorepo."

_FIXTURE = f"""\
responses:
  ember-owl:
    - match: ["monorepo"]
      reply: "{_FIRST_POINT}"
    - match: ["coupling"]
      reply: "{_SECOND_POINT}"
    - match: []
      reply: "{_CATCH_ALL}"
  iron-fox:
    - match: ["synthes"]
      closes: true
      reply: "{_CLOSING}"
"""

_FAST_ALIAS_MAP: dict[str, dict[str, Any]] = {
    "fast": {
        "provider": "mock",
        "model": "mock-fast",
        "input_per_1m_tokens": 0.0,
        "output_per_1m_tokens": 0.0,
    },
}

_MODES = ("off", "bid", "plan")


@pytest.fixture(autouse=True)
def _offline_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    fixture = tmp_path / "offline_responses.yaml"
    fixture.write_text(_FIXTURE, encoding="utf-8")
    monkeypatch.setenv("PERSATRIX_OFFLINE_RESPONSES", str(fixture))
    reset_cache()
    yield
    reset_cache()


async def _bid(
    content: str,
    *,
    mode: str,
    transcript: list[dict[str, Any]] | None = None,
) -> SalienceDecision:
    with use_alias_map(_FAST_ALIAS_MAP):
        return await evaluate_salience(
            llm_client=LLMClient(MockProvider(agent_id="ember-owl")),
            content=content,
            transcript=transcript or [],
            agent_id="ember-owl",
            persona_name="Ember Owl",
            persona_role="VP of Engineering",
            threshold=None,
            mode=mode,
        )


async def _compose(messages: list[dict[str, Any]]) -> str:
    response = await MockProvider(agent_id="ember-owl").create_message(
        model="offline", messages=messages, system="", tools=[],
        max_tokens=512, temperature=0.2,
    )
    return response.text or ""


class TestTheBidParses:
    @pytest.mark.parametrize("mode", _MODES)
    async def test_an_unmade_scripted_point_bids_to_speak(self, mode: str) -> None:
        decision = await _bid("[iron-fox]: Should we adopt a monorepo?", mode=mode)
        assert decision.speak is True, decision

    @pytest.mark.parametrize("mode", _MODES)
    async def test_nothing_scripted_stays_silent_without_a_parse_failure(self, mode: str) -> None:
        """The catch-all answers a direct message; it is not a reason to join a room."""
        decision = await _bid("[iron-fox]: Anyone up for lunch?", mode=mode)
        assert decision.speak is False
        assert decision.reason != "parse_failure", decision

    async def test_nothing_scripted_is_nothing_to_add(self) -> None:
        decision = await _bid("[iron-fox]: Anyone up for lunch?", mode="bid")
        assert decision.reason == "nothing_to_add"


class TestNoRepeats:
    async def test_a_point_already_made_this_round_is_already_answered(self) -> None:
        decision = await _bid(
            "[iron-fox]: Back to the monorepo.",
            mode="bid",
            transcript=[{"role": "assistant", "content": _FIRST_POINT}],
        )
        assert decision.speak is False
        assert decision.reason == "already_answered"

    async def test_the_next_unmade_point_still_bids(self) -> None:
        decision = await _bid(
            "[iron-fox]: A monorepo adds coupling, surely?",
            mode="bid",
            transcript=[{"role": "assistant", "content": _FIRST_POINT}],
        )
        assert decision.speak is True

    async def test_the_compose_turn_makes_the_next_point(self) -> None:
        reply = await _compose([
            {"role": "user", "content": "[nova-sparrow]: Should we adopt a monorepo?"},
            {"role": "assistant", "content": _FIRST_POINT},
            {"role": "user", "content": "[iron-fox]: A monorepo adds coupling, surely?"},
        ])
        assert reply == _SECOND_POINT

    async def test_a_handed_over_turn_with_no_new_point_gets_the_catch_all(self) -> None:
        """Handed a turn with every matching point made, the persona answers
        with its generic line rather than re-posting the paragraph."""
        reply = await _compose([
            {"role": "user", "content": "Message from nova-sparrow: What about a monorepo?"},
            {"role": "assistant", "content": _FIRST_POINT},
            {"role": "user", "content": "Message from iron-fox: Back to the monorepo."},
        ])
        assert reply == _CATCH_ALL

    async def test_only_with_the_catch_all_spent_does_a_point_repeat(self) -> None:
        reply = await _compose([
            {"role": "user", "content": "Message from nova-sparrow: What about a monorepo?"},
            {"role": "assistant", "content": _FIRST_POINT},
            {"role": "user", "content": "Message from iron-fox: Back to the monorepo."},
            {"role": "assistant", "content": _CATCH_ALL},
            {"role": "user", "content": "Message from nova-sparrow: One more time — the monorepo?"},
        ])
        assert reply == _FIRST_POINT

    async def test_a_closing_line_starts_the_next_discussion_fresh(self) -> None:
        """The channel window outlives a discussion, so without a boundary a
        re-convened room would find every point already made. A reply marked
        ``closes: true`` — any persona's — is that boundary."""
        transcript = [
            {"role": "assistant", "content": _FIRST_POINT},
            {"role": "user", "content": f"[iron-fox]: {_CLOSING}"},
        ]
        decision = await _bid("[nova-sparrow]: Should we adopt a monorepo?", mode="bid",
                              transcript=transcript)
        assert decision.speak is True
        reply = await _compose([
            *transcript, {"role": "user", "content": "[nova-sparrow]: Should we adopt a monorepo?"},
        ])
        assert reply == _FIRST_POINT

    async def test_the_catch_all_still_answers_every_off_script_message(self) -> None:
        reply = await _compose([
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": _CATCH_ALL},
            {"role": "user", "content": "anyone there?"},
        ])
        assert reply == _CATCH_ALL
