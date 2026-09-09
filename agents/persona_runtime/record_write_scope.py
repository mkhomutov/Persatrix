"""Bind the write axes a closing record owns, not the closer's.

Split out of :mod:`agents.persona_runtime.close_path` (v0.3.15 PR B2
review round 3) when that module crossed the 500-line cap
``scripts/checks/file_size.py --strict`` enforces — but the seam is worth
having on its own terms.

A persona resolves its storage axes AMBIENT, per request. A record is
routinely closed from inside a DIFFERENT request's scope: the room-wide
fans, ``idle_check``, and the catch-up split all fire from whichever event
happened to trigger them. Every axis that is both ambient-resolved and
frozen on the record therefore has to be re-bound here, or the closer's
value is stamped on the derived rows — and every one of these axes is
filtered on read with strict equality and no carve-out, so a mis-stamp is
not a cosmetic error but a row its own reader can never see.

That list has grown once per release, and each time it was found by a bug
rather than by looking: the principal in PR #846, the epoch in this one.
Naming the set in one module is the cheapest way to make the next addition
an edit to a list instead of a fourth incident.

Since ISSUE-0137 the tenant also travels by the call for TWO of those
tiers: ``store_episode`` and ``FactStore.store`` take ``principal_id``
and the close path passes the record's own, the way ``speaker_id``
always has.  Read the scope of that narrowly — this wrapper is still
what holds the invariant everywhere else, and a derived-write path that
forgets it still produces rows their own reader cannot see:

* the EPOCH has no such parameter on any tier, and its recall predicate
  is the same strict equality, so forgetting the wrapper still hides the
  conversation from the epoch that produced it;
* the RELATIONSHIP tier has no tenant argument at all
  (:mod:`agents.memory.relationship` resolves ambient at every write),
  and the close path writes it in Phase 2.

So the argument is the contract for the two tiers that take it, this is
the net under everything else, and removing the principal half would
silently re-expose every tier the close path cannot pass it to.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from ..epoch_id import epoch_scope
from ..principal_id import principal_scope

if TYPE_CHECKING:
    from ..memory.interactions import Interaction

__all__ = ["record_write_scopes"]


def record_write_scopes(
    interaction: Interaction,
) -> contextlib.AbstractContextManager[object]:
    """Bind the record's OWN tenant AND epoch for its whole derivation.

    Both storage axes are resolved AMBIENT by every tier, and both are
    frozen on the record at open, so both have to be re-bound here — the
    room-wide fans, ``idle_check`` and (since v0.3.15 PR B2) the catch-up
    split all close a record from inside whichever request happened to
    trigger them, which is a DIFFERENT request's scope.

    The principal half shipped with PR #846; the epoch half is the PR B2
    review's, and it was reachable the moment the catch-up split became
    symmetric: a replayed event carries no epoch key, so it force-closed a
    live record with only the persona's world epoch bound and
    ``store_episode`` stamped the row with that instead of the epoch the
    live conversation was opened under.  Epoch recall filters with strict
    equality and no carve-out, so the speaker's own conversation becomes
    permanently unreadable from the epoch that produced it.

    A blank ``epoch_id`` means the record was minted by a site that
    captures none (a direct ``Interaction(...)``), and resolution stays
    exactly where it was — ambient — so no pre-existing path changes.

    Both halves are still bound here after ISSUE-0137 gave the principal
    an explicit parameter on two tiers — the module docstring lists what
    still rides this binding alone.
    """
    stack = contextlib.ExitStack()
    stack.enter_context(principal_scope(interaction.principal_id))
    if interaction.epoch_id:
        stack.enter_context(epoch_scope(interaction.epoch_id))
    return stack
