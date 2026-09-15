"""Offline / mock LLM provider for zero-cost demos and CI smoke runs.

A third :class:`~agents.llm_types.LLMProvider` implementation alongside
``AnthropicProvider`` / ``OpenAIProvider`` (see :mod:`agents.llm_providers`).
It returns scripted or persona-flavoured text **without any network call,
API key, or token spend**, so the whole agent society — chat, channels,
memory, the wallet-lease path, OpenTelemetry traces — runs end-to-end for
$0 and zero risk.

Activation is purely config/alias-driven — the **same standard way** every
other provider is selected (RFC 0033). There is no global force-knob:

* ``provider: mock`` on a ``models.aliases`` entry the agents reference
  (e.g. ``quality`` → ``{provider: mock, model: offline, …}``) routes the
  whole society to the mock — this is how ``make demo-offline`` works.
* ``provider: mock`` directly on an agent's ``config/agents.yaml`` entry
  opts a single agent in.

Both flow through :func:`agents.llm_client.create_provider`'s provider
dispatch. The curated-replies file path is read from
``PERSATRIX_OFFLINE_RESPONSES`` (mock *configuration*, analogous to an API
key — not a provider-selection knob); it defaults to
``config/offline_responses.yaml``.

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

**Open-floor bids.** A group channel asks each participant first whether to
post at all (:mod:`agents.salience_bid`), and parses the answer — so that one
call gets a verdict in its grammar, never a reply (ISSUE-0160: answering it
with a discussion paragraph silenced every participant as ``parse_failure``).
The verdict is *speak* when the persona has a scripted point for the new
message that it has not already made in the window, and *silent* otherwise;
the catch-all (``match: []``) answers a direct message but is never a reason
to join a room. The reply turn that follows makes that same unmade point, so
a persona walks through its scripted points instead of repeating the first.

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
    "reset_cache",
]

_RESPONSES_ENV = "PERSATRIX_OFFLINE_RESPONSES"

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

# The open-floor bid prompt (``agents.salience_bid._build_bid_messages``) is ONE
# user message: the round's transcript, the new message, then the instruction,
# which ends in the answer form its parser reads — ``should_post: yes|no`` for
# ``reasoning.mode`` bid/plan (``agents.salience_deliberation``), ``speak: yes|no``
# for ``off``. The three ``salience-bid-*user.md`` snippets all open with the
# instruction marker below; ``tests/unit/python/test_llm_offline_bid.py`` drives
# the real gate in every mode, so a reworded snippet fails there, not in a demo.
_BID_HEAD = "Conversation so far this round:\n"
_BID_NEW_MESSAGE = "\n\nNew message:\n"
_BID_INSTRUCTION = "\n\nDecide whether you have something genuinely new"
_STRUCTURED_FORM_RE = re.compile(r"^should_post:\s*yes\|no\s*$", re.MULTILINE)
_SCALAR_FORM_RE = re.compile(r"^speak:\s*yes\|no\s*$", re.MULTILINE)

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


def _closing_replies() -> frozenset[str]:
    """Every persona's reply marked ``closes: true`` — the lines that end a
    discussion (the chair's closing synthesis), normalised for comparison."""
    return frozenset(
        _normalised(entry["reply"])
        for entries in _load_responses().values()
        for entry in entries
        if entry.get("closes") is True and isinstance(entry.get("reply"), str)
    )


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


def _normalised(text: str) -> str:
    """Whitespace-collapsed, lower-cased text, for "was this point already made"."""
    return " ".join(text.split()).lower()


def _window_turns(messages: list[Any]) -> list[tuple[bool, str]]:
    """``(own, text)`` for every turn before the current one (the last)."""
    return [
        (m.get("role") == "assistant", _content_to_text(m.get("content")))
        for m in (messages or [])[:-1]
        if isinstance(m, dict)
    ]


_BID_TURN_RE = re.compile(r"^(Me|Thread): ", re.MULTILINE)

# The speaker prefix a replayed or dispatched peer turn carries ahead of its
# text: ``[iron-fox]: `` (conversation window) or ``Message from iron-fox: ``.
_PEER_PREFIX_RE = re.compile(
    r"^\s*(?:<\|user_message[^|]*\|>\s*)?(?:\[[a-z0-9-]+\]:|Message from [a-z0-9-]+:)\s*",
)


def _split_bid(text: str) -> tuple[list[tuple[bool, str]], str, bool] | None:
    """``(turns, new_message, structured)`` when ``text`` is an open-floor bid
    prompt, else ``None``. The transcript renders the persona's own posts as
    ``Me:`` lines and everyone else's as ``Thread:`` lines."""
    structured = _STRUCTURED_FORM_RE.search(text) is not None
    if not structured and _SCALAR_FORM_RE.search(text) is None:
        return None
    cut = text.rfind(_BID_INSTRUCTION)
    if not text.startswith(_BID_HEAD) or cut < 0 or _BID_NEW_MESSAGE not in text[:cut]:
        return None
    transcript, _, new_message = text[len(_BID_HEAD):cut].rpartition(_BID_NEW_MESSAGE)
    parts = _BID_TURN_RE.split(transcript)
    speakers, bodies = parts[1::2], parts[2::2]
    turns = [(speaker == "Me", body) for speaker, body in zip(speakers, bodies, strict=True)]
    return turns, new_message, structured


def _verdict(*, speak: bool, already_made: bool, structured: bool) -> str:
    """The bid answer in the form the gate parses."""
    if not structured:
        return "speak: yes\nscore: 1.0" if speak else "speak: no\nscore: 0.0"
    if speak:
        return (
            "should_post: yes\nreason_code: adds_substance\n"
            "reason_note: a scripted point not yet made"
        )
    code = "already_answered" if already_made else "nothing_to_add"
    return f"should_post: no\nreason_code: {code}\nreason_note: no scripted point left to make"


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
        bid = _split_bid(user_text)
        if bid is not None:
            reply = self._bid_verdict(*bid)
        else:
            scripted = self._scripted_reply(user_text, made=self._made(_window_turns(messages)))
            reply = scripted if scripted is not None else self._fallback_reply(user_text)

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

    def _points(self, user_text: str, made: str) -> tuple[list[str], list[str], str | None]:
        """The curated replies for ``user_text``: ``(unmade, made, catch_all)``.

        An entry matches when every keyword in its ``match`` list is a
        case-insensitive substring of the message; an empty ``match`` list is
        the catch-all. A specific reply counts as *made* when the persona's own
        earlier turns (``made``) already contain it. File order is kept, so
        list specific scenarios first.
        """
        haystack = _clean(user_text).lower()
        said = _normalised(made)
        unmade: list[str] = []
        repeats: list[str] = []
        catch_all: str | None = None
        for entry in _load_responses().get(self._agent_id, []):
            reply = entry.get("reply")
            keywords = entry.get("match", [])
            if not isinstance(reply, str) or not isinstance(keywords, list):
                continue
            if not all(isinstance(k, str) and k.lower() in haystack for k in keywords):
                continue
            if not keywords:
                catch_all = catch_all or reply.strip()
            elif said and _normalised(reply) in said:
                repeats.append(reply.strip())
            else:
                unmade.append(reply.strip())
        return unmade, repeats, catch_all

    def _made(self, turns: list[tuple[bool, str]]) -> str:
        """The points this persona has made — or answered — in the window.

        Its own turns count, and so does the point it would have made on each
        earlier peer turn: it bid on every message it saw. The replay is what
        stops a repeat the window alone would allow — a message handled after
        the persona's own reply was published does not carry that reply, since
        the runtime drops rows newer than the message being answered. A closing
        line (``closes: true``) ends a discussion, so the replay starts over
        after it: the channel window outlives the discussion, and a re-convened
        room would otherwise find every point already made.
        """
        closing = _closing_replies()
        made: list[str] = []
        for own, text in turns:
            if _normalised(_clean(_PEER_PREFIX_RE.sub("", text))) in closing:
                made = []
                continue
            if not own:
                unmade, _, _ = self._points(text, "\n".join(made))
                text = unmade[0] if unmade else ""
            made.append(text)
        return "\n".join(made)

    def _scripted_reply(self, user_text: str, *, made: str = "") -> str | None:
        """The first scripted point not yet made; else the catch-all once; else
        the first matching point again (the last resort, and the pre-ISSUE-0160
        behaviour a question asked a third time still gets).

        The middle step serves a turn the persona is handed without a bid — a
        reply that @-mentions it, or the floor re-fanning a round's last reply
        — when its scripted points are spent: it answers with its generic line
        rather than re-posting a paragraph. It does not stay silent: a silent
        reply on such a turn left a booted roundtable open with nothing further
        dispatched (ISSUE-0160, Notes).
        """
        unmade, repeats, catch_all = self._points(user_text, made)
        if unmade:
            return unmade[0]
        if catch_all is not None and _normalised(catch_all) not in _normalised(made):
            return catch_all
        return repeats[0] if repeats else catch_all

    def _bid_verdict(
        self, turns: list[tuple[bool, str]], new_message: str, structured: bool,
    ) -> str:
        """Speak only with a scripted point for the new message not yet made."""
        unmade, repeats, _ = self._points(new_message, self._made(turns))
        return _verdict(speak=bool(unmade), already_made=bool(repeats), structured=structured)

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
