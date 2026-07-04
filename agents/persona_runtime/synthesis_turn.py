"""RFC 0052 §D — the chair synthesis turn (persona half, PR 4b-ii).

When the deterministic bounded close trips (``autonomous.max_rounds`` /
the wallet soft budget), the orchestrator dispatches a **synthesis forced
turn** to the channel's ``escalation_chair_id`` (a directed dispatch
carrying the ``synthesis_turn`` marker — ``internal/channels/
synthesis_close.go``), and the chair authors the goal-directed closing
synthesis. The reply is recognised orchestrator-side as the CLOSING
ARTIFACT (the close-on-reply ordering: the chair's publish echoes its
dispatched-under interaction id as the wire claim, and the armed close
consumes it instead of re-fanning it), so the chair proposes and the
orchestrator disposes — RFC 0030 CE4 stays intact.

This module is the framing half — the ``convener.py`` sibling, and the
same trust story: the operator ``goal``/``topic`` directive rides in the
dispatched event's ``content`` (assembled orchestrator-side from the
channel's ``autonomous`` config). That is operator config, a distinct
trust class from persona-authored content, so it is wrapped in the RFC
0009 ``<external_data>`` envelope before it reaches the chair's prompt,
with the same defensive max-length bound the convene seam applies.
"""

from __future__ import annotations

import logging

from agents.prompt_loader import load_snippet
from agents.security import CONTEXT_SOURCE_EXTERNAL, wrap_external

logger = logging.getLogger(__name__)

# Defensive upper bound on the operator synthesis directive at the injection
# seam — the ``_CONVENE_DIRECTIVE_MAX_CHARS`` twin (the directive is composed
# from the same ``goal``/``topic`` fields the convene directive draws on, so
# the same generous-over-realistic sizing applies).
_SYNTHESIS_DIRECTIVE_MAX_CHARS = 8_000

_TRUNCATION_MARKER = "\n[... synthesis directive truncated ...]"


def _bound_directive(directive: str) -> str:
    """Cap the operator free-text at :data:`_SYNTHESIS_DIRECTIVE_MAX_CHARS`.

    Truncation over rejection, the convene rationale: a synthesis on a
    truncated-but-bounded goal still closes the discussion with an
    artifact — strictly better than a silent dispatch failure that would
    leave the bounded close to its artifact-less timeout fallback. The
    drop is WARN-logged so it is not silent.
    """
    if len(directive) <= _SYNTHESIS_DIRECTIVE_MAX_CHARS:
        return directive
    logger.warning(
        "synthesis directive truncated to %d chars (was %d); the operator "
        "goal/topic exceeds the prompt bound, so its tail is dropped from "
        "the synthesis turn",
        _SYNTHESIS_DIRECTIVE_MAX_CHARS,
        len(directive),
    )
    return directive[:_SYNTHESIS_DIRECTIVE_MAX_CHARS] + _TRUNCATION_MARKER


def format_synthesis_turn(directive: str) -> str:
    """Wrap the operator synthesis directive in the RFC 0009 envelope and
    prepend the synthesis framing.

    ``directive`` is the operator ``goal``/``topic`` assembled
    orchestrator-side and carried in the synthesis event's ``content``. It
    has already passed the RFC 0011 PR 5 ingest sanitizer at the runtime
    boundary, so the envelope marks ``sanitized="true"``; ``flagged`` stays
    ``False`` for the same reason as the convene seam (the inbound
    sanitizer's per-message flag is not threaded to this format site — its
    WARN log is the documented interim signal). The envelope's breakout
    escaping (``wrap_external``) defends the structural separation
    regardless.
    """
    envelope = wrap_external(
        _bound_directive(directive),
        source=CONTEXT_SOURCE_EXTERNAL,
        flagged=False,
        sanitized=True,
    )
    return f"{load_snippet('synthesis-turn')}\n\n{envelope}"


__all__ = ["format_synthesis_turn"]
