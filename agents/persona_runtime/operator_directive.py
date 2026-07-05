"""RFC 0052 — shared framing for the orchestrator's operator-directive
forced turns (the convene opener, §B; the chair synthesis turn, §D).

Both forced turns inject operator-supplied ``topic``/``agenda``/``goal``
config — a distinct trust class from persona-authored content — carried in a
directed event's ``content``. This module is the ONE place that (a) caps the
operator free-text at a defensive prompt bound (truncation over rejection,
WARN-logged so it is not silent) and (b) wraps the result in the RFC 0009
``<external_data>`` envelope under a named framing snippet. The two seams
(``convener.py``, ``synthesis_turn.py``) differ only in the framing snippet,
the directive "kind" word, and the per-seam max-char bound — everything else
was byte-for-byte duplicated before this split (PR #718 review).

Each seam passes its own module ``logger`` so the truncation WARNING keeps its
call-site attribution (the convene/synthesis logger name, which the seams'
``caplog``-scoped tests assert against).
"""

from __future__ import annotations

import logging

from agents.prompt_loader import load_snippet
from agents.security import CONTEXT_SOURCE_EXTERNAL, wrap_external

# Defensive default bound (UTF-8-agnostic character count) on an operator
# directive at the injection seam. Generous over a realistic
# topic + multi-item agenda + goal (a 64-item agenda at ~120 chars/item is well
# under it), so it never clips a well-formed directive — it exists to bound
# prompt bloat and abuse from an unattended channel, not to validate shape (the
# Go config-validate gate's job, PR 1). Single-sourced here (PR #718 review) so
# both forced-turn seams — the convene opener (§B) and the synthesis turn (§D),
# which share the same trust story and the same sizing — draw one value; a
# future tune moves one constant, not two twins. The Go wire ceiling
# (``maxConveneDirectiveBytes`` = 64 KiB) sits far above this prompt bound.
OPERATOR_DIRECTIVE_MAX_CHARS = 8_000


def bound_directive(
    directive: str, *, max_chars: int, kind: str, logger: logging.Logger
) -> str:
    """Cap the operator free-text at ``max_chars`` characters.

    Truncation over rejection, the convene rationale: a forced turn on a
    truncated-but-bounded directive still does its job (opens or closes the
    discussion with an artifact) — strictly better than a silent dispatch
    failure on an unattended channel. The Go wire ceiling
    (``maxConveneDirectiveBytes`` = 64 KiB) sits far above this prompt bound,
    so a directive between the two clears dispatch intact yet loses its tail
    here; the drop is WARN-logged (via the seam's own ``logger``) so it is not
    silent.
    """
    if len(directive) <= max_chars:
        return directive
    logger.warning(
        "%s directive truncated to %d chars (was %d); the operator "
        "topic/agenda/goal exceeds the prompt bound, so its tail is dropped "
        "from the %s turn",
        kind, max_chars, len(directive), kind,
    )
    return directive[:max_chars] + f"\n[... {kind} directive truncated ...]"


def format_operator_directive(
    directive: str, *, snippet: str, max_chars: int, kind: str, logger: logging.Logger
) -> str:
    """Bound the operator directive, wrap it in the RFC 0009 envelope, and
    prepend the ``snippet`` framing.

    ``directive`` has already passed the RFC 0011 PR 5 ingest sanitizer at the
    runtime boundary, so the envelope marks ``sanitized="true"``; ``flagged``
    stays ``False`` because the inbound sanitizer's per-message flag is not
    threaded to this format site (its WARN log is the documented interim
    signal). The envelope's breakout escaping (``wrap_external``) defends the
    structural separation regardless.
    """
    envelope = wrap_external(
        bound_directive(directive, max_chars=max_chars, kind=kind, logger=logger),
        source=CONTEXT_SOURCE_EXTERNAL,
        flagged=False,
        sanitized=True,
    )
    return f"{load_snippet(snippet)}\n\n{envelope}"


__all__ = [
    "OPERATOR_DIRECTIVE_MAX_CHARS",
    "bound_directive",
    "format_operator_directive",
]
