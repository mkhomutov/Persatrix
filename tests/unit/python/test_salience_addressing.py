"""RFC 0030 Tier B (v0.3.8) — PR 3 natural-language addressing extractor.

The pure recipient-extraction signal (:func:`detect_nl_addressing` /
:class:`NLAddressing` in :mod:`agents.salience_addressing`). Its consumption —
the bid's bar shift + prompt nudge — is pinned in ``test_salience_bid.py``;
the end-to-end "draws primarily Iron Fox" story is in
``tests/integration/test_salience_nl_addressing.py``.

Conservative + high-precision by construction: a *clear* free-text invitation
of a named person ("let's hear from Iron Fox") classifies as ``self``/``other``
relative to the bidding persona; an ambiguous name or a pronoun ("what do you
all think?") classifies as *neither*, so it suppresses no one. Structured
``@``-mentions are Tier A's job, not this soft signal (TB4 / amendment OQ #2).
"""

from __future__ import annotations

from agents.salience_addressing import NLAddressing, detect_nl_addressing


class TestDetectNLAddressing:
    def test_invited_by_name_is_self(self):
        a = detect_nl_addressing(
            content="let's hear from Iron Fox on this", persona_name="Iron Fox",
        )
        assert a == NLAddressing(self_named=True, other_named=False)

    def test_someone_else_invited_is_other(self):
        a = detect_nl_addressing(
            content="let's hear from Iron Fox on this", persona_name="Ember Owl",
        )
        assert a == NLAddressing(self_named=False, other_named=True)

    def test_over_to_cue_is_self(self):
        a = detect_nl_addressing(content="over to Iron Fox", persona_name="Iron Fox")
        assert a.self_named is True

    def test_what_does_x_think_cue(self):
        assert detect_nl_addressing(
            content="what does Iron Fox think about Redis?", persona_name="Iron Fox",
        ).self_named is True
        assert detect_nl_addressing(
            content="what does Iron Fox think about Redis?", persona_name="Ember Owl",
        ).other_named is True

    def test_first_name_address_still_matches_self(self):
        """A single-token first-name invitation matches the full-name persona
        (subset match) so the addressee still hears it."""
        a = detect_nl_addressing(content="over to Fox on this", persona_name="Iron Fox")
        assert a.self_named is True

    def test_pronoun_is_not_a_name(self):
        """A second-person / group pronoun is not a named recipient — neither
        self nor other (no one is suppressed on an ambiguous address)."""
        a = detect_nl_addressing(
            content="what do you all think here?", persona_name="Ember Owl",
        )
        assert a == NLAddressing(self_named=False, other_named=False)

    def test_no_cue_is_neither(self):
        a = detect_nl_addressing(
            content="What database should we use for the cache?",
            persona_name="Ember Owl",
        )
        assert a == NLAddressing(self_named=False, other_named=False)

    def test_empty_inputs_are_neither(self):
        assert detect_nl_addressing(content="", persona_name="Ember Owl") == (
            NLAddressing(self_named=False, other_named=False)
        )
        assert detect_nl_addressing(content="over to Fox", persona_name="") == (
            NLAddressing(self_named=False, other_named=False)
        )

    def test_self_takes_precedence_when_both_named(self):
        """Being named is a speak signal: if the persona is invited alongside
        someone else, ``self`` wins so the persona is not suppressed."""
        a = detect_nl_addressing(
            content="let's hear from Iron Fox. over to Ember Owl too",
            persona_name="Ember Owl",
        )
        assert a.self_named is True


class TestCompoundInvitation:
    """A single cue can invite *several* people ("let's hear from Iron Fox and
    Ember Owl"). Every invitee must read as ``self``; only a genuinely un-named
    persona reads as ``other``. The first PR-3 cut captured only the first name
    in the list, so the second invitee was wrongly penalised as *other_named* —
    the opposite of the intent (an invitee biased toward silence)."""

    _LIST = "let's hear from Iron Fox and Ember Owl on this"

    def test_first_listed_invitee_is_self(self):
        a = detect_nl_addressing(content=self._LIST, persona_name="Iron Fox")
        assert a.self_named is True

    def test_second_listed_invitee_is_self_not_other_only(self):
        """The regression guard for finding #2: the second name in an
        ``A and B`` invitation is an invitee, so it reads ``self`` (which wins
        precedence in the bar). The first cut captured only the first name, so
        this persona was ``self_named=False, other_named=True`` — an invitee
        biased *toward silence*, the opposite of the intent."""
        a = detect_nl_addressing(content=self._LIST, persona_name="Ember Owl")
        assert a.self_named is True

    def test_comma_separated_list_classifies_each_invitee(self):
        content = "what do Iron Fox, Ember Owl, Gray Wolf think about Redis?"
        for name in ("Iron Fox", "Ember Owl", "Gray Wolf"):
            assert detect_nl_addressing(
                content=content, persona_name=name,
            ).self_named is True, name

    def test_uninvited_third_party_is_other_for_a_list(self):
        a = detect_nl_addressing(content=self._LIST, persona_name="Gray Wolf")
        assert a == NLAddressing(self_named=False, other_named=True)

    def test_list_does_not_swallow_trailing_prose(self):
        """Precision guard: a lower-cased continuation after ``and`` is prose,
        not a name, so it must not manufacture a phantom invitee. "ask Redis"
        following "and" must NOT register persona "Redis" as invited."""
        a = detect_nl_addressing(
            content="let's hear from Iron Fox and ask Redis",
            persona_name="Redis",
        )
        assert a.self_named is False


class TestSmartApostrophe:
    """Chat clients autocorrect ``let's`` to a curly apostrophe (U+2019). The
    flagship cue must still fire (finding #4) — otherwise the headline
    invitation silently misses on the most common real-world rendering."""

    def test_curly_apostrophe_invitation_still_fires(self):
        a = detect_nl_addressing(
            content="let’s hear from Iron Fox", persona_name="Iron Fox",
        )
        assert a.self_named is True
