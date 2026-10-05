"""RFC 0044 Phase 1 — the golden-trace cassette on disk (OQ #1, OQ #2).

A cassette is a ``{request_hash: response_payload}`` mapping, kept in a YAML
sidecar beside its recipe (``<eval_id>.golden.yaml``). This module is its
format: how an :class:`~agents.llm_types.LLMResponse` becomes a YAML/JSON-safe
payload and back, and how the mapping is written and read.

The request hash that keys a cassette, and the providers that record and
replay through one, are in :mod:`evaluators.replay_llm_client`. These
functions lived there too and are still importable from it; they moved here
so that module could grow without passing the size this repository splits a
file at.

This module depends only on :mod:`agents.llm_types` and the stdlib +
``pyyaml`` — no orchestrator or network coupling.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import yaml

from agents.llm_types import (
    LLMResponse,
    StopReason,
    ToolCall,
    Usage,
)

__all__ = [
    "dump_cassette",
    "load_cassette",
    "payload_to_response",
    "response_to_payload",
]


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
