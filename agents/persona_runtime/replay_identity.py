"""ISSUE-0130 shape (b) — when two replayed spans are the same span.

The idempotence half of PR B2, and the reason narrowing the shape-(a)
derivation skip is safe to ship.

Shape (a) stopped the leak the v0.3.14 MT measured — ``local`` episodes
growing ``0 → 2 → 5 → 13 → 18`` across four restarts — by deriving
nothing at all from a replayed span.  B2 hands that derivation back, now
correctly attributed, and would hand the growth curve back with it:
catch-up still has no watermark (RFC 0011 OQ #8), so every boot re-reads
the same last-N window and would summarise it again under the same
tenant.  Relocating an unbounded write from the wrong tenant to the right
one is not a fix.

So a replayed span gets an identity that does not change between boots,
and the close path declines to derive one it has already derived.  The
identity is the record's own content: its ``(principal, speaker, scope)``
key, the agent, every OTHER axis the row will be stamped and later filtered
by, and the ordered wire ids of the messages it replays.  Every axis the
stored row is partitioned by has to be in the digest, or the guard reaches
across that partition and suppresses a derivation the asking side can never
read.  Three such axes are not part of the record key and so are named
explicitly: the ACTIVE EPOCH, the SESSION, and the RFC 0037 §D
``protection_level``.  All three are read off the record or handed in by the
close path that is about to stamp them, never resolved independently here —
a digest that disagrees with the row it guards is the whole failure mode.

Reading the epoch AMBIENT — which the first cut of this module did, via
``episodic.active_epoch_id()`` — was itself a boot-instability rather than
a fix, because the ambient epoch depends on WHICH close path fires and not
on the span: the pass-end sweep runs with no request scope bound, while the
ingest-time split runs inside a live event's ``on_event``, where
``request_scope_from_metadata`` has bound that request's epoch.  The same
window therefore hashed one way on a boot no live turn interrupted and
another way on a boot it did, and the guard missed (PR B2 review).
Nothing in that is clock- or boot-derived — ``interaction_id`` normally is
(``uuid4``), and ``started_at`` is boot time, not wire time, which is
exactly why neither can answer this.

**What it does and does not bound.** The same window replayed again
derives once, however many times the process restarts — the release
acceptance bar.  A window that has MOVED (new messages arrived while the
agent was down, or old ones aged out of the last-N page) is a different
span with a different identity, and it derives again, overlapping the
earlier episode's content.  That residual is bounded by "restarts that
had traffic in between" rather than by "restarts", and closing it needs
the OQ #8(b) ``?since=`` watermark, which stays out of scope here.  The
direction of the residual is deliberate: an identity that matched a moved
window would silently drop the messages that moved it, and losing memory
is worse than duplicating it.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory.episodic import EpisodicMemory
    from ..memory.interaction_types import Interaction

__all__ = ["replay_span_already_derived", "replay_span_identity"]

logger = logging.getLogger(__name__)

#: Prefix on the derived ``interaction_id`` so a replay-derived episode is
#: identifiable as one in the store, in logs and in the MT's row dumps
#: without joining anything.  A ``uuid4`` cannot collide with it.
REPLAY_INTERACTION_ID_PREFIX = "replay-"


def replay_span_identity(
    interaction: Interaction,
    agent_id: str,
    epoch_id: str,
    protection_level: str,
    message_ids: list[str],
) -> str | None:
    """The boot-stable id for a replayed span, or ``None``.

    ``message_ids`` carries ONE ENTRY PER TURN in span order, ``""``
    where that turn had no wire id.  ``None`` means the span cannot be
    identified — it has no turns, or ANY turn carried no wire id — and
    the caller should derive WITHOUT the dedup guard rather than skip:
    the guard exists to bound duplication, and trading it for silently
    losing a span's memory inverts the cost.

    Requiring EVERY turn to be identified is the whole contract, not a
    strictness preference.  A digest over the identified subset is
    stable across two spans that differ only in their unidentified
    turns, so the second span matches the first and is skipped — its
    content never summarised, never extracted, and never retried,
    because the record is popped by then.  That is the losing direction
    this module refuses.

    The premise cannot be assumed away at the wire either:
    ``validate_channel_message_dict`` reads ``msg.get("id", "")`` and
    only type- and length-checks it, so a history row with a missing or
    empty ``id`` reaches the builder.  Reaching ``None`` is therefore
    rare rather than impossible, and is logged at WARN because it means
    the guard is off for that span.

    The agent id is in the digest as well as in the lookup that consumes
    it: the id is written to ``episodes.interaction_id``, a column
    nothing constrains to one agent, and two personas replaying the same
    room legitimately derive their own episodes from the same messages.

    ``epoch_id``, ``interaction.session_id`` and ``protection_level`` are
    in the digest for the same reason the principal is: each is written
    onto the row and then filtered on read, so a digest missing one lets a
    row written under one value suppress derivation under another, where
    that row can never be read — the span's memory silently absent from
    the partition that asked for it, permanently, since every later boot
    re-matches the same row.

    * ``epoch_id`` — ``store_episode`` stamps it and every episodic recall
      filters it with unconditional strict equality, no ``"*"`` bypass and
      no carve-out (:mod:`agents.memory._epoch_filter`).  It is the axis
      the first cut missed twice: once by omitting it, then by reading it
      ambient instead of off the record.
    * ``session_id`` — same shape, one axis over, and missed the same way
      (PR B2 review).  It is written from ``interaction.session_id`` and
      walled by ``session_in_clause`` on every episodic read, with a
      carve-out for ``legacy`` only.  An operator rotating
      ``PERSATRIX_SESSION_ID`` between boots would otherwise recompute an
      identical digest, skip the derivation, and never be able to read the
      row the earlier session wrote.
    * ``protection_level`` — the RFC 0037 §C capture the row is stamped
      with, gating readability at the §D wall.  Handed in by the close
      path rather than derived here, so the value in the digest is
      provably the value in the row: one ``normalize_for_stamp`` call
      feeds both.
    """
    if not message_ids or not all(message_ids):
        logger.warning(
            "ISSUE-0130: replayed span for agent=%s scope=%s has %d of %d "
            "turns without a wire message id; deriving without the "
            "re-derivation guard",
            agent_id, interaction.scope,
            sum(1 for m in message_ids if not m), len(message_ids),
        )
        return None
    digest = hashlib.sha256(_join_components([
        agent_id,
        epoch_id,
        interaction.session_id,
        protection_level,
        interaction.principal_id,
        interaction.speaker_id,
        interaction.scope,
        *message_ids,
    ])).hexdigest()
    return f"{REPLAY_INTERACTION_ID_PREFIX}{digest}"


def _join_components(parts: list[str]) -> bytes:
    """Encode ``parts`` so distinct component lists give distinct bytes.

    LENGTH-PREFIXED, not delimiter-joined (PR B2 review).  The first cut
    joined on ``\x1f``, which is only injective if no component can contain
    that byte — and ``message_id`` can: ``validate_channel_message_dict``
    type- and length-checks ``msg["id"]`` but applies no character class,
    unlike ``sender_id``'s ``^[a-z0-9][a-z0-9-]*[a-z0-9]$``.  A single row
    whose id is ``"m1\x1fm2"`` therefore produced the same digest as a
    two-message span ``["m1", "m2"]``, and the second span was reported
    "already derived": its content never summarised, never extracted, never
    retried, because the record is popped by then.  That is the losing
    direction this module refuses everywhere else.

    The component count is variable (``message_ids`` is splatted last), so
    the encoding has to be self-delimiting rather than merely escaped.
    ``<byte-length>:<utf-8 bytes>`` per component is, and it costs nothing:
    no component can forge a boundary because the length is read before the
    bytes.
    """
    out = bytearray()
    for part in parts:
        raw = part.encode("utf-8")
        out += str(len(raw)).encode("ascii")
        out += b":"
        out += raw
    return bytes(out)


async def replay_span_already_derived(
    *,
    episodic: EpisodicMemory,
    interaction: Interaction,
    agent_id: str,
    protection_level: str,
    message_ids: list[str],
) -> bool:
    """Give ``interaction`` its boot-stable id; report if it is a repeat.

    ``True`` means an earlier boot already derived exactly this span and
    the caller must not derive it again.

    **This REPLACES ``interaction.interaction_id``**, and the replacement
    is the mechanism rather than a side effect: the id is what gets
    written to ``episodes.interaction_id``, which is the only column the
    lookup can match on without a migration, and it must be the same
    value the next boot computes.  A ``uuid4`` minted at open cannot be.
    Everything downstream reads the id off the interaction — Phase 1's
    row, Phase 2's ``update_episode_summary`` match, and the
    ``source_interaction_id`` on every extracted fact — so replacing it
    here, before Phase 1, keeps all three on one value.  Nothing
    upstream has recorded the old one: a replayed span parks no vote and
    draws no wallet lease (both are live-close concerns), and the two
    doors that close one — the pass-end sweep and the replay→live split
    — hand it straight here.

    Failures are non-fatal in the DERIVE direction.  An unidentifiable
    span (:func:`replay_span_identity` returns ``None``) and a lookup
    that raises both fall through to deriving, because the guard bounds
    duplication while skipping on a transient read error would lose the
    span's memory outright — and the close path has exactly one attempt
    at it.

    A lookup that raises additionally leaves the ``uuid4`` in place
    rather than claiming the digest.  Deriving twice is the accepted
    residual; deriving twice UNDER ONE ID is not, because
    ``interaction_id`` stopped being collision-free the moment it became
    content-derived, and ``update_episode_summary`` matches
    ``WHERE agent_id = ? AND interaction_id = ? AND summary = ?`` with no
    ``LIMIT`` — two rows sharing a digest would have Phase 2 rewrite both
    and mis-report ``rowcount``.  The uuid4 keeps the failed attempt in
    its own namespace; the next boot computes the digest cleanly.
    """
    # The record's OWN epoch, frozen at open, NOT the ambient one: the
    # close path binds exactly this value around ``store_episode``
    # (``close_path._record_write_scopes``), so the digest and the row it
    # guards agree however this close was reached.  A blank means the
    # record was minted by a site that captures no epoch, and both this
    # and the write fall back to the ambient read together.
    identity = replay_span_identity(
        interaction, agent_id,
        interaction.epoch_id or episodic.active_epoch_id(),
        protection_level,
        message_ids,
    )
    if identity is None:
        return False
    try:
        already = await episodic.has_episode_for_interaction(identity)
    except Exception:
        logger.warning(
            "ISSUE-0130: re-derivation guard failed for agent=%s scope=%s "
            "interaction_id=%s; deriving under the boot-local id (the guard "
            "bounds duplication, it must not cost a span its memory)",
            agent_id, interaction.scope, identity, exc_info=True,
        )
        return False
    interaction.interaction_id = identity
    if already:
        logger.info(
            "ISSUE-0130: replayed span already derived — skipping "
            "re-derivation (agent=%s scope=%s principal=%s speaker=%s "
            "turns=%d interaction_id=%s)",
            agent_id, interaction.scope, interaction.principal_id,
            interaction.speaker_id, len(message_ids), identity,
        )
    return already
