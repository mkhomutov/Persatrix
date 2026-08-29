"""v0.3.15 residuals PR 3 — the ``(principal, speaker, scope)`` tracker key.

The record-shape decision the ISSUE-0082 Phase 0 gate resolved on live
evidence (principal 2026-08-07, speaker/Phase 0b 2026-08-21 —
``docs/issues/ISSUE-0082-residuals-phase0-gate.md``): the
:class:`~agents.memory.interaction_tracker.InteractionTracker` keys open
records by principal AND speaker AND scope, so a group room no longer
aggregates every participant's turns into one record that closes under
one tenant (ISSUE-0123 R-1) or one anonymous bucket (ISSUE-0131).

The suite pins, in the plan's own order (residuals PR plan, PR 3 tests):

* two principals, one room scope → two records;
* one ``local`` principal, THREE agent speakers, one room scope →
  three records — the Phase 0b regression, the exact case plain
  Option A (``(principal, scope)``) would have shipped broken;
* a room-wide close closes all of them (the ISSUE-0123 part 3 fan);
* plus the key contracts the callers lean on: frozen-at-open,
  ambient-principal resolution, ``close_record`` identity, the
  ``append_turn`` no-cap ingest, and ``open_scopes`` as a projection.

The close-notification final-turn-of-each pin lives with the other
notification tests in ``test_interaction_close_notification.py``.
"""

from __future__ import annotations

from agents.memory.boundary_detectors import (
    REASON_STRUCTURAL,
    default_detectors,
)
from agents.memory.interaction_key import record_key, resolve_record_key
from agents.memory.interactions import InteractionTracker
from agents.principal_id import DEFAULT_PRINCIPAL_ID, principal_scope

_SCOPE = "group:planning"


class TestPrincipalAxis:
    """R-1: one record per tenant within a room scope."""

    def test_two_principals_one_scope_two_records(self):
        tracker = InteractionTracker()
        with principal_scope("alice-person"):
            a = tracker.add_turn(_SCOPE, speaker_id="alice")
        with principal_scope("bob-person"):
            b = tracker.add_turn(_SCOPE, speaker_id="bob")

        records = tracker.records_for_scope(_SCOPE)
        assert len(records) == 2
        assert a is not b
        assert a.principal_id == "alice-person"
        assert b.principal_id == "bob-person"
        assert a.scope == b.scope == _SCOPE

    def test_principal_resolves_ambient_at_each_turn(self):
        """A turn keys by ITS OWN request's principal — conversation A's
        record cannot absorb a turn that arrived under tenant B."""
        tracker = InteractionTracker()
        with principal_scope("alice-person"):
            first = tracker.add_turn(_SCOPE, speaker_id="dana")
        with principal_scope("bob-person"):
            second = tracker.add_turn(_SCOPE, speaker_id="dana")
        assert first is not second, (
            "same speaker under two tenants must not share a record"
        )
        assert first.turn_count == second.turn_count == 1

    def test_no_ambient_principal_collapses_to_local(self):
        """Unauthenticated / single-tenant traffic keys under the
        ``local`` default — byte-identical to pre-re-key behaviour on
        the principal axis (the Leg 8 acceptance line)."""
        tracker = InteractionTracker()
        record = tracker.add_turn(_SCOPE, speaker_id="iron-fox")
        assert record.principal_id == "local"


