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
