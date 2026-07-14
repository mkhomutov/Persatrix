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

import yaml

from agents.llm_types import (
    LLMResponse,
    LLMToolResult,
    StopReason,
    ToolCall,
    Usage,
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

    A miss is fatal by design: it means the recipe drifted from the recording
    (or the golden was never recorded for this request), which must be visible
    rather than silently passing the eval (RFC 0044 §D).
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


# ─── Response payload (de)serialization ──────────────────────────────────────


def response_to_payload(response: LLMResponse) -> dict[str, Any]:
    """Serialize an :class:`LLMResponse` to a YAML/JSON-safe cassette payload.

    ``stop_reason`` becomes its string value; the opaque ``signature`` bytes on a
    tool call become base64. Fields that are empty/default (no ``tool_calls``, no
    ``signature``) are omitted so a recorded golden stays readable in review.
    """
    payload: dict[str, Any] = {
        "text": response.text,
        "stop_reason": response.stop_reason.value,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
    }
    if response.tool_calls:
        calls: list[dict[str, Any]] = []
        for tc in response.tool_calls:
            call: dict[str, Any] = {"id": tc.id, "name": tc.name, "input": tc.input}
            if tc.signature is not None:
                call["signature_b64"] = base64.b64encode(tc.signature).decode("ascii")
            calls.append(call)
        payload["tool_calls"] = calls
    return payload


def payload_to_response(payload: dict[str, Any]) -> LLMResponse:
    """Inverse of :func:`response_to_payload`."""
    usage = payload.get("usage") or {}
    tool_calls: list[ToolCall] = []
    for call in payload.get("tool_calls") or []:
        sig_b64 = call.get("signature_b64")
        tool_calls.append(
            ToolCall(
                id=call["id"],
                name=call["name"],
                input=call.get("input") or {},
                signature=base64.b64decode(sig_b64) if sig_b64 is not None else None,
            )
        )
    return LLMResponse(
        text=payload.get("text"),
        tool_calls=tool_calls,
        stop_reason=StopReason(payload.get("stop_reason", StopReason.END_TURN.value)),
        usage=Usage(
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        ),
    )


# ─── Cassette file I/O ───────────────────────────────────────────────────────


def dump_cassette(cassette: dict[str, dict[str, Any]], path: str | Path) -> None:
    """Write a ``{request_hash: response_payload}`` cassette to ``path`` as YAML.

    YAML matches the OQ #1 sidecar decision (``<eval_id>.golden.yaml``); keys are
    sorted so a re-recorded golden produces a minimal, reviewable diff.
    """
    text = yaml.safe_dump(cassette, sort_keys=True, allow_unicode=True, default_flow_style=False)
    Path(path).write_text(text, encoding="utf-8")


def load_cassette(path: str | Path) -> dict[str, dict[str, Any]]:
    """Read a cassette written by :func:`dump_cassette`. An empty file → ``{}``."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"cassette at {path} is not a mapping: got {type(data).__name__}")
    for key, payload in data.items():
        # Validate values at load time so a malformed cassette fails here with a
        # legible error, not a bare AttributeError deep inside payload_to_response.
        if not isinstance(payload, dict):
            raise ValueError(
                f"cassette at {path} has a non-mapping payload for key "
                f"{str(key)[:12]}…: got {type(payload).__name__}"
            )
    return data


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
                f"({len(self._cassette)} response(s) in cassette). The recipe "
                f"drifted from the golden or the response was never recorded — "
                f"re-record with `make eval-record`."
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
    ) -> LLMResponse:
        # Hash the RAW request — the cassette key must be provider-agnostic so
        # ReplayProvider (which has no wrapped provider) recomputes it identically.
        # ``tools`` here is the *unformatted* definitions: this provider's
        # format_tool_definitions is a pass-through (see below), so the runtime's
        # ``format_tool_definitions() -> create_message(tools=...)`` sequence hands
        # create_message the raw defs, not the vendor-native shape.
        key = hash_request(
            model=model,
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            drop_keys=self._drop_keys,
        )
        # Apply the *live* provider's native tool formatting only for the real
        # call — Anthropic wants ``input_schema``, OpenAI a ``{type: function}``
        # wrapper, etc. (agents/llm_providers.py). This shaping stays out of the
        # hash so record and replay key on the same request.
        response = await self._inner.create_message(
            model=model,
            messages=messages,
            system=system,
            tools=self._inner.format_tool_definitions(tools),
            max_tokens=max_tokens,
            temperature=temperature,
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
        """
        return _append_tool_round(messages, response, tool_results)
