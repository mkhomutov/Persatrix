"""
Interaction lifecycle façade — RFC 0020 §C/D Phase 1 (in-memory).

Tracks open interactions per RFC 0020 §G key, accumulates turns, and
closes them when a :class:`BoundaryDetector` fires.  Since the v0.3.15
residuals PR 3 (ISSUE-0123 R-1 + ISSUE-0131) the tracker key is the
tuple ``(principal_id, speaker_id, scope)`` — see
:mod:`agents.memory.interaction_tracker` for the key contract, the
room-wide close fan, and the counter-shape consequence.

The tracker is process-local: each agent instance owns its own
:class:`InteractionTracker`.  RFC 0020 §C "Restart behavior" pins the
in-memory-only contract — open interactions are deliberately not
persisted; pre-restart turns are recoverable from message storage but
are not auto-summarised.

Module split history (each move keeps this module the public façade,
re-exporting every name so existing imports keep working):

* :mod:`agents.memory.scopes` — scope vocabulary helpers
  (``scope_for_dm`` / ``scope_for_thread`` / ``scope_for_group`` /
  ``scope_for_channel_event``) — PR-262 follow-up.
* :mod:`agents.memory.interaction_janitor` — closing-state janitor
  (``cleanup_closing_interactions``) and the summary-text sentinels
  (``SUMMARY_PENDING_TEXT`` / ``SUMMARY_UNAVAILABLE_TEXT``).
* :mod:`agents.memory.interaction_types` — ``Turn`` / ``Interaction``
  (RFC 0052 PR 4b-ii split).
* :mod:`agents.memory.interaction_tracker` +
  :mod:`agents.memory.interaction_metrics` — the tracker itself and its
  counter emission (v0.3.15 residuals PR 3 split: the
  ``(principal, speaker, scope)`` re-key pushed the combined module
  past the 500-line cap enforced by
  ``scripts/checks/file_size.py --strict``).
* :mod:`agents.memory.interaction_key` — the record key itself: the
  three axes and the resolution rules the tracker's entry points share
  (PR #846 review, when the tracker returned to the cap).  Like
  :mod:`~agents.memory.interaction_metrics` and unlike the modules
  above it, this one is NOT re-exported here: its names never were
  importable from this façade, so re-exporting would widen the public
  surface rather than preserve it, and a caller reaching for the key
  wants the module that documents it.

The ``REASON_*`` constants and :data:`CloseReason` are re-exported from
:mod:`agents.memory.boundary_detectors` for the same reason — several
call sites import them from here.
"""

from __future__ import annotations

from .boundary_detectors import (
    DEFAULT_IDLE_TIMEOUT_SEC,
    REASON_CATCHUP_COMPLETE,
    REASON_COST,
    REASON_IDLE_GAP,
    REASON_MAX_TURNS,
    REASON_SHUTDOWN,
    REASON_STRUCTURAL,
    REASON_TOPIC_SHIFT,
    BoundaryDetector,
    CloseReason,
    MaxTurnsDetector,
    default_detectors,
)
from .interaction_janitor import (
    SUMMARY_PENDING_TEXT,
    SUMMARY_UNAVAILABLE_TEXT,
    cleanup_closing_interactions,
)
from .interaction_tracker import Clock, InteractionTracker
from .interaction_types import Interaction, Turn
from .scopes import (
    SCOPE_TICK,
    is_group_scope,
    is_thread_scope,
    scope_for_channel_event,
    scope_for_dm,
    scope_for_group,
    scope_for_thread,
)

__all__ = [
    "DEFAULT_IDLE_TIMEOUT_SEC",
    "REASON_CATCHUP_COMPLETE",
    "REASON_COST",
    "REASON_IDLE_GAP",
    "REASON_MAX_TURNS",
    "REASON_SHUTDOWN",
    "REASON_STRUCTURAL",
    "REASON_TOPIC_SHIFT",
    "SCOPE_TICK",
    "SUMMARY_PENDING_TEXT",
    "SUMMARY_UNAVAILABLE_TEXT",
    "BoundaryDetector",
    "Clock",
    "CloseReason",
    "Interaction",
    "InteractionTracker",
    "MaxTurnsDetector",
    "Turn",
    "cleanup_closing_interactions",
    "default_detectors",
    "is_group_scope",
    "is_thread_scope",
    "scope_for_channel_event",
    "scope_for_dm",
    "scope_for_group",
    "scope_for_thread",
]
