"""RFC 0030 Tier B PR 4 (v0.3.8) — the `chair` facilitator + the inert Layer-5 seam.

PR 4 of the Tier B PR plan
(``docs/rfcs/0030-amendment-relevance-gated-response-tierb-pr-plan.md``).
These are the **red** half of the TDD pair for the `chair` closeout: they pin
two contracts before the :mod:`agents.chair_moderation` module exists.

The `chair` ships in v0.3.8 as a low-threshold **facilitator only**:

* **Facilitator (active).** On the wire a `chair` is just a `participant`
  whose salience ``threshold`` is the low ``DefaultChairThreshold`` (Go,
  ``internal/channels/config.go``). A low bar means the chair clears the cheap
  relevance bid (:func:`agents.salience_bid.evaluate_salience`) readily and so
  keeps a discussion moving, where a *default* `participant` (unset threshold →
  bias-to-silence, TB2) would stay quiet on the same middling score. The first
  test pins exactly that contrast, anchored to the real Go default so the test
  cannot drift away from the shipped chair bar.

* **Moderator (inert, TB5).** The chair's *active half* — reading the transcript
  and deciding to continue / wrap up / terminate the conversation — is **Layer 5,
  deferred to v0.4.0**. v0.3.8 wires the seam but leaves it inert: a v0.3.8 chair
  **cannot unilaterally close a conversation** (convergence comes only from the
  deterministic governance Layers 1/2/4). The second group of tests pins that the
  seam exists, only ever returns CONTINUE, and is **not invoked** by any runtime
  path — the same reserved-field discipline v0.3.7 used for ``threshold``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agents.chair_moderation import (
    ModeratorAction,
    ModeratorDecision,
    evaluate_chair_moderation,
)
from agents.llm_client import LLMClient, LLMResponse
from agents.model_aliases import use_alias_map
from agents.salience_bid import SalienceDecision, evaluate_salience

# A `fast` alias that resolves to the mock provider for the duration of a test
# (the bid's internal ``resolve("fast")`` must not hit the shipped
# ``unconfigured`` provider, which fails loud by design). Mirrors
# ``test_salience_bid.py``.
_FAST_ALIAS_MAP: dict[str, dict[str, Any]] = {
    "fast": {
        "provider": "mock",
        "model": "mock-fast",
        "input_per_1m_tokens": 0.0,
        "output_per_1m_tokens": 0.0,
    },
}

_CONFIG_GO = Path("internal/channels/config.go")
# `const DefaultChairThreshold = <float>` on a single line — the same
# single-line const shape the channel-size cap drift pin parses
# (``test_cross_language_salience_max_channel_members_drift.py``). A future
# move into a `const ( ... )` block would force a deliberate update here.
_GO_CHAIR_THRESHOLD = re.compile(
    r"^\s*const\s+DefaultChairThreshold\s*=\s*([0-9]*\.?[0-9]+)\s*(?://.*)?$",
    re.MULTILINE,
)


def _go_chair_threshold() -> float:
    """Parse ``DefaultChairThreshold`` out of the Go source.

    The chair's whole facilitator identity is this low Go-side default
    delivered over the ``ChannelMessageEvent.threshold`` wire field (the
    Python bid has no chair concept — it just receives a threshold). Anchoring
    the test to the live Go literal keeps "a chair clears readily" bound to the
    *actual* shipped bar rather than a copied magic number.
    """
    src = _CONFIG_GO.read_text(encoding="utf-8")
    match = _GO_CHAIR_THRESHOLD.search(src)
    if match is None:
        pytest.fail(
            f"could not find `const DefaultChairThreshold = <float>` in "
            f"{_CONFIG_GO}. If the constant was moved into a `const ( ... )` "
            f"block or renamed, update the parse rule in this test to match — "
            f"the chair bar this test asserts is the Go default."
        )
    return float(match.group(1))


def _client(text: str) -> LLMClient:
    provider = AsyncMock()
    provider.create_message = AsyncMock(return_value=LLMResponse(text=text))
    return LLMClient(provider)


_TRANSCRIPT: list[dict[str, Any]] = [
    {"role": "user", "content": "[iron-fox]: We should pick a database for the cache."},
    {"role": "assistant", "content": "Redis is the obvious fit for a cache layer."},
]


async def _bid(*, threshold: float | None, score_text: str) -> SalienceDecision:
    with use_alias_map(_FAST_ALIAS_MAP):
        return await evaluate_salience(
            llm_client=_client(score_text),
            content="What database should we use for the cache?",
            transcript=_TRANSCRIPT,
            agent_id="ember-owl",
            persona_name="Ember Owl",
            persona_role="VP of Engineering",
            threshold=threshold,
        )


class TestChairClearsBidReadily:
    """The facilitator half: a chair's low threshold speaks where a default
    `participant` (unset threshold) stays silent on the same middling score."""

    async def test_chair_low_threshold_clears_a_middling_score(self):
        """A middling salience score clears the chair's low Go default bar."""
        chair_threshold = _go_chair_threshold()
        # A score comfortably above the chair bar but well below the decisive
        # bar an unset threshold demands (``_DECISIVE_SCORE`` = 0.8).
        decision = await _bid(
            threshold=chair_threshold, score_text="speak: yes\nscore: 0.40",
        )
        assert decision.speak is True
        assert decision.reason == "salient"

    async def test_default_participant_stays_silent_on_the_same_score(self):
        """The *same* middling score, with an unset threshold (the default
        `participant`, bias-to-silence TB2), stays silent — the contrast that
        makes the chair a facilitator rather than just another participant."""
        decision = await _bid(threshold=None, score_text="speak: yes\nscore: 0.40")
        assert decision.speak is False
        assert decision.reason == "below_threshold"

    def test_chair_bar_is_strictly_below_the_decisive_bar(self):
        """The chair default sits in ``(0, _DECISIVE_SCORE)`` — low enough to
        clear readily, but above zero so a chair still respects a genuinely
        low-salience score rather than literally piling onto every message."""
        from agents.salience_bid import _DECISIVE_SCORE

        chair_threshold = _go_chair_threshold()
        assert 0.0 < chair_threshold < _DECISIVE_SCORE


