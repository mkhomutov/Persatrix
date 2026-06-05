"""Config-contract test for the interim group-channel window sizing
(v0.3.7 conversation test-findings PR plan, F-2b / PR 3).

A group channel with three personas stores ~4 rows per user message
(the user turn + three persona replies), so the default ``max_turns: 20``
holds only ~5 rounds before FIFO eviction — the conversation opening had
already scrolled off when the live tester asked "how did we start?".

This is an **interim** per-agent bump for the three group-demo personas
(`ember-owl`, `iron-fox`, `nova-sparrow`) — raising their resolved
conversation window to 40 turns / 4096 tokens — **not** the RFC 0034
Phase 3 tuning (participant-count-aware sizing + cache LRU + calibration).
The window config is resolved per-agent (``resolve_conversation_window_config``
has no channel-type axis), so this raises the persona's window in every
channel it speaks on; the **global default stays 20/2048**, so DM-only
personas and the chat surface are unchanged.

These tests pin the bumped values against the *shipped* ``config/agents.yaml``
(not a synthetic fixture) so a future edit that drifts them — or a new
group persona added without the block — is caught here. ``test_conversation_window``
separately pins the global default; this module pins the per-agent
override and the global-default-preserved intent together.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from agents.persona_runtime.conversation_window import (
    ConversationWindowConfig,
    resolve_conversation_window_config,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_AGENTS_YAML = _REPO_ROOT / "config" / "agents.yaml"
_OPTIMIZATION_YAML = _REPO_ROOT / "config" / "optimization.yaml"

# The three group-demo personas (config/channels.yaml `group:planning`).
_GROUP_PERSONAS = ("ember-owl", "iron-fox", "nova-sparrow")
_EXPECTED_TURNS = 40
_EXPECTED_TOKENS = 4096


def _shipped_agents_by_id() -> dict[str, dict]:
    doc = yaml.safe_load(_AGENTS_YAML.read_text(encoding="utf-8"))
    return {a["id"]: a for a in doc.get("agents", [])}


class TestGroupPersonaWindowBump:
    """The three group-demo personas resolve to the interim 40/4096
    window; a persona without an override still inherits the global
    default; and the global default block itself is untouched.
    """

    def test_group_personas_resolve_to_bumped_window(self) -> None:
        agents = _shipped_agents_by_id()
        for pid in _GROUP_PERSONAS:
            assert pid in agents, f"{pid} missing from shipped config/agents.yaml"
            cfg = resolve_conversation_window_config(agents[pid])
            assert cfg.enabled is True, pid
            assert cfg.max_turns == _EXPECTED_TURNS, pid
            assert cfg.max_tokens == _EXPECTED_TOKENS, pid

    def test_global_default_unchanged_so_dm_surface_keeps_small_window(
        self,
    ) -> None:
        """The bump is per-agent — the shipped global default (which
        DM-only personas and the chat surface inherit) stays 20/2048.
        """
        defaults = ConversationWindowConfig()
        assert (defaults.max_turns, defaults.max_tokens) == (20, 2048)

        block = yaml.safe_load(
            _OPTIMIZATION_YAML.read_text(encoding="utf-8"),
        )["conversation_window"]
        assert block["max_turns"] == 20
        assert block["max_tokens"] == 2048

    def test_persona_without_override_inherits_global_default(self) -> None:
        """A non-group persona that ships no ``conversation_window`` block
        still resolves to the small default — proving the bump did not
        leak into the global default.
        """
        agents = _shipped_agents_by_id()
        no_block = [
            a
            for aid, a in agents.items()
            if aid not in _GROUP_PERSONAS and "conversation_window" not in a
        ]
        assert no_block, "expected at least one persona without an override block"
        defaults = ConversationWindowConfig()
        for agent in no_block:
            cfg = resolve_conversation_window_config(agent)
            assert cfg.max_turns == defaults.max_turns, agent.get("id")
            assert cfg.max_tokens == defaults.max_tokens, agent.get("id")
