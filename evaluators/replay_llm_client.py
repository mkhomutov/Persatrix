"""RFC 0044 Phase 1 — the replay LLM client (PR 2, resolves OQ #2).

The golden-trace harness replays recorded scenarios with ``llm_mode: replay``
(RFC 0044 §C): the LLM client returns recorded responses instead of calling a
real provider, so an eval is byte-stable and CI-safe despite LLM output being
non-deterministic (§D). This module is that client.

**Cassette shape (OQ #2).** A cassette is a ``{request_hash: response_payload}``
mapping. The key is a SHA-256 over a *canonicalized* request; the value is a
YAML/JSON-safe serialization of an :class:`~agents.llm_types.LLMResponse`. The
canonicalization (:func:`canonicalize_request`) is:

- **order-independent** — dict keys are sorted, so a request hashes the same
  regardless of key insertion order; and
- **volatile-field-stripping** — keys in :data:`DEFAULT_VOLATILE_KEYS`
  (prompt-cache markers, opaque provider round-trip signatures, timestamps /
  idempotency keys) are removed at any nesting depth, so an incidental,
  non-semantic difference between two runs does not cause a replay miss (RFC
  0044 §H OQ #2).

**Record and replay share one canonicalization.** :class:`RecordingProvider`
wraps a live provider and captures the cassette *using the exact hash the
replay side will later look up*, so a recorded golden is guaranteed replayable.
Keeping both halves in one module makes that single-source-of-truth explicit;
the ``make eval-record`` / ``eval-replay`` targets and the runner that drives
recipes through these providers land in PR 3.

**Fail loud on a miss.** :meth:`ReplayProvider.create_message` raises
:class:`ReplayCassetteMissError` when a request is not in the cassette — a drifted
recipe or an incomplete recording must surface, never silently pass.

The cassette's format on disk (a response as a payload, and the YAML file that
holds the mapping) is in :mod:`evaluators.replay_cassette`; its four functions
are re-exported here, where they used to live.

This module depends only on :mod:`agents.llm_types` (the provider Protocol and
data types) and the stdlib + ``pyyaml`` — no orchestrator or network coupling.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from enum import Enum
from pathlib import Path
from typing import Any

from agents.llm_types import (
    LLMResponse,
    LLMToolResult,
)
from evaluators.replay_cassette import (
    dump_cassette,
    load_cassette,
    payload_to_response,
    response_to_payload,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_VOLATILE_KEYS",
    "RecordingProvider",
    "ReplayCassetteMissError",
    "ReplayProvider",
    "canonicalize_request",
    "dump_cassette",
    "hash_request",
    "load_cassette",
    "payload_to_response",
    "response_to_payload",
]

#: Request keys stripped at any nesting depth before hashing (RFC 0044 §H OQ #2) —
#: non-semantic fields that can differ between two identical requests: ``cache_control``
#: (Anthropic prompt-cache markers), ``signature`` (the opaque provider round-trip
#: token, e.g. Gemini's ``thought_signature``, echoed back on later turns), and the
#: transport volatiles ``timestamp`` / ``idempotency_key`` / ``request_id``.
DEFAULT_VOLATILE_KEYS: frozenset[str] = frozenset(
    {"cache_control", "signature", "timestamp", "idempotency_key", "request_id"}
)


class ReplayCassetteMissError(RuntimeError):
    """Raised when a replayed request is absent from the cassette.

    A miss is fatal by design: the replayed request matched no recorded one —
    a replay overlay differing from the record's, or a genuine recipe/prompt
    drift — which must be visible, not silently pass the eval (RFC 0044 §D).
    """


# ─── Canonicalization + hashing ──────────────────────────────────────────────


def _strip_volatile(obj: Any, drop_keys: frozenset[str]) -> Any:
    """Recursively drop ``drop_keys`` from every mapping in ``obj``.

    Lists and scalars pass through structurally; only dict *keys* are filtered,
    at any depth. Returns a new structure (does not mutate the input).
    """
    if isinstance(obj, dict):
        return {
            k: _strip_volatile(v, drop_keys)
            for k, v in obj.items()
            if k not in drop_keys
        }
    if isinstance(obj, (list, tuple)):
        return [_strip_volatile(v, drop_keys) for v in obj]
    return obj


def _json_default(obj: Any) -> Any:
    """Serialize non-JSON request values: ``bytes`` → base64, ``Enum`` → ``value``.

    Anything else raises: a ``repr`` fallback would embed a per-process ``id()``,
    yielding a hash that differs across processes — defeating the very portability
    this canonicalization exists for (``hashlib`` over the salted builtin ``hash``
    for the same reason). Fail loud at record time, not later as a CI replay miss.
    """
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode("ascii")
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(
        f"cannot canonicalize {type(obj).__name__} into a stable request hash; "
        f"request values must be JSON-native, bytes, or an Enum"
    )


def canonicalize_request(
    *,
    model: str,
    messages: list,
    system: str,
    tools: list,
    max_tokens: int,
    temperature: float,
    drop_keys: frozenset[str] = DEFAULT_VOLATILE_KEYS,
) -> str:
    """Return the stable, volatile-stripped JSON string that identifies a request.

    The six ``create_message`` inputs are the full semantic identity of a request
    (a change to any of them is a different question for the model). Volatile keys
    (:data:`DEFAULT_VOLATILE_KEYS`, overridable) are stripped, then the whole
    structure is dumped with sorted keys and no incidental whitespace, so the
    result is byte-identical across runs and processes.
    """
    request = {
        "model": model,
        "messages": messages,
        "system": system,
        "tools": tools,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    stripped = _strip_volatile(request, drop_keys)
    return json.dumps(
        stripped,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def hash_request(
    *,
    model: str,
    messages: list,
    system: str,
    tools: list,
    max_tokens: int,
    temperature: float,
    drop_keys: frozenset[str] = DEFAULT_VOLATILE_KEYS,
) -> str:
    """SHA-256 hex digest of :func:`canonicalize_request` — the cassette key.

    ``hashlib`` (not the salted builtin ``hash``) so the digest is stable across
    processes, which is what makes a recorded cassette portable to CI.
    """
    canon = canonicalize_request(
        model=model,
        messages=messages,
        system=system,
        tools=tools,
        max_tokens=max_tokens,
        temperature=temperature,
        drop_keys=drop_keys,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ─── Tool-round message shape (shared by replay + record) ────────────────────


def _append_tool_round(
    messages: list,
    response: LLMResponse,
    tool_results: list[LLMToolResult],
) -> list:
    """Rebuild the message list after a tool round, Anthropic-block-shaped.

    Mirrors :meth:`agents.llm_offline.MockProvider.append_tool_round` so a replay
    of a tool-using scenario produces the *same* next-turn ``messages`` the live
    run produced — which is what lets the follow-up request canonicalize to the
    same hash and hit the cassette. The opaque ``signature`` is deliberately not
    emitted here (it is a stripped volatile), so record and replay agree.
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