class TestSpeakerAxis:
    """ISSUE-0131 / Phase 0b: the axis the principal cannot carry."""

    def test_three_agent_speakers_one_local_principal_three_records(self):
        """THE Phase 0b regression: a room of personas shares the one
        ``local`` principal, so plain Option A would aggregate all
        three into a single record — the nova-sparrow misattribution.
        The speaker key splits them."""
        tracker = InteractionTracker()
        for speaker in ("iron-fox", "nova-sparrow", "ember-owl"):
            tracker.add_turn(_SCOPE, speaker_id=speaker)

        records = tracker.records_for_scope(_SCOPE)
        assert len(records) == 3
        assert {r.speaker_id for r in records} == {
            "iron-fox", "nova-sparrow", "ember-owl",
        }
        assert {r.principal_id for r in records} == {"local"}
        assert all(r.turn_count == 1 for r in records)

    def test_same_speaker_turns_aggregate(self):
        tracker = InteractionTracker()
        for _ in range(3):
            record = tracker.add_turn(_SCOPE, speaker_id="iron-fox")
        assert record.turn_count == 3
        assert len(tracker.records_for_scope(_SCOPE)) == 1

    def test_speaker_and_principal_frozen_at_open(self):
        """The key halves are the ``session_id`` footing: set when the
        record opens, immutable for its lifetime — trivially, because a
        different pair IS a different key."""
        tracker = InteractionTracker()
        with principal_scope("alice-person"):
            record = tracker.add_turn(_SCOPE, speaker_id="alice")
        assert (record.principal_id, record.speaker_id) == (
            "alice-person", "alice",
        )
        # Later turns under other keys leave it untouched.
        with principal_scope("bob-person"):
            tracker.add_turn(_SCOPE, speaker_id="bob")
        assert (record.principal_id, record.speaker_id) == (
            "alice-person", "alice",
        )

    def test_senderless_turn_uses_the_no_speaker_key(self):
        """Tick / senderless single-turn scopes coerce to ``""`` — in a
        single-tenant deployment that is one key per scope, the exact
        pre-re-key shape (the compat contract the 38 pre-existing
        tracker tests exercise)."""
        tracker = InteractionTracker()
        a = tracker.add_turn("tick")
        b = tracker.add_turn("tick", speaker_id=None)
        c = tracker.add_turn("tick", speaker_id="  ")
        assert a is b is c
        assert a.speaker_id == ""
        assert a.turn_count == 3


class TestRoomWideClose:
    """ISSUE-0123 part 3: room events fan; idle stays per record."""

    def test_close_scope_closes_every_record(self):
        tracker = InteractionTracker()
        with principal_scope("alice-person"):
            tracker.add_turn(_SCOPE, speaker_id="alice")
        for speaker in ("iron-fox", "nova-sparrow"):
            tracker.add_turn(_SCOPE, speaker_id=speaker)
        other = tracker.add_turn("group:standup", speaker_id="iron-fox")

        closed = tracker.close_scope(_SCOPE, reason=REASON_STRUCTURAL)

        assert len(closed) == 3
        assert all(not r.is_open for r in closed)
        assert all(r.close_reason == REASON_STRUCTURAL for r in closed)
        assert tracker.records_for_scope(_SCOPE) == []
        # The fan is scoped: a sibling room's record is untouched.
        assert other.is_open
        assert tracker.records_for_scope("group:standup") == [other]

    def test_close_targets_one_key(self):
        """The keyed ``close`` stays a ONE-record close — the fan is a
        separate, deliberate call (room events must opt in)."""
        tracker = InteractionTracker()
        iron_fox = tracker.add_turn(_SCOPE, speaker_id="iron-fox")
        tracker.add_turn(_SCOPE, speaker_id="ember-owl")

        closed = tracker.close_record(iron_fox, reason=REASON_STRUCTURAL)

        assert closed is not None and closed.speaker_id == "iron-fox"
        survivors = tracker.records_for_scope(_SCOPE)
        assert [r.speaker_id for r in survivors] == ["ember-owl"]

    def test_close_record_is_identity_guarded(self):
        """A held reference to a record that already closed (and whose
        key a successor now occupies) closes NOTHING — the concurrent
        close told the truth; the successor is a different interaction."""
        tracker = InteractionTracker()
        first = tracker.add_turn(_SCOPE, speaker_id="iron-fox")
        assert tracker.close_record(first, reason=REASON_STRUCTURAL) is first
        successor = tracker.add_turn(_SCOPE, speaker_id="iron-fox")

        assert tracker.close_record(first, reason=REASON_STRUCTURAL) is None
        assert successor.is_open

    def test_close_scope_stamps_one_instant(self):
        """One room event, one timestamp (PR #846 review): ``close_scope``
        reads the clock ONCE, so every sibling's ``closed_at`` matches —
        whichever fan caller forgot to pre-read ``now``."""
        ticks = iter(range(1_000, 2_000))
        tracker = InteractionTracker(clock=lambda: float(next(ticks)))
        for speaker in ("iron-fox", "nova-sparrow", "ember-owl"):
            tracker.add_turn(_SCOPE, speaker_id=speaker)

        closed = tracker.close_scope(_SCOPE, reason=REASON_STRUCTURAL)

        assert len(closed) == 3
        assert len({r.closed_at for r in closed}) == 1, (
            "one room event, one close instant"
        )

    def test_idle_check_closes_per_record_not_per_room(self):
        """Idle is the one close that is NOT a room event: a speaker who
        went quiet idles out on their own last-turn timer while an
        active sibling record stays open."""
        clock_now = 1_000.0
        tracker = InteractionTracker(idle_timeout_sec=60.0)
        tracker.add_turn(_SCOPE, speaker_id="quiet-one", now=clock_now)
        tracker.add_turn(_SCOPE, speaker_id="chatty-one", now=clock_now)
        tracker.add_turn(_SCOPE, speaker_id="chatty-one", now=clock_now + 55.0)

        closed = tracker.idle_check(now=clock_now + 70.0)

        assert [r.speaker_id for r in closed] == ["quiet-one"]
        assert [
            r.speaker_id for r in tracker.records_for_scope(_SCOPE)
        ] == ["chatty-one"]


