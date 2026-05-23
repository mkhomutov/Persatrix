"""Offline / mock LLM provider for zero-cost demos and CI smoke runs.

A third :class:`~agents.llm_types.LLMProvider` implementation alongside
``AnthropicProvider`` / ``OpenAIProvider`` (see :mod:`agents.llm_providers`).
It returns scripted or persona-flavoured text **without any network call,
API key, or token spend**, so the whole agent society — chat, channels,
memory, the wallet-lease path, OpenTelemetry traces — runs end-to-end for
$0 and zero risk.

Activation (either is sufficient; the env var wins):

* ``PERSATRIX_OFFLINE=1`` — global override, forces *every* agent onto the
  mock regardless of its configured ``model`` / ``provider``.
* ``provider: mock`` in an agent's ``config/agents.yaml`` entry.

Both are wired in :func:`agents.llm_client.create_provider`.

**Reply selection.** Each turn the provider:

1. extracts the latest user message from the ``messages`` array,
2. looks up a curated reply in ``config/offline_responses.yaml`` keyed by
   ``agent_id`` (path overridable via ``PERSATRIX_OFFLINE_RESPONSES``), and
3. falls back to a deterministic, persona-flavoured placeholder when no
   fixture matches.

The provider only ever returns plain text with ``stop_reason == END_TURN``
— exactly like a model that has not been prompt-trained on the persona
action schema. The runtime's :func:`agents.persona_runtime.channel_reply.
synthesize_channel_reply` then promotes that text into a channel publish,
so both the chat-as-DM and channel paths work without the mock needing to
know channel IDs or emit the JSON action format.

**Cost / observability.** No provider SDK is imported and no request is
issued, so real spend is zero. The provider reports *synthetic* token
usage (derived from text length) so the OTel ``gen_ai.usage.*`` spans,
the token metrics, and the RFC 0023 wallet-lease settle path stay
populated — the budget machinery is exercised at $0.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .llm_types import LLMResponse, LLMToolResult, StopReason, Usage

logger = logging.getLogger(__name__)

__all__ = [
    "MockProvider",
    "offline_mode_enabled",
    "reset_cache",
]

_OFFLINE_ENV = "PERSATRIX_OFFLINE"
_RESPONSES_ENV = "PERSATRIX_OFFLINE_RESPONSES"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Repo-relative default location for curated offline replies.  Mirrors the
# ``config/`` anchoring used by :mod:`agents.optimization`.
_DEFAULT_RESPONSES_PATH: Path = (
    Path(__file__).resolve().parent.parent / "config" / "offline_responses.yaml"
)

# Strip the ``<|user_message …|>`` delimiters the runtime wraps around
# inbound user text (prompt-injection mitigation) before keyword-matching
# or echoing it back — same delimiter shape as
# :func:`agents.chat_reply.extract_chat_reply`.
_USER_MSG_DELIM_RE = re.compile(r"<\|/?user_message[^|]*\|>")


def offline_mode_enabled() -> bool:
    """Return whether ``PERSATRIX_OFFLINE`` selects the offline provider."""
    return os.environ.get(_OFFLINE_ENV, "").strip().lower() in _TRUTHY


@lru_cache(maxsize=1)
def _load_responses() -> dict[str, list[dict[str, Any]]]:
    """Read curated offline replies once per process.

    On any failure (missing file, parse error, unexpected shape) returns
    an empty mapping so the provider falls through to generated
    placeholders. The cache is process-wide; tests that swap the fixture
    file should call :func:`reset_cache` first.
    """
    path = Path(os.environ.get(_RESPONSES_ENV, _DEFAULT_RESPONSES_PATH))
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.debug("offline responses not found at %s; using generated replies", path)
        return {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "failed to load offline responses at %s: %s; using generated replies",
            path, exc,
        )
        return {}
    if not isinstance(data, dict):
        return {}
    responses = data.get("responses")
    if not isinstance(responses, dict):
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for agent_id, entries in responses.items():
        if isinstance(entries, list):
            out[str(agent_id)] = [e for e in entries if isinstance(e, dict)]
    return out


def reset_cache() -> None:
    """Clear the cached offline-responses file (used by tests)."""
    _load_responses.cache_clear()


def _content_to_text(content: Any) -> str:
    """Flatten an Anthropic-style message ``content`` to plain text.

    Handles both the string form (the common seed shape) and the
    list-of-blocks form, concatenating any ``text`` blocks.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "\n".join(parts)
    return ""