#: Where a recorded tool round keeps the assistant turn as the live provider
#: returned it: a Claude turn's thinking blocks, which the next request must
#: send back unchanged. The live call sends it; the cassette key never sees it.
_LIVE_CONTENT_KEY = "provider_content"


def _hashed_and_live(messages: list) -> tuple[list, list]:
    """*messages* as hashed, and as the live provider gets them.

    A turn that carries its provider's own blocks is hashed in the canonical
    shape and sent with those blocks, so a thinking turn goes back whole while
    record and replay still key on one request.
    """
    hashed: list = []
    live: list = []
    for message in messages:
        if isinstance(message, dict) and _LIVE_CONTENT_KEY in message:
            canonical = {k: v for k, v in message.items() if k != _LIVE_CONTENT_KEY}
            hashed.append(canonical)
            live.append({**canonical, "content": message[_LIVE_CONTENT_KEY]})
        else:
            hashed.append(message)
            live.append(message)
    return hashed, live


# ─── Providers ───────────────────────────────────────────────────────────────


class ReplayProvider:
    """Recorded-response :class:`~agents.llm_types.LLMProvider` (RFC 0044 §C).

    Returns the cassette response whose key matches the canonicalized request
    hash; raises :class:`ReplayCassetteMissError` on a miss. Contacts no SDK and does
    no I/O per call, so replay is deterministic and free.
    """

    name = "replay"

    def __init__(
        self,
        cassette: dict[str, dict[str, Any]],
        *,
        drop_keys: frozenset[str] = DEFAULT_VOLATILE_KEYS,
    ) -> None:
        self._cassette = cassette
        self._drop_keys = drop_keys

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        drop_keys: frozenset[str] = DEFAULT_VOLATILE_KEYS,
    ) -> ReplayProvider:
        """Build a provider from a cassette file (:func:`load_cassette`)."""
        return cls(load_cassette(path), drop_keys=drop_keys)

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
        key = hash_request(
            model=model,
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            drop_keys=self._drop_keys,
        )
        payload = self._cassette.get(key)
        if payload is None:
            raise ReplayCassetteMissError(
                f"no recorded response for request {key[:12]}… "
                f"({len(self._cassette)} response(s) in cassette). Check the replay "
                f"overlay matches the record's (`make eval-replay` pins it) before "
                f"assuming drift and re-recording the golden."
            )
        return payload_to_response(payload)

    def format_tool_definitions(self, tools: list[dict]) -> list[dict]:
        """Pass tool definitions through unchanged (replay issues no live call)."""
        return list(tools)

    def append_tool_round(
        self,
        messages: list,
        response: LLMResponse,
        tool_results: list[LLMToolResult],
    ) -> list:
        return _append_tool_round(messages, response, tool_results)


