"""Strict-equality epoch recall predicate (ISSUE-0085 PR 3).

The epoch-axis sibling of :mod:`agents.memory._principal_filter` (and,
structurally, *not* :mod:`agents.memory._session_filter`).  Single source
of truth for the ``epoch_id`` predicate shared by the persona-memory
tiers (``episodes`` / ``relationships`` / ``facts`` / ``notes`` /
``interactions``), so a future refactor cannot drift the run-isolation
filter across tiers.

Two functions:

* :func:`resolve_active_epoch` — apply the call-time precedence
  (task-local ``epoch_scope`` → tier construction snapshot).  Call once
  at the tier's public-API boundary; pass the resolved id down to the
  recall helpers *and* use it on the write path so a row is tagged with
  the same epoch its reader will filter on.
* :func:`epoch_eq_clause` — given a resolved epoch and a column
  reference, return the ``" AND col = ?"`` SQL fragment + params.

Why this follows :mod:`_principal_filter`, **not** :mod:`_session_filter`:

* The session helper returns ``None`` for the ``"*"`` sentinel (no
  filter — CLI/debug only) and always unions :data:`LEGACY_SESSION_ID`
  so pre-RFC rows stay visible from every session — the carve-out exists
  *for* continuity.
* The epoch helper has **no** ``"*"`` bypass and **no** carve-out: the
  predicate is unconditional strict equality.  A row written by one
  run/test epoch must never be visible to another, so there is
  deliberately no way to spell "all epochs" here — that is the whole
  point of run isolation (ISSUE-0085; the structural half of the F-3
  fix the scope-axes reframing moves off the session axis).  An admin /
  debug cross-epoch view, if ever needed, must be an explicit
  out-of-band query — never this default recall path.

:data:`agents.epoch_id.DEFAULT_EPOCH_ID` (``"live"``) is *not* a
carve-out: it is the epoch every production / untagged deployment uses,
and the ``DEFAULT 'live'`` value migration v12 backfills onto
pre-existing rows.  Production never changes it (behaviour unchanged);
once a second epoch exists, ``live`` rows are visible only to ``live``.

Internal helper (leading-underscore module name); callers inside
:mod:`agents.memory` import the public names directly.
"""

from __future__ import annotations

from ..epoch_id import current_epoch_id

__all__ = [
    "epoch_eq_clause",
    "resolve_active_epoch",
]


def resolve_active_epoch(snapshot: str) -> str:
    """Resolve the active epoch for the current call.

    Precedence: a per-request :func:`~agents.epoch_id.epoch_scope`
    (task-local ContextVar) wins; otherwise the tier's construction-time
    ``snapshot`` (seeded from :envvar:`PERSATRIX_EPOCH`) is used.  When no
    scope is active the snapshot is returned verbatim, so the single-world
    CLI / test / boot paths are unchanged.

    The same single seam every tier uses on both its recall and write
    paths — recall filters on the resolved epoch and writes tag the row
    with it, so a row is always readable by the epoch that wrote it.
    """
    return current_epoch_id() or snapshot


def epoch_eq_clause(
    epoch_id: str,
    *,
    column: str,
) -> tuple[str, list[str]]:
    """Build the ``" AND col = ?"`` fragment + ``[epoch_id]`` params.

    Always returns a non-empty fragment: the epoch predicate is
    unconditional (no ``"*"`` bypass, no carve-out).

    SECURITY: ``column`` is interpolated directly into the returned SQL
    via f-string — it must be a **trusted internal literal** (a column or
    qualified ``alias.column`` reference fixed at the call site), never
    user input.  Identical contract to
    :func:`agents.memory._principal_filter.principal_eq_clause`; every
    in-tree caller passes a string constant.
    """
    return f" AND {column} = ?", [epoch_id]
