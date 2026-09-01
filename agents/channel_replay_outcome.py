"""What one on-startup catch-up pass learned about its own completeness.

Split out of :mod:`agents.channel_catchup` (v0.3.15 PR B2 review round 3),
which sat at the 500-line cap ``scripts/checks/file_size.py --strict``
enforces.  The seam is a real one: this is the value the catch-up loop
PRODUCES and the ISSUE-0130 (b) derivation gate
(:func:`agents.persona_runtime.replay_sweep.close_replayed_scopes`)
CONSUMES, so it belongs to neither module's internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["ReplayPassOutcome"]


@dataclass
class ReplayPassOutcome:
    """What one catch-up pass learned about its own COMPLETENESS.

    Mutated in place, never returned — the same reason ``counts`` is
    (below): the pass runs under ``asyncio.wait_for`` and may be cancelled
    mid-flight, and a return value is exactly what is lost then.  The
    caller owns the object and reads it in a ``finally``.

    Two axes, because a pass can be incomplete in two different shapes and
    they do not have the same blast radius:

    * ``completed`` — channels whose window reached the END of the loop.
      A channel missing here was cut by the wall-clock budget or never
      reached, so EVERY record it opened holds a prefix.
    * ``speaker_gaps`` — ``(channel_id, sender_id)`` pairs where one row
      raised inside ``on_event``.  That leaves a hole in exactly ONE
      speaker's record, since records are keyed per speaker; the first cut
      disqualified the whole channel for it, so one deterministically
      raising row cost every OTHER speaker in that room their derivation,
      on every boot, forever.  A row whose sender cannot be read is the
      one case that still falls back to disqualifying the channel — an
      unattributable gap could be in any record.
    """

    completed: set[str] = field(default_factory=set)
    speaker_gaps: set[tuple[str, str]] = field(default_factory=set)