class RecordingProvider:
    """Wraps a live :class:`~agents.llm_types.LLMProvider`, capturing a cassette.

    Delegates every ``create_message`` to the wrapped provider and stores the
    response keyed by the *same* hash :class:`ReplayProvider` will look up, so a
    recording is guaranteed replayable. This is the record half of ``make
    eval-record`` (RFC 0044 §C); the Makefile target and runner wiring land in
    PR 3. ``name`` mirrors the wrapped provider so OTEL ``gen_ai.system``
    attribution stays correct during a record run. The cassette is single-slot
    per request; a second *differing* response (non-determinism or a retry) is
    lossy — the later wins and a warning is logged.

    Two things reach the live call without entering the key, since replay has
    neither: the calling alias's request settings (``provider_config``, which
    ``LLMClient`` hands over when the wrapped provider takes them), and a
    thinking turn's own content blocks (see :meth:`append_tool_round`).
    """

    def __init__(
        self,
        inner: Any,
        *,
        drop_keys: frozenset[str] = DEFAULT_VOLATILE_KEYS,
    ) -> None:
        self._inner = inner
        self._drop_keys = drop_keys
        self.name = getattr(inner, "name", "recording")
        self.accepts_provider_config = getattr(inner, "accepts_provider_config", False) is True
        self.cassette: dict[str, dict[str, Any]] = {}

    async def create_message(
        self,
        *,
        model: str,
        messages: list,
        system: str,
        tools: list,
        max_tokens: int,
        temperature: float,
        provider_config: dict[str, Any] | None = None,
    ) -> LLMResponse:
        # Hash the RAW request — the cassette key must be provider-agnostic so
        # ReplayProvider (which has no wrapped provider) recomputes it identically.
        # ``tools`` here is the *unformatted* definitions: this provider's
        # format_tool_definitions is a pass-through (see below), so the runtime's
        # ``format_tool_definitions() -> create_message(tools=...)`` sequence hands
        # create_message the raw defs, not the vendor-native shape.
        hashed, live = _hashed_and_live(messages)
        key = hash_request(
            model=model,
            messages=hashed,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            drop_keys=self._drop_keys,
        )
        # Apply the *live* provider's native tool formatting only for the real
        # call — Anthropic wants ``input_schema``, OpenAI a ``{type: function}``
        # wrapper, etc. (agents/llm_providers.py). This shaping stays out of the
        # hash so record and replay key on the same request. So do the alias's
        # request settings, without which the live provider would fall back to
        # the ones it was built with, the seat alias's, on a lane's call too.
        settings = {} if provider_config is None else {"provider_config": provider_config}
        response = await self._inner.create_message(
            model=model,
            messages=live,
            system=system,
            tools=self._inner.format_tool_definitions(tools),
            max_tokens=max_tokens,
            temperature=temperature,
            **settings,
        )
        payload = response_to_payload(response)
        prior = self.cassette.get(key)
        # Single-slot cassette (OQ #2 {hash: response}): a *differing* response for
        # a request already seen this run — non-determinism or a retry — is lossy;
        # warn rather than silently collapse it (RFC 0044 §D). The later one wins.
        if prior is not None and prior != payload:
            logger.warning(
                "recording overwrote a differing response for request %s… — the "
                "single-slot cassette is now lossy (non-determinism or a retry?)",
                key[:12],
            )
        self.cassette[key] = payload
        return response

    def format_tool_definitions(self, tools: list[dict]) -> list[dict]:
        """Return the tool definitions UNCHANGED (not the wrapped provider's shape).

        Critical for record↔replay symmetry: the runtime formats tools via this
        method and passes the result into ``create_message(tools=…)``, and ``tools``
        is one of the six hashed request inputs. The vendor formatters rewrite the
        shape structurally (``parameters`` → ``input_schema`` for Anthropic, a
        ``{type: function}`` wrapper for OpenAI), so if this delegated to the inner
        provider the cassette would be keyed on the vendor-native shape while
        :meth:`ReplayProvider.format_tool_definitions` (no wrapped provider) keys on
        the raw shape — a guaranteed miss for every tool-bearing eval. Both sides
        therefore pass tools through raw; the vendor formatting is applied inside
        :meth:`create_message`, for the live call only.
        """
        return list(tools)

    def append_tool_round(
        self,
        messages: list,
        response: LLMResponse,
        tool_results: list[LLMToolResult],
    ) -> list:
        """Rebuild the post-tool-round messages in the shared canonical shape.

        Uses :func:`_append_tool_round` (Anthropic-block-shaped) rather than
        delegating to ``self._inner.append_tool_round`` on purpose — the same
        symmetry constraint as :meth:`format_tool_definitions`: ReplayProvider has
        no wrapped provider, so both sides must produce the *same* next-turn message
        shape for the follow-up request to re-hash to the recorded key. The
        canonical shape matches the OQ #3 default record provider (Anthropic
        ``quality`` alias); recording a *multi-round tool loop* against a
        non-Anthropic live provider is out of Phase-1 scope.

        A turn that thought (``response.provider_content``) also keeps its own
        blocks beside the canonical ones: :meth:`create_message` sends those to
        the live provider, which needs its thinking blocks back unchanged, and
        hashes the canonical ones (ISSUE-0169).
        """
        rebuilt = _append_tool_round(messages, response, tool_results)
        if response.provider_content is not None:
            rebuilt[-2] = {**rebuilt[-2], _LIVE_CONTENT_KEY: response.provider_content}
        return rebuilt
