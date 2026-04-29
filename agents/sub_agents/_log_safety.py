"""Log-safety helpers — control-char strip + length cap for log payloads.

Lifted from :mod:`agents.sub_agents.spawner` in RFC 0008 PR 6b
(`feature/v030-rfc0008-procedural-cleanup`) per the [PR 3a R4 L4 / R5 S1
follow-up](../../docs/rfcs/0008-pr-plan.md#consolidated-triage-table).

Single source of truth for the CWE-117 / OWASP A09 log-injection defence
applied to attacker-influenceable text (sub-agent ``output.result``,
wrapped :class:`DelegationContractError` text, JSON decode error
messages, etc.) before it is interpolated into orchestrator log lines or
:class:`DelegationFailure` messages.

Two callers re-import these symbols:

* :mod:`agents.sub_agents.spawner` — every ``DelegationFailure`` raise
  site that interpolates an attacker-influenceable string.
* :mod:`agents.task_agent` — debug-log of a malformed
  :class:`DelegationResult` payload received from an LLM.

The helpers are deliberately dependency-free so future callers (e.g.
:mod:`agents.memory.facade` warn-logs over recall keys) can adopt them
without dragging the spawner module into their import graph.
"""

from __future__ import annotations

# ─── Control-character strip ──────────────────────────────────────────
# PR #224 review round-3 (Should #1): volume-bounding alone is
# insufficient defence against CWE-117 log injection.  A sub-agent that
# returns ``output.result = "harmless\n[ERROR] fake admin alert"`` (well
# under the 200-char cap) will inject a forged log line into the
# ``DelegationFailure`` message that downstream
# ``logger.error("dispatch failed: %s", exc)`` calls render verbatim
# across newlines.  We therefore strip *all* C0 control characters
# (0x00-0x1F) plus DEL (0x7F) to a visible sentinel ``"\u2424"`` (U+2424
# SYMBOL FOR NEWLINE) before truncation.
#
# Sentinel choice rationale:
#
# * Printable Unicode glyph that survives any sane log pipeline.
# * Visually distinct from real text so operators can spot the strip in
#   grep output.
# * Does not collide with any control character it replaces.
# * Encoding note (PR 3a R4 L6): U+2424 is a single Unicode code point
#   (3 bytes in UTF-8: ``e2 90 a4``) — log pipelines that re-encode to
#   ASCII (e.g. naïve ``str.encode("ascii")``) will mojibake or drop the
#   sentinel.  This is acceptable: the strip step has already
#   neutralised the injection vector before the sentinel is rendered;
#   the sentinel itself is a *visibility aid* for operators inspecting
#   logs, not a security boundary.  Operators running ASCII-only
#   pipelines should configure their log formatter to escape non-ASCII
#   (e.g. Python's ``logging.Formatter`` with ``%(message)s`` then
#   ``ascii(...)`` post-process) rather than rely on the sentinel
#   surviving the round-trip.
#
# TAB (0x09) is included in the strip set because tab-separated log
# formats would otherwise allow column injection.
_CTRL_REPLACEMENT = "\u2424"
_CTRL_TRANSLATION = str.maketrans(
    {c: _CTRL_REPLACEMENT for c in list(range(0x20)) + [0x7F]}
)

# ─── Default length cap ───────────────────────────────────────────────
# PR #224 review round-2 (S2-mirror): ``DelegationFailure`` messages are
# rendered into orchestrator logs and propagated to callers; sub-agent
# ``output.result`` and wrapped ``DelegationContractError`` text both
# carry attacker-influenceable payloads (LLM01 / OWASP A09
# log-injection).  All ``DelegationFailure`` raise sites that
# interpolate such strings must funnel them through :func:`bounded` to
# cap log surface.  Cap is generous enough for triage signal but small
# enough to prevent payload echo.  Single source of truth so any future
# tweak applies uniformly.
_DELEGATION_FAILURE_MESSAGE_CAP = 200


def bounded(text: object, *, cap: int = _DELEGATION_FAILURE_MESSAGE_CAP) -> str:
    """Sanitise and truncate *text* for inclusion in error messages.

    Performs two defences against attacker-influenceable text reaching
    orchestrator logs (LLM01 / OWASP A09 / CWE-117):

    1. **Control-character strip** — every C0 control character
       (0x00-0x1F) plus DEL (0x7F) is replaced with
       :data:`_CTRL_REPLACEMENT`.  This neutralises forged-line
       injection (`\\n`, `\\r`), ANSI escape sequences (`\\x1b[...`),
       NUL bytes, and tab-column injection in TSV-style log pipelines.
    2. **Length cap** — strings longer than *cap* are truncated to the
       first *cap* characters followed by the canonical marker
       ``"… (truncated)"``.

    Non-string inputs (e.g. ``Exception`` instances) are coerced via
    ``str()`` first.  The marker matches the one already used by
    :meth:`agents.sub_agents.spawner.SubAgentSpawner._enforce_output_schema`
    so log-grep tooling sees one canonical form.

    PR #224 review round-3 (Should #1): added the control-char strip
    step after round-2's volume cap left the injection vector open.
    """
    s = text if isinstance(text, str) else str(text)
    s = s.translate(_CTRL_TRANSLATION)
    if len(s) <= cap:
        return s
    return s[:cap] + "… (truncated)"


# Backwards-compat private alias retained for the duration of the v0.3.x
# series so any out-of-tree caller pinned to the spawner-internal name
# keeps working.  Remove in v0.4.0 once the public ``bounded`` name has
# been the documented form for a full release cycle.
_bounded = bounded


__all__ = [
    "bounded",
    "_bounded",
    "_CTRL_REPLACEMENT",
    "_CTRL_TRANSLATION",
    "_DELEGATION_FAILURE_MESSAGE_CAP",
]