class TestLayer5SeamIsInert:
    """The moderator half (TB5): the seam exists, only ever returns CONTINUE,
    and is not wired into any runtime path in v0.3.8."""

    def test_vocabulary_reserves_the_close_actions(self):
        """The v0.4.0 moderator's full decision vocabulary is *defined* — but
        the close actions are reserved, not reachable from the inert seam."""
        assert {a.name for a in ModeratorAction} == {
            "CONTINUE", "WRAP_UP", "TERMINATE",
        }

    def test_inert_seam_always_continues(self):
        """Across an empty transcript, a populated one, and an explicit
        interaction id, the inert seam returns CONTINUE — never a close."""
        for transcript in ([], _TRANSCRIPT):
            for interaction_id in (None, "int-abc123"):
                decision = evaluate_chair_moderation(
                    transcript=transcript, interaction_id=interaction_id,
                )
                assert isinstance(decision, ModeratorDecision)
                assert decision.action is ModeratorAction.CONTINUE
                assert decision.action not in (
                    ModeratorAction.WRAP_UP, ModeratorAction.TERMINATE,
                )

    def test_seam_takes_no_arguments_required(self):
        """The seam is callable with no arguments (every parameter is a
        keyword default) — a v0.4.0 caller adds the transcript/id it needs
        without changing the inert contract."""
        assert evaluate_chair_moderation().action is ModeratorAction.CONTINUE

    def test_seam_is_not_invoked_by_any_runtime_path(self):
        """Structural inertness: *nothing* under ``agents/`` imports or calls the
        seam in v0.3.8 — the seam module itself is the only file allowed to name
        it. Scanning the whole package (rather than a hand-picked file list)
        means a future PR that wires Layer 5 from *any* runtime module — not just
        the few the bid happens to touch today — lands as a *deliberate* failure
        here. If Layer 5 is intentionally being wired (v0.4.0), update this test."""
        seam_module = Path("agents/chair_moderation.py")

        def _is_runtime(p: Path) -> bool:
            # The seam module names itself; test files are not a runtime path.
            return (
                p != seam_module
                and "tests" not in p.parts
                and not p.name.startswith("test_")
                and not p.name.endswith("_test.py")
            )

        runtime_files = [p for p in Path("agents").rglob("*.py") if _is_runtime(p)]
        # Guard against a vacuous pass: this assertion is a *tripwire* for a
        # future PR wiring Layer 5, so it is only meaningful if it actually
        # scanned the package. The paths here are repo-root-relative (as in the
        # ``DefaultChairThreshold`` drift parse above); if pytest's cwd is not
        # the repo root the glob comes back empty and the offender check below
        # would pass for the wrong reason. Fail loudly on an empty scan instead
        # — the seam module excludes *itself*, so a healthy tree always leaves
        # other runtime modules here.
        assert runtime_files, (
            "scanned no runtime modules under agents/ — the inert-seam check "
            "cannot run. Is pytest's working directory the repo root?"
        )

        offenders = sorted(
            str(p)
            for p in runtime_files
            if "evaluate_chair_moderation" in (src := p.read_text(encoding="utf-8"))
            or "chair_moderation" in src
        )
        assert not offenders, (
            "the Layer-5 chair-moderation seam is meant to be inert in v0.3.8 "
            f"but is referenced by a runtime path: {offenders}. If Layer 5 is "
            "intentionally being wired (v0.4.0), update this test."
        )
