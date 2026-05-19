"""Shared fixtures and helpers for persona runtime test modules."""

from unittest.mock import AsyncMock, MagicMock

from agents.base import TaskInput
from agents.llm_client import LLMClient, LLMResponse

_PERSONA_CONFIG: dict = {
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
        "background": "15 years in software engineering.",
        "behavior": {
            "directness": "direct",
            "detail_focus": "big-picture",
            "formality": "professional",
            "risk_tolerance": "moderate",
            "expressiveness": "reserved",
        },
        "quirks": ["Starts every Monday with 'What's on fire?'"],
        "goals": {
            "primary": "Ship v2.0 on time",
            "secondary": ["Reduce tech debt by 20%"],
            "hidden": "Prove the team can self-organize",
        },
    },
    "permissions": {
        "memory": {"read": True, "write": True},
    },
    "memory": {
        "db_path": ":memory:",
        "notes": {
            "max_notes": 100,
            "auto_reflect_after": 5,
        },
    },
}


def _make_client(
    responses: list[LLMResponse] | None = None,
) -> LLMClient:
    """Create a mock LLMClient that returns the given responses."""
    mock_provider = AsyncMock()
    if responses:
        mock_provider.create_message = AsyncMock(side_effect=responses)
    else:
        mock_provider.create_message = AsyncMock(
            return_value=LLMResponse(text="I'll handle this task.")
        )
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: [
            *msgs,
            {"role": "assistant", "content": "tool round"},
            {"role": "user", "content": "tool results"},
        ]
    )
    return LLMClient(mock_provider)


def _task(payload: str = "do something") -> TaskInput:
    return TaskInput(task_id="t1", workflow_id="w1", payload=payload)
