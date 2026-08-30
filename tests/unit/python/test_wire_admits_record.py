"""``wire_admits_record`` — the room fans' shared wire-id conjunct.

PR #846 review (finding: fan altitude).  The admission rule used to be
written three times, in ``close_notification``, ``cost_close`` and
``vote_close``, and the copies had drifted into TWO behaviours on a
blank anchor with only one of them saying so.  The rule now has one
spelling, and the divergence is a named keyword rather than a silent
difference between three loop conditions — so these pins cover both
callers' semantics in one place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.memory.interactions import Interaction
from agents.persona_runtime.interaction_boundary import wire_admits_record


def _record(wire_id: str = "") -> Interaction:
    return Interaction(
        interaction_id="i-1", scope="group:planning", started_at=1_000.0,
        wire_interaction_id=wire_id,
    )


class TestUnstampedRecord:
    """The tolerant-wire-reader posture: a record that carries no id
    cannot participate in the rule, so it is never excluded by it."""

    def test_unstamped_record_is_admitted_under_any_anchor(self):
        assert wire_admits_record(_record(), "wire-A") is True

    def test_unstamped_record_is_admitted_under_a_blank_anchor(self):
        assert wire_admits_record(_record(), "") is True

    def test_unstamped_record_is_admitted_even_by_the_strict_caller(self):
        assert wire_admits_record(
            _record(), "wire-A", tolerate_blank_anchor=False,
        ) is True


class TestStampedRecord:
    def test_matching_anchor_admits(self):
        assert wire_admits_record(_record("wire-A"), "wire-A") is True

    def test_differing_anchor_skips(self):
        """The successor defence: a record stamped with a conversation
        the trigger predates must not be buried as 'ended', and its
        metered summary must not be billed to the predecessor's
        reserve."""
        assert wire_admits_record(_record("wire-B"), "wire-A") is False

    def test_differing_anchor_skips_for_the_strict_caller_too(self):
        assert wire_admits_record(
            _record("wire-B"), "wire-A", tolerate_blank_anchor=False,
        ) is False


class TestBlankAnchorDivergence:
    """The whole reason the keyword exists — the two callers genuinely
    disagree here, and the disagreement is deliberate."""

    def test_tolerant_caller_admits_a_stamped_record(self):
        """``close_notification`` / ``cost_close``: a blank anchor means
        the traffic is wire-untracked (thread scope, tick, an old
        producer), where the pre-wire scope-keyed behaviour is right."""
        assert wire_admits_record(_record("wire-A"), "") is True

    def test_strict_caller_skips_a_stamped_record(self):
        """``vote_close``: a vote parked on an unstamped record judged an
        unstamped conversation, so it must not reach across and close
        records that DO name one."""
        assert wire_admits_record(
            _record("wire-A"), "", tolerate_blank_anchor=False,
        ) is False

    def test_both_callers_agree_when_neither_side_is_stamped(self):
        assert wire_admits_record(_record(), "") is True
        assert wire_admits_record(
            _record(), "", tolerate_blank_anchor=False,
        ) is True


def test_replay_flag_is_not_this_predicate_s_business():
    """The ``replayed`` exclusion belongs to every fan unconditionally
    and is a property of the record, not of the wire — so it lives in
    ``InteractionTracker.close_scope``, not here.  Pinned so a future
    edit does not quietly fold it in and leave ``close_scope``'s copy
    as the only guard for the session-end fan."""
    replayed = _record("wire-A")
    replayed.replayed = True
    assert wire_admits_record(replayed, "wire-A") is True


# The three room-close fans, which must take the SCOPE's anchor rather
# than reading the wire id raw.
_FAN_MODULES = (
    Path("agents/persona_runtime/episode_routing.py"),
    Path("agents/persona_runtime/cost_close.py"),
    Path("agents/persona_runtime/close_notification.py"),
)


@pytest.mark.parametrize("py_path", _FAN_MODULES, ids=lambda p: p.stem)
def test_room_close_fans_delegate_to_the_shared_anchor(py_path: Path) -> None:
    """PR #846 review: ``scope_wire_anchor`` computes this predicate's
    second argument, and the two must not drift apart again.

    The derivation — the raw wire-id read with the thread carve-out
    applied, since a threaded reply carries the parent FLOOR's id and
    that id says nothing about the thread (RFC 0030 IP3) — was spelled
    inline at all three fan sites, and the close-notification copy had
    silently dropped the carve-out.  That stamped the floor's id onto
    thread records through the fan's wire-id backfill, wrote it as the
    thread episode's ``governance_interaction_id`` (contradicting the
    close path's own DM/thread/non-channel → NULL contract) and billed
    the OQ #6 metered close summary against the floor's conversation.

    Structural rather than behavioural on purpose: a fourth fan, or a
    revert of one of the three, reintroduces the bug by *omission*, which
    no per-call assertion catches.
    """
    source = py_path.read_text(encoding="utf-8")
    assert "scope_wire_anchor(scope, event)" in source, (
        f"{py_path} must take its wire anchor from "
        f"`scope_wire_anchor(scope, event)`"
    )
    assert "wire_interaction_id(event)" not in source, (
        f"{py_path} reads the wire interaction id RAW. A room-close fan "
        f"must take `scope_wire_anchor(scope, event)` — reading raw drops "
        f"the thread carve-out, stamping a thread record with the parent "
        f"floor's id and billing its metered close summary to the floor."
    )
