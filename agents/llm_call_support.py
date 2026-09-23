"""Helpers :class:`agents.llm_client.LLMClient` uses around each call.

Moved out of :mod:`agents.llm_client` to keep it under the 500-line cap:
the coarse error bucket for failed calls, the trace ID and the input-token
estimate a wallet lease is requested with. ``agents.llm_client`` imports
them back, so the old import paths still work.
"""

from __future__ import annotations

import logging
from typing import Any

from opentelemetry import trace

logger = logging.getLogger(__name__)


# ─── LLM error classification (PR-170 S1) ─────────────────


def _classify_llm_error(exc: BaseException) -> str:
    """Classify an LLM exception into a low-cardinality ``error.type`` bucket.

    Provider SDKs (anthropic, openai) raise their own error types; the
    agent runtime deliberately avoids importing them to stay provider-
    agnostic.  Keyword-match on the exception class name + message instead:
    the resulting buckets are coarse but stable across provider-SDK
    updates and remain within the metric-attribute cardinality budget.
    """
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "ratelimit" in name or "rate_limit" in name or "rate limit" in msg or "429" in msg:
        return "rate_limit"
    if "timeout" in name or "timeout" in msg or isinstance(exc, TimeoutError):
        return "timeout"
    return "provider_error"


# ─── Wallet-lease helpers (RFC 0023 PR 3) ───────────────────


def _current_trace_id() -> str:
    """Return the active OTEL trace ID as a 32-hex string, or ``""``.

    Threaded onto the ``LeaseRequest`` as ``trace_id`` so the wallet-side
    lease logs correlate with the agent's LLM-call span (RFC 0023 § C)."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return ""
    return trace.format_trace_id(ctx.trace_id)


def _estimate_input_tokens(kwargs: dict[str, Any]) -> int:
    """Estimate the prompt's input-token count for the lease request.

    Reuses the project-wide ``cl100k_base`` tokeniser (RFC 0023 Open
    Question §5 — a single tokeniser path system-wide). The estimate
    funds the lease's *provisional* charge only; ``SettleLease``
    reconciles it to the provider-reported actuals, so a best-effort
    flatten of the system prompt + message text is sufficient. Tool
    definitions (``kwargs["tools"]``) are deliberately *not* counted:
    serialising every tool schema would add complexity for no
    enforcement benefit — settle reconciles to actuals, so an estimate
    that under-counts only shrinks the provisional hold, never the
    final charge. A tokeniser import failure degrades to the chars/4
    fallback rather than blocking the call."""
    parts: list[str] = []
    for key in ("cache_prefix", "system"):
        text = kwargs.get(key)
        if isinstance(text, str) and text:
            parts.append(text)
    for msg in kwargs.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                parts.append(text if isinstance(text, str) else str(block.get("content") or ""))
    text = "\n".join(p for p in parts if p)
    try:
        from .persona_runtime.memory_budget import _count_tokens

        return _count_tokens(text)
    except Exception:  # pragma: no cover — estimation must never block a call
        logger.debug("token estimate fell back to chars/4", exc_info=True)
        return max(0, len(text) // 4)
