"""RFC 0052 — the convener opening turn (persona half).

An *autonomous* channel ([RFC 0052 §B]) opens with no human message: the
orchestrator dispatches a **convene forced turn** to the channel's
configured ``autonomous.convener`` (a directed dispatch carrying the
``convene`` marker, the sibling of the chair-stall-escalation forced turn —
``internal/channels/convene.go``), and the convener persona authors the
opening turn from which the existing ``InboundEventWake`` chain carries the
discussion.

This module is the framing half — the sibling of
:func:`agents.persona_runtime.prompt_assembly.format_chair_escalation`. The
gate admission ([agents/response_gate.py]) and the wire lift
([agents/channel_wire_metadata.py]) mirror ``chair_escalation`` exactly; the
one thing genuinely new here is the **trust class of the seed material**.

The operator-supplied ``topic`` / ``agenda`` / ``goal`` ride in the
dispatched event's ``content`` (assembled orchestrator-side from the
channel's ``autonomous`` config). That is operator config, **not**
persona-authored content — a distinct trust class, and the one genuinely new
injection surface this RFC opens ([RFC §Security]). It is therefore wrapped
in the RFC 0009 ``<external_data>`` envelope before it reaches the convener's
prompt, so the convener treats it as *data describing what to discuss*, never
as instructions. A defensive max-length bound is applied at this wrap seam:
there is no codebase precedent for capping prose fields (the only schema
``maxLength``s are on opaque tokens), so the bound is a deliberate PR 3
decision (the PR 1 deep-review follow-up), sized generously over a realistic
topic+agenda+goal yet bounding prompt bloat / abuse from an unattended
channel.

[RFC 0052 §B]: ../../docs/rfcs/0052-autonomous-agent-channels.md
[RFC §Security]: ../../docs/rfcs/0052-autonomous-agent-channels.md
"""

from __future__ import annotations

import logging

from agents.prompt_loader import load_snippet
from agents.security import CONTEXT_SOURCE_EXTERNAL, wrap_external

logger = logging.getLogger(__name__)

# Defensive upper bound (UTF-8-agnostic character count) on the operator
# convene directive at the injection seam. Generous over a realistic
# topic + multi-item agenda + goal (a 64-item agenda at ~120 chars/item is
# well under this), so it never clips a well-formed directive — it exists to
# bound prompt bloat and abuse from an unattended channel, not to validate
# shape (that is the Go config-validate gate's job, PR 1).
_CONVENE_DIRECTIVE_MAX_CHARS = 8_000

_TRUNCATION_MARKER = "\n[... convene directive truncated ...]"


def _bound_directive(directive: str) -> str:
    """Cap the operator free-text at :data:`_CONVENE_DIRECTIVE_MAX_CHARS`.

    Truncation is preferable to rejection here: the orchestrator already
    validated the ``autonomous`` block at config time, so an over-length
    directive is a pathological-but-armed channel, and an opening turn on a
    truncated-but-bounded topic still convenes — strictly better than a
    silent dispatch failure on an unattended channel.

    The drop is logged at WARNING so it is not *silent*. The operator surface
    only sees the convene ``202`` (the opener is authored async), and the Go
    wire ceiling (``maxConveneDirectiveBytes`` = 64 KiB in
    ``internal/channels/convene.go``) sits far above this prompt bound, so a
    directive between the two limits clears dispatch intact yet loses its tail
    here — the WARN is the same interim operator signal the inbound sanitizer
    relies on (see :func:`format_convener_opening`).
    """
    if len(directive) <= _CONVENE_DIRECTIVE_MAX_CHARS:
        return directive
    logger.warning(
        "convene directive truncated to %d chars (was %d); the operator "
        "topic/agenda/goal exceeds the prompt bound, so its tail is dropped "
        "from the opening turn",
        _CONVENE_DIRECTIVE_MAX_CHARS,
        len(directive),
    )
    return directive[:_CONVENE_DIRECTIVE_MAX_CHARS] + _TRUNCATION_MARKER


def format_convener_opening(directive: str) -> str:
    """Wrap the operator convene directive in the RFC 0009 envelope and
    prepend the convener framing.

    ``directive`` is the operator ``topic``/``agenda``/``goal`` assembled
    orchestrator-side and carried in the convene event's ``content``. It has
    already passed the RFC 0011 PR 5 ingest sanitizer at the runtime boundary
    (``_sanitize_inbound_event`` — the same clearing every channel-message
    body gets), so the envelope marks ``sanitized="true"``; the ``flagged``
    attribute is left ``false`` because the inbound sanitizer's per-message
    flag is not threaded to this format site (the documented floor — a
    pattern that survived clearing is the inbound sanitizer's WARN log, the
    same interim signal every channel body relies on). The envelope's own
    breakout escaping (``wrap_external``) still defends the structural
    separation regardless.
    """
    envelope = wrap_external(
        _bound_directive(directive),
        source=CONTEXT_SOURCE_EXTERNAL,
        flagged=False,
        sanitized=True,
    )
    return f"{load_snippet('convener-opening')}\n\n{envelope}"


__all__ = ["format_convener_opening"]
