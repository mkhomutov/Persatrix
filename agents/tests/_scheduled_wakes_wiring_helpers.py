"""Shared fixtures + helpers for ``test_scheduled_wakes_cache_wiring*.py``
— RFC 0024 PR 2.1.

Test files are split to stay under the 500-line review-friendly cap; the
helpers live in this underscore-prefixed module (not a test file) so the
two test files import from one canonical source instead of duplicating
the fixture + builder pair.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from agents.llm_client import LLMClient, LLMResponse


def make_client() -> LLMClient:
    """Mock :class:`LLMClient` that returns an empty ``"ok"`` response."""
    mock_provider = AsyncMock()
    mock_provider.create_message = AsyncMock(return_value=LLMResponse(text="ok"))
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return LLMClient(mock_provider)


def persona_config(*, db_path: str, timers: list[dict] | None) -> dict[str, Any]:
    """Minimal persona-agent config with optional ``autonomy.timers``."""
    autonomy: dict[str, Any] = {"level": "autonomous"}
    if timers is not None:
        autonomy["timers"] = timers
    return {
        "id": "ember-owl",
        "type": "persona",
        "name": "Ember Owl",
        "role": "Engineering leadership",
        "model": "test-model",
        "temperature": 0.7,
        "max_llm_calls": 10,
        "max_tokens": 4096,
        "persona": {
            "title": "VP of Engineering",
            "background": "15 years.",
            "behavior": {},
        },
        "permissions": {"memory": {"read": True, "write": True}},
        "memory": {"db_path": db_path, "notes": {"max_notes": 100}},
        "autonomy": autonomy,
    }