class TestAppendTurnAndProjections:
    """The fan's ingest half and the audited scope-level reads."""

    def test_append_turn_lands_on_the_given_record_only(self):
        tracker = InteractionTracker()
        a = tracker.add_turn(_SCOPE, speaker_id="iron-fox")
        b = tracker.add_turn(_SCOPE, speaker_id="ember-owl")

        tracker.append_turn(a, {"summary": "closing message"})

        assert a.turn_count == 2
        assert a.turns[-1].payload == {"summary": "closing message"}
        assert b.turn_count == 1

    def test_append_turn_does_not_enforce_the_cap(self):
        """The one deliberate cap exemption: the only caller closes the
        record in the same step, and the room close's truthful trigger
        outranks the cap label (``append_turn``'s contract).

        ``default_detectors(max_turns=2)`` — never an APPENDED second
        ``MaxTurnsDetector``: the constructor caches the FIRST one in the
        chain, so the appended shape left the default cap bound and this
        pin vacuous (PR #846 review)."""
        tracker = InteractionTracker(detectors=default_detectors(max_turns=2))
        assert tracker._max_turns == 2, "the low cap actually bound"
        record = tracker.add_turn(_SCOPE, speaker_id="iron-fox")
        tracker.append_turn(record, {"summary": "final"})
        assert record.turn_count == 2
        assert record.is_open, "append_turn never inline-closes"

    def test_append_turn_noops_on_a_closed_record(self):
        tracker = InteractionTracker()
        record = tracker.add_turn(_SCOPE, speaker_id="iron-fox")
        tracker.close_record(record, reason=REASON_STRUCTURAL)
        tracker.append_turn(record, {"summary": "late"})
        assert record.turn_count == 1

    def test_open_scopes_is_a_deduplicated_projection(self):
        """The audited ``open_scopes`` contract: scope-level view, one
        entry per scope however many records it holds, insertion order."""
        tracker = InteractionTracker()
        tracker.add_turn(_SCOPE, speaker_id="iron-fox")
        tracker.add_turn("group:standup", speaker_id="iron-fox")
        tracker.add_turn(_SCOPE, speaker_id="ember-owl")

        assert tracker.open_scopes() == [_SCOPE, "group:standup"]
        assert len(tracker.open_records()) == 3


