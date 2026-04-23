"""Provider-agnostic LLM data types and Protocol.

Leaf module — imports nothing from :mod:`agents.llm_client` or
:mod:`agents.llm_providers`.  Both of those modules import from here, so
keeping this file free of project-internal imports breaks the historical
``llm_client`` ↔ ``llm_providers`` cycle that previously required a
deferred re-export with ``# noqa: E402`` in :mod:`agents.llm_client`
(see PR #167 round-2 review *Should Fix*).

The public re-export surface remains unchanged: callers continue to
import these symbols from :mod:`agents.llm_client`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class StopReason(Enum):
    """Provider-agnostic stop reason.

    Unmapped provider-specific stop reasons (e.g. Anthropic's stop_sequence,
    OpenAI's content_filter) are mapped to END_TURN with a warning log.
    """

    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"


@dataclass
class ToolCall:
    """Provider-agnostic tool call."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Usage:
    """Token usage from LLM response."""

    input_tokens: int
    output_tokens: int


@dataclass
class LLMToolResult:
    """Provider-agnostic tool result for LLM message building.

    Not to be confused with tools.registry.ToolResult which represents
    the raw result from a tool function (success/data/error/error_type).
    """

    tool_call_id: str
    content: str
    is_error: bool


@dataclass
class LLMResponse:
    """Normalized response from any LLM provider."""

    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: StopReason = StopReason.END_TURN
    usage: Usage = field(default_factory=lambda: Usage(0, 0))


class LLMProvider(Protocol):
    """Protocol for LLM provider implementations."""

    # Stable identifier emitted as the OTEL ``gen_ai.system`` attribute
    # (``"anthropic"``, ``"openai"``, …).  Declared here so call sites do
    # not have to derive it from ``type().__name__`` (which silently
    # produces wrong values for test doubles like ``AsyncMock``).
    name: str

    async def create_message(
        self,
        *,
        model: str,
        messages: list,
        system: str,
        tools: list,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse: ...

    def format_tool_definitions(self, tools: list[dict]) -> list[dict]: ...

    def append_tool_round(
        self,
        messages: list,
        response: LLMResponse,
        tool_results: list[LLMToolResult],
    ) -> list: ...
