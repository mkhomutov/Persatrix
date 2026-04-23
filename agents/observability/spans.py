"""Persatrix span naming conventions and helpers (RFC 0019 § D / § E).

Single source of truth for span names so call sites do not drift across
modules and so the test suite can pin exact names without hard-coding
strings in many places.

Also exposes:

* :func:`gen_ai_attributes` — build the OTEL Gen-AI semantic-convention
  attribute bag for an LLM call (`gen_ai.system`, `gen_ai.request.model`,
  `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
  `gen_ai.response.finish_reasons`, `gen_ai.operation.name`).
* :func:`tool_payload_capture_mode` — read the
  ``PERSATRIX_TRACE_TOOL_PAYLOADS`` env var and return one of
  ``"none"`` / ``"metadata"`` / ``"full"``.  Default is ``"none"`` so
  payloads stay opt-in.
* :func:`apply_redaction` — pass an attribute bag through the project
  ``Redactor`` (RFC 0018 § F) so the same secrets-policy code path
  serves both log records and span attributes.
"""

from __future__ import annotations

import os
from typing import Any

from .redact import NoopRedactor, Redactor

# ─── Span names ──────────────────────────────────────────────────────────────

PERSONA_TICK_SPAN = "agent.persona.tick"
PERSONA_EVENT_SPAN = "agent.persona.event"

EPISODIC_RECALL_SPAN = "agent.memory.episodic.recall"
EPISODIC_REMEMBER_SPAN = "agent.memory.episodic.remember"

RELATIONSHIP_LOOKUP_SPAN = "agent.memory.relationship.lookup"
RELATIONSHIP_UPDATE_SPAN = "agent.memory.relationship.update"

LLM_CALL_SPAN = "agent.llm.call"
TOOL_EXECUTE_SPAN = "agent.tool.execute"
SUBAGENT_SPAWN_SPAN = "agent.subagent.spawn"

# ─── Tool-payload capture (Gen-AI tracing opt-in) ───────────────────────────

_PAYLOAD_ENV = "PERSATRIX_TRACE_TOOL_PAYLOADS"
_VALID_MODES = frozenset({"none", "metadata", "full"})

# Module-level redactor, kept replaceable so a future security RFC can swap
# in a real implementation without touching call sites.  Mirrors the pattern
# used by the structlog chain in ``agents.observability.logging``.
_redactor: Redactor = NoopRedactor()


def set_redactor(redactor: Redactor) -> None:
    """Install a project-wide redactor for tool-payload span attributes.

    Tests and the future security RFC use this hook to replace the default
    :class:`NoopRedactor`.
    """
    global _redactor
    _redactor = redactor


def get_redactor() -> Redactor:
    """Return the currently installed redactor."""
    return _redactor


def tool_payload_capture_mode() -> str:
    """Return the active tool-payload capture mode.

    Reads ``PERSATRIX_TRACE_TOOL_PAYLOADS`` on every call so tests can flip
    the mode with ``monkeypatch.setenv`` without re-importing the module.
    Unknown values are coerced to ``"none"`` (safe default — no leak).
    """
    raw = os.environ.get(_PAYLOAD_ENV, "").strip().lower()
    return raw if raw in _VALID_MODES else "none"


def apply_redaction(attributes: dict[str, Any]) -> dict[str, Any]:
    """Pass ``attributes`` through the installed redactor.

    Returns a new dict (the redactor contract — callers may keep the input).
    """
    return _redactor.redact(attributes)


# ─── Gen-AI semantic-convention helpers ─────────────────────────────────────


def gen_ai_attributes(
    *,
    system: str,
    request_model: str,
    operation: str = "chat",
    response_model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    finish_reasons: list[str] | None = None,
) -> dict[str, Any]:
    """Build an OTEL Gen-AI attribute bag for an LLM-call span.

    Keys mirror the upstream spec verbatim (`gen_ai.*`) so vendor backends
    render Persatrix LLM traces with no Persatrix-specific configuration.
    Unset fields are omitted rather than emitted as ``None`` — span backends
    treat absent attributes more gracefully than null values.
    """
    attrs: dict[str, Any] = {
        "gen_ai.system": system,
        "gen_ai.request.model": request_model,
        "gen_ai.operation.name": operation,
    }
    if response_model:
        attrs["gen_ai.response.model"] = response_model
    if input_tokens is not None:
        attrs["gen_ai.usage.input_tokens"] = input_tokens
    if output_tokens is not None:
        attrs["gen_ai.usage.output_tokens"] = output_tokens
    if finish_reasons:
        attrs["gen_ai.response.finish_reasons"] = list(finish_reasons)
    return attrs


__all__ = [
    "EPISODIC_RECALL_SPAN",
    "EPISODIC_REMEMBER_SPAN",
    "LLM_CALL_SPAN",
    "PERSONA_EVENT_SPAN",
    "PERSONA_TICK_SPAN",
    "RELATIONSHIP_LOOKUP_SPAN",
    "RELATIONSHIP_UPDATE_SPAN",
    "SUBAGENT_SPAWN_SPAN",
    "TOOL_EXECUTE_SPAN",
    "apply_redaction",
    "gen_ai_attributes",
    "get_redactor",
    "set_redactor",
    "tool_payload_capture_mode",
]