class TestAdmittedRecords:
    """``admitted_records`` — the fan's eligibility seam (PR #846 review).

    The replay exclusion used to be spelled once in ``close_scope`` and
    again in ``close_notification``, which cannot use ``close_scope``
    because it needs the admitted set BEFORE any close.  Both now read
    the rule from here, so these pins cover both fans.
    """

    def test_replay_opened_records_are_excluded(self):
        """A replayed span belongs to the pass-end
        ``REASON_CATCHUP_COMPLETE`` sweep, never a live room cause."""
        tracker = InteractionTracker()
        live = tracker.add_turn(_SCOPE, speaker_id="iron-fox")
        tracker.add_turn(_SCOPE, speaker_id="ember-owl", replayed=True)

        assert tracker.admitted_records(_SCOPE) == [live]
        assert len(tracker.records_for_scope(_SCOPE)) == 2, (
            "the replayed record is still OPEN — excluded from the fan, "
            "not closed by it"
        )

    def test_admit_predicate_narrows_further(self):
        tracker = InteractionTracker()
        iron_fox = tracker.add_turn(_SCOPE, speaker_id="iron-fox")
        tracker.add_turn(_SCOPE, speaker_id="ember-owl")

        admitted = tracker.admitted_records(
            _SCOPE, admit=lambda r: r.speaker_id == "iron-fox",
        )
        assert admitted == [iron_fox]

    def test_other_scopes_are_never_admitted(self):
        tracker = InteractionTracker()
        here = tracker.add_turn(_SCOPE, speaker_id="iron-fox")
        tracker.add_turn("group:standup", speaker_id="iron-fox")

        assert tracker.admitted_records(_SCOPE) == [here]

    def test_close_scope_is_admitted_records_plus_the_close(self):
        """The delegation that keeps the rule single-owner: whatever
        ``admitted_records`` returns is exactly what the fan closes."""
        tracker = InteractionTracker()
        tracker.add_turn(_SCOPE, speaker_id="iron-fox")
        replayed = tracker.add_turn(
            _SCOPE, speaker_id="ember-owl", replayed=True,
        )
        expected = tracker.admitted_records(_SCOPE)

        closed = tracker.close_scope(_SCOPE, reason=REASON_STRUCTURAL)

        assert closed == expected
        assert replayed.is_open


class TestKeyResolutionContract:
    """The rules :mod:`agents.memory.interaction_key` owns since the
    PR #846 review split, exercised at the seam rather than only
    through the tracker's behaviour."""

    def test_a_records_own_fields_reproduce_the_key_it_is_filed_under(self):
        """The two build directions must agree, or ``close_record``'s
        identity guard looks under a key the map never used and silently
        no-ops — the record then leaks open with no sweep left to find
        it.  ``resolve_record_key`` builds from a CALL, ``record_key``
        from the RECORD; this is the only test that pins them together."""
        tracker = InteractionTracker()
        record = tracker.add_turn(
            _SCOPE, principal_id="acme", speaker_id="iron-fox",
        )

        assert record_key(record) == resolve_record_key(
            _SCOPE, "acme", "iron-fox",
        )
        assert tracker.close_record(record, reason=REASON_STRUCTURAL) is record

    def test_resolution_is_idempotent(self):
        """``add_turn`` resolves the key and then hands the halves to
        ``start``, which resolves AGAIN — so a normalisation step that is
        not idempotent (a prefix, a case fold, a tenant-alias lookup)
        would make the lookup key and the storage key diverge.  Every
        turn would then open a fresh record while still returning one to
        the caller, so nothing else in the suite would fail."""
        key = resolve_record_key(_SCOPE, "  acme  ", "  iron-fox  ")

        assert resolve_record_key(_SCOPE, key[0], key[1]) == key

    def test_blank_axes_collapse_to_the_pre_re_key_shape(self):
        """``None``/blank speaker is the no-speaker CONVENTION, not a
        missing value, and a blank principal cannot mint a key no recall
        predicate would match."""
        assert resolve_record_key(_SCOPE, None, None)[1] == ""
        assert resolve_record_key(_SCOPE, None, "   ")[1] == ""
        assert resolve_record_key(_SCOPE, "   ", None)[0] == DEFAULT_PRINCIPAL_ID

    def test_omitted_principal_is_ambient_not_the_default(self):
        """AMBIENT, never "default": the task-local scope wins, so the
        record a turn lands in and the tenant its close-derived rows bind
        cannot disagree."""
        with principal_scope("acme"):
            assert resolve_record_key(_SCOPE, None, "iron-fox")[0] == "acme"
        assert resolve_record_key(_SCOPE, None, "iron-fox")[0] == (
            DEFAULT_PRINCIPAL_ID
        )