def _latest_user_text(messages: list[Any]) -> str:
    """Return the text of the most recent ``role == "user"`` message."""
    for msg in reversed(messages or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return _content_to_text(msg.get("content"))
    return ""


def _clean(text: str) -> str:
    """Strip user-message delimiters and surrounding whitespace."""
    return _USER_MSG_DELIM_RE.sub("", text).strip()


def _snippet(text: str, limit: int = 160) -> str:
    cleaned = _clean(text)
    if len(cleaned) > limit:
        return cleaned[:limit].rstrip() + "…"
    return cleaned


class MockProvider:
    """Deterministic offline LLM provider — scripted + persona fallback.

    Satisfies the :class:`agents.llm_types.LLMProvider` Protocol. Never
    contacts a provider SDK; see the module docstring for the activation
    and reply-selection contract.
    """

    name = "mock"

    def __init__(
        self,
        *,
        agent_id: str = "",
        display_name: str = "",
        persona: dict[str, Any] | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._display_name = display_name or agent_id or "agent"
        self._persona = persona or {}

    @classmethod
    def from_config(cls, agent_config: dict[str, Any]) -> MockProvider:
        """Build a provider from an ``agents.yaml`` entry.

        Captures the agent identity and persona block so curated fixtures
        can be keyed by ``id`` and the generated fallback can stay in
        character.
        """
        persona = agent_config.get("persona")
        return cls(
            agent_id=str(agent_config.get("id", "")),
            display_name=str(agent_config.get("name", "")),
            persona=persona if isinstance(persona, dict) else {},
        )

    async def create_message(
        self,
        *,
        model: str,
        messages: list,
        system: str,
        tools: list,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        user_text = _latest_user_text(messages)
        reply = self._scripted_reply(user_text)
        if reply is None:
            reply = self._fallback_reply(user_text)

        # Synthetic usage so OTel token spans/metrics and the wallet-lease
        # settle path stay populated — no real spend occurs.
        input_chars = len(system or "") + sum(
            len(_content_to_text(m.get("content"))) if isinstance(m, dict) else 0
            for m in messages or []
        )
        usage = Usage(
            input_tokens=max(1, input_chars // 4),
            output_tokens=max(1, len(reply) // 4),
        )
        return LLMResponse(
            text=reply,
            tool_calls=[],
            stop_reason=StopReason.END_TURN,
            usage=usage,
        )

    def _scripted_reply(self, user_text: str) -> str | None:
        """Return the first curated reply whose keywords all match, else None.

        An entry matches when every keyword in its ``match`` list is a
        case-insensitive substring of the user message. An empty ``match``
        list is a catch-all (use it last as a per-agent default). Entries
        are tried in file order, so list specific scenarios first.
        """
        haystack = _clean(user_text).lower()
        for entry in _load_responses().get(self._agent_id, []):
            reply = entry.get("reply")
            keywords = entry.get("match", [])
            if not isinstance(reply, str) or not isinstance(keywords, list):
                continue
            if all(isinstance(k, str) and k.lower() in haystack for k in keywords):
                return reply.strip()
        return None

    def _fallback_reply(self, user_text: str) -> str:
        """Deterministic, honest, lightly in-character placeholder.

        Fires only off-script (no fixture match). The curated fixtures
        cover the demo scenarios, so this is a safety net rather than the
        showcase — it stays truthful that no live model is running rather
        than fabricating a confident answer.
        """
        title = str(self._persona.get("title", "")).strip()
        who = self._display_name + (f" ({title})" if title else "")
        asked = _snippet(user_text)
        tail = f' (You asked: "{asked}")' if asked else ""
        return (
            f"{who}: I'm running in offline demo mode, so there's no live model "
            f"behind me right now — this is a deterministic placeholder, not a "
            f"real answer.{tail}"
        )

    def format_tool_definitions(self, tools: list[dict]) -> list[dict]:
        """Accept tool definitions and pass them through unchanged.

        Offline mode never issues tool calls, so the value is inert; it is
        returned as-is rather than dropped so the action loop's
        ``tools`` kwarg keeps a stable shape.
        """
        return list(tools)

    def append_tool_round(
        self,
        messages: list,
        response: LLMResponse,
        tool_results: list[LLMToolResult],
    ) -> list:
        """Mirror the Anthropic tool-round shape for Protocol completeness.

        Never invoked in practice — :meth:`create_message` only returns
        ``END_TURN`` — but implemented correctly so the contract holds if a
        future caller drives a tool loop against the mock.
        """
        assistant_content: list[dict[str, Any]] = []
        if response.text:
            assistant_content.append({"type": "text", "text": response.text})
        for tc in response.tool_calls:
            assistant_content.append(
                {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}
            )
        result_blocks: list[dict[str, Any]] = []
        for tr in tool_results:
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": tr.tool_call_id,
                "content": tr.content,
            }
            if tr.is_error:
                block["is_error"] = True
            result_blocks.append(block)
        return [
            *messages,
            {"role": "assistant", "content": assistant_content},
            {"role": "user", "content": result_blocks},
        ]
