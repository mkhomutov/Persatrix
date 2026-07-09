"""RFC 0052 §E — the convener-side reverser for a standing convene timer id.

The Python inverse of the Go producer's timer-id encoding
(:func:`internal/channels/standing_schedule.go` ``standingConveneTimerID`` /
``ParseStandingConveneTimerID``). PR 7c-i landed the producer — the pure
derivation from an armed standing channel to the RFC 0024
``autonomy.timers`` entry a config round-trip registers in the convener's
``agents.yaml`` (``callback_kind="convene"``, ``id="convene-<name>"``). This
module is the consumer half of the *id* contract: when that timer fires, the
convener-side :class:`agents.tick.TickScheduler` handler (PR 7c-ii-a) must
recover the group channel to convene, and a fired
:class:`agents.event_loop_types.ScheduledWake` carries only ``timer_id`` +
``callback_kind`` — **no** ``channel_id`` — so the id is the only channel
reference.

Two facts are copied from the Go source (and pinned against it by
``agents/tests/test_convene_timer.py`` so a Go-side change desyncs loudly):

* :data:`STANDING_CONVENE_KIND` — the ``ScheduledWake.callback_kind`` the
  dispatch branch keys on, distinct from the ``"tick"`` the legacy timer
  carries and from the unrelated ``"convene"`` barewords elsewhere (the RFC
  0009 forced-turn marker, the wire flag, the convener-opening directive
  ``kind``); this constant is the ``callback_kind`` namespace alone.
* the encoding: a fixed ``convene-`` prefix over a group channel *name* drawn
  from the same lowercase-alnum-plus-hyphen charset the Go
  ``channelNamePattern`` enforces.

:func:`parse_standing_convene_timer_id` is a strict inverse over the encoder's
range — it recovers a channel id only from an id this producer could have
emitted, and returns ``None`` for anything else (another kind's entry, a bare
or empty prefix, or a schema-valid ``autonomy.timers[].id`` whose name no group
channel could carry, e.g. one containing ``_``). Rejecting rather than
mis-decoding makes it a safe classifier: an operator-named ``convene-*``
non-convene timer can never decode to a bogus convene target.
"""

from __future__ import annotations

import re

# The ``ScheduledWake.callback_kind`` a fired standing-convene timer carries —
# mirrors ``StandingConveneKind`` in internal/channels/standing_schedule.go.
STANDING_CONVENE_KIND = "convene"

# The fixed marker a convene timer id begins with — mirrors
# ``standingConveneTimerPrefix``. The trailing ``-`` is an unambiguous split
# point because a channel name never starts with ``-``.
_STANDING_CONVENE_TIMER_PREFIX = "convene-"

# The canonical group-address prefix — mirrors ``ChannelTypeGroup + ":"``.
_GROUP_PREFIX = "group:"

# Mirrors ``channelNamePattern`` in internal/channels/channels.go: lowercase
# alnum with interior hyphens, anchored, minimum length two (must start AND end
# on an alnum). A subset of the timer-id charset (which also admits ``_``), so
# the reverser rejects a schema-valid id whose name this pattern refuses.
#
# The pattern string is copied VERBATIM from the Go source (``^``/``$`` anchors
# included) because the drift guard pins ``.pattern`` byte-for-byte against
# ``channelNamePattern`` — but it is applied below with
# :meth:`re.Pattern.fullmatch`, NOT :meth:`~re.Pattern.match`. Go's RE2 ``$`` is
# end-of-text, whereas Python's ``re`` ``$`` also matches just *before* a trailing
# newline, so ``match`` would accept a ``name`` like ``"foo\n"`` that the Go
# encoder's range excludes. ``fullmatch`` (whole-string) closes that
# one-character cross-language gap while leaving ``.pattern`` unchanged.
_CHANNEL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


def parse_standing_convene_timer_id(timer_id: str) -> str | None:
    """Recover the canonical ``group:<name>`` channel a convene timer id encodes.

    Returns ``None`` when ``timer_id`` is not a convene timer this producer
    emits: no ``convene-`` prefix, an empty name, or a name outside the group
    channel-name charset (see :data:`_CHANNEL_NAME_PATTERN`). The strip is a
    single (non-greedy) prefix removal, so a channel literally named
    ``convene-foo`` — encoded ``convene-convene-foo`` — round-trips.
    """
    if not timer_id.startswith(_STANDING_CONVENE_TIMER_PREFIX):
        return None
    name = timer_id[len(_STANDING_CONVENE_TIMER_PREFIX):]
    # fullmatch (not match): the pattern's ``$`` matches before a trailing
    # newline in Python but not in Go's RE2, so match would over-accept relative
    # to the encoder — see the _CHANNEL_NAME_PATTERN note above.
    if not _CHANNEL_NAME_PATTERN.fullmatch(name):
        return None
    return _GROUP_PREFIX + name
