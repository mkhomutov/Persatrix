"""The interaction **record key** — ``(principal_id, speaker_id, scope)``.

Split out of :mod:`agents.memory.interaction_tracker` (PR #846 review):
that module was created BY a 500-line-cap overflow and had returned to
exactly 500, so the next correction to it was already being compressed
to fit rather than written.  The key is the seam that separates
cleanest — it is a named concept in its own right (``docs/ai-glossary.md``,
"Record key"), it carries resolution rules that are not obvious from the
tracker's control flow, and it is what the residuals PR 4 ``speaker_id``
projection has to reason about.  The tracker keeps the lifecycle; this
module owns what a record is filed under.

Decided on live evidence (ISSUE-0123 R-1 + ISSUE-0131, Phase 0/0b — see
``docs/issues/ISSUE-0082-residuals-phase0-gate.md``):

* ``scope`` — the RFC 0020 §G room/thread/DM unit, unchanged.  It stays
  the persisted ``episodes.scope`` string and the prefix-predicate /
  ``idx_episodes_scope`` surface; the two other axes are NOT encoded
  into it (each has its own column since migrations v11 and 18).
* ``principal_id`` — the tenant the turn ran under, resolved from the
  ambient :func:`~agents.principal_id.principal_scope` (else the
  env/default).  A group room no longer accumulates every authenticated
  person's turns into one record that closes under whichever principal
  happened to trigger the close, and the close path binds this value
  back around the derivation so the rows agree with the record
  (``close_path.persist_closed_interaction``).
* ``speaker_id`` — the event's ``sender_id``.  The principal is a
  *tenant* axis and only authenticated humans have one, so a room full
  of personas shares the ``local`` principal; the speaker half is what
  keeps agent A's turns out of the record B's restatement is derived
  from (the Phase 0b misattribution).  ``""`` = no speaker (tick /
  single-turn scopes whose event carries none).

All three are frozen onto the :class:`~agents.memory.interaction_types
.Interaction` at open (the ``session_id`` footing) — they ARE the key,
so a later turn under a different principal or speaker lands in a
different record.  The DM topology already answered this design:
``scope_for_dm`` keys on the sender, so a DM record has always been
per-speaker; the tuple key makes group and thread scopes consistent
with it.

The two functions here are the only two directions the key is ever
built from: :func:`resolve_record_key` from a CALL's arguments (which
may omit axes), and :func:`record_key` from an already-open RECORD
(which cannot).  Keeping both here is what makes them provably agree —
a record must be findable under the key its own fields produce, or
:meth:`~agents.memory.interaction_tracker.InteractionTracker.close_record`
silently no-ops and the record leaks open.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..principal_id import normalize_principal_id, resolve_principal_id_silent

if TYPE_CHECKING:
    from .interaction_types import Interaction

__all__ = ["RecordKey", "record_key", "resolve_record_key"]


#: What an open interaction is filed under: ``(principal, speaker, scope)``.
RecordKey = tuple[str, str, str]


def record_key(interaction: Interaction) -> RecordKey:
    """The key an already-open ``interaction`` is registered under.

    Stable for the record's lifetime, the three components being frozen
    at open — so this is a pure read, never a re-resolution.  That is
    load-bearing for ``close_record``'s identity guard: it looks the
    record up under this key and closes only when the map still holds
    THAT object, which is sound precisely because the key cannot have
    moved under it.
    """
    return (interaction.principal_id, interaction.speaker_id, interaction.scope)


def resolve_record_key(
    scope: str, principal_id: str | None, speaker_id: str | None,
) -> RecordKey:
    """Resolve the key for a CALL, which may omit either new axis.

    ``principal_id=None`` means AMBIENT (never "default"): the
    task-local scope wins, then the env var, then ``local`` — the same
    precedence the storage tiers resolve writes under, so the record a
    turn lands in and the tenant its close-derived rows bind cannot
    disagree.  An explicit value goes through
    :func:`~agents.principal_id.normalize_principal_id`, so ``""``
    cannot mint a key that no recall predicate would ever match.

    ``speaker_id=None`` or blank is ``""`` — the no-speaker convention,
    not a missing value: a tick and a single-turn scope genuinely have
    no speaker, and collapsing them to one key per scope is the correct
    pre-v0.3.15 shape rather than a degradation.

    Uniform across every entry point that takes the axes
    (``get`` / ``start`` / ``add_turn``), so a legacy ``get(scope)``
    still finds the record a legacy ``add_turn(scope)`` opened.

    Idempotent by contract, and the tracker depends on it: ``add_turn``
    resolves the key, then hands the halves to ``start``, which resolves
    again.  Both normalisers are idempotent today (strip-then-default);
    a future normalisation step that is not — a prefix, a case fold, a
    tenant-alias lookup — would make the lookup key and the storage key
    diverge, opening a fresh record per turn with no failing test,
    since the caller still gets a record back.
    """
    principal = (
        normalize_principal_id(principal_id)
        if principal_id is not None
        else resolve_principal_id_silent()
    )
    return (principal, (speaker_id or "").strip(), scope)
