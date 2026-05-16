"""RFC 0034 Phase 1 PR 3 — conversation-window config resolution.

Pins :func:`resolve_conversation_window_config`, the helper
``_ConversationWindowMixin._build_seed_messages`` calls on a persona's
first turn — resolved once from the persona's config and cached
(``conversation_seed.py``) — to turn the optional per-agent
``conversation_window`` block (``config/agents.yaml``) into a
:class:`ConversationWindowConfig`. An absent block — or any absent key —
inherits the dataclass default, which mirrors the
``config/optimization.yaml`` defaults block (the two are pinned equal by
``test_conversation_window.py::test_defaults_match_optimization_yaml``).

A malformed block must never crash agent construction: production
configs are gated through ``make validate`` against
``schemas/agent.schema.json``, but test fixtures and dict-built configs
bypass that, so the resolver degrades a bad value to its per-key
default rather than raising.
"""

from __future__ import annotations

from agents.persona_runtime.conversation_window import (
    ConversationWindowConfig,
    resolve_conversation_window_config,
)


class TestResolveConversationWindowConfig:
    def test_absent_block_returns_defaults(self):
        """A config with no ``conversation_window`` key resolves to the
        committed Phase 1 defaults."""
        assert resolve_conversation_window_config({}) == ConversationWindowConfig()

    def test_full_block_overrides_every_field(self):
        """An explicit per-agent block wins over every default."""
        cfg = resolve_conversation_window_config({
            "conversation_window": {
                "enabled": False,
                "max_turns": 8,
                "max_tokens": 512,
            },
        })
        assert cfg == ConversationWindowConfig(
            max_turns=8, max_tokens=512, enabled=False,
        )

    def test_partial_block_inherits_absent_keys(self):
        """Keys absent from the block fall back to the dataclass default;
        present keys override."""
        cfg = resolve_conversation_window_config({
            "conversation_window": {"max_turns": 5},
        })
        defaults = ConversationWindowConfig()
        assert cfg.max_turns == 5
        assert cfg.max_tokens == defaults.max_tokens
        assert cfg.enabled == defaults.enabled

    def test_non_dict_block_falls_back_to_defaults(self):
        """A non-mapping ``conversation_window`` value (a stray scalar in
        a hand-written config) degrades to defaults without raising."""
        assert resolve_conversation_window_config(
            {"conversation_window": "enabled"},
        ) == ConversationWindowConfig()
        assert resolve_conversation_window_config(
            {"conversation_window": None},
        ) == ConversationWindowConfig()

    def test_malformed_value_falls_back_per_key(self):
        """A wrong-typed value for one key degrades that key only — the
        rest of the block still resolves."""
        cfg = resolve_conversation_window_config({
            "conversation_window": {
                "enabled": "yes",       # not a bool
                "max_turns": "lots",    # not an int
                "max_tokens": 700,      # valid — survives
            },
        })
        defaults = ConversationWindowConfig()
        assert cfg.enabled == defaults.enabled
        assert cfg.max_turns == defaults.max_turns
        assert cfg.max_tokens == 700

    def test_bool_is_not_accepted_as_an_int_count(self):
        """``True``/``False`` must not slip through as ``max_turns`` /
        ``max_tokens`` — ``bool`` is an ``int`` subclass in Python, so the
        resolver rejects it explicitly."""
        cfg = resolve_conversation_window_config({
            "conversation_window": {"max_turns": True, "max_tokens": False},
        })
        defaults = ConversationWindowConfig()
        assert cfg.max_turns == defaults.max_turns
        assert cfg.max_tokens == defaults.max_tokens
