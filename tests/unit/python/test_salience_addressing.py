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

import pytest

from agents.salience_addressing import (
    NLAddressing,
    _split_names,
    detect_nl_addressing,
)


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


class TestLowercaseListInvitees:
    """Review finding #2 — a multi-invitee list typed in casual all-lowercase
    chat ("let's hear from iron fox and ember owl") must still classify *every*
    invitee as ``self``. The first PR-3 cut kept a list continuation only when
    it was Title-cased, so the second invitee in a lowercase list was dropped
    and the persona mis-read as ``other_named`` — biased *toward silence*, the
    exact opposite of being invited."""

    _LOWER = "let's hear from iron fox and ember owl on this"

    def test_lowercase_first_invitee_is_self(self):
        a = detect_nl_addressing(content=self._LOWER, persona_name="Iron Fox")
        assert a.self_named is True

    def test_lowercase_second_invitee_is_self_not_other(self):
        """The regression guard: the second name in a lowercase ``a and b``
        invitation is still an invitee, so it reads ``self`` (which wins bar
        precedence). The Title-case-only rule dropped it and produced
        ``self_named=False`` — an invitee biased *toward silence*. (``other``
        may also be set by the co-invitee Iron Fox; ``self`` precedence is what
        matters and is asserted here, matching the mixed-case sibling test.)"""
        a = detect_nl_addressing(content=self._LOWER, persona_name="Ember Owl")
        assert a.self_named is True

    def test_lowercase_comma_list_classifies_each_invitee(self):
        content = "what do iron fox, ember owl, gray wolf think about redis?"
        for name in ("Iron Fox", "Ember Owl", "Gray Wolf"):
            assert detect_nl_addressing(
                content=content, persona_name=name,
            ).self_named is True, name

    def test_mixed_case_prose_guard_still_holds(self):
        """Precision is preserved for the mixed-case case: "Iron Fox and ask
        Redis" must NOT register persona Redis — the continuation is a verb
        phrase, not an invitee (the original Title-case guard's intent)."""
        a = detect_nl_addressing(
            content="let's hear from Iron Fox and ask Redis", persona_name="Redis",
        )
        assert a.self_named is False

    def test_lowercase_prose_led_continuation_still_guarded(self):
        """Even all-lowercase, a continuation led by a discourse/verb word is
        prose, not a name: "... and we should ask redis" must not invite
        Redis (precision holds regardless of casing)."""
        a = detect_nl_addressing(
            content="let's hear from iron fox and we should ask redis",
            persona_name="Redis",
        )
        assert a.self_named is False


class TestGroupReferenceIsNotAName:
    """Review finding #3 — a determiner + group noun ("the team", "the folks",
    "the group", "you guys") addresses no specific persona, so it must classify
    as *neither*: it invites everyone / no one, never a someone-else penalty
    that would bias the whole channel toward silence. The non-name filter
    originally fired only when *every* captured token was a stop-word, so a
    leading article ("the") leaked the phrase through as a phantom recipient."""

    @pytest.mark.parametrize("content", [
        "hand this over to the team",
        "over to the folks",
        "let's hear from the group",
        "over to you guys",
        "over to the whole team",
        "over to the rest of the team",
    ])
    def test_group_reference_is_neither(self, content):
        a = detect_nl_addressing(content=content, persona_name="Iron Fox")
        assert a == NLAddressing(self_named=False, other_named=False), content

    def test_a_real_name_after_an_article_still_registers(self):
        """The determiner strip must not swallow a genuine name: "the Iron Fox"
        is still Iron Fox (high precision is not bought with false negatives)."""
        a = detect_nl_addressing(
            content="over to the Iron Fox", persona_name="Iron Fox",
        )
        assert a.self_named is True


class TestOrListInvitees:
    """Review finding #1 — an ``A or B`` invitation ("let's hear from Iron Fox
    or Ember Owl") names *both* people, exactly like ``A and B``. The first cut
    handled ``and`` / ``,`` / ``&`` / ``+`` but not ``or``: ``or`` was excluded
    from a name *word* (the ``(?!(?:and|or)\\b)`` lookahead, anticipating it as a
    separator) yet never added to the list connective, so the second invitee was
    dropped *and* the persona was mis-read as ``other_named`` — biased *toward
    silence*, the precise opposite of being invited."""

    _OR = "let's hear from Iron Fox or Ember Owl"

    def test_or_first_invitee_is_self(self):
        assert detect_nl_addressing(
            content=self._OR, persona_name="Iron Fox",
        ).self_named is True

    def test_or_second_invitee_is_self_not_other(self):
        """The regression guard for finding #1: the name after ``or`` is an
        invitee, so it reads ``self`` (which wins bar precedence). The first cut
        produced ``self_named=False, other_named=True`` — an invitee biased
        *toward silence*."""
        assert detect_nl_addressing(
            content=self._OR, persona_name="Ember Owl",
        ).self_named is True

    def test_or_uninvited_third_party_is_other(self):
        a = detect_nl_addressing(content=self._OR, persona_name="Gray Wolf")
        assert a == NLAddressing(self_named=False, other_named=True)

    def test_lowercase_or_list_classifies_each_invitee(self):
        content = "what do iron fox or ember owl think about redis?"
        for name in ("Iron Fox", "Ember Owl"):
            assert detect_nl_addressing(
                content=content, persona_name=name,
            ).self_named is True, name


class TestCapitalizedProseGuard:
    """Review finding #2 — the ``_split_names`` Title-case branch trusted *any*
    capitalised continuation as a name. But a sentence-initial capital ("... and
    Maybe we use Redis") and the always-capitalised pronoun "I" ("... and I think
    Redis is great") are prose, not invitees. Trusting their capital manufactured
    a *phantom* recipient — and when an un-named bystander persona happened to
    match that phantom's words, it was mis-read as ``other_named`` and biased
    *toward silence*, violating the module's "suppresses no one on an ambiguity"
    invariant. The original precision test only used a *lowercase* continuation
    ("ask Redis"), so the Title-case path went unguarded."""

    def test_capitalized_pronoun_continuation_is_not_a_name(self):
        """"... and I think Redis is great" must NOT register persona Redis: the
        continuation is prose led by the pronoun "I", not an invitation."""
        a = detect_nl_addressing(
            content="let's hear from Iron Fox and I think Redis is great",
            persona_name="Redis",
        )
        assert a.self_named is False

    def test_capitalized_discourse_continuation_is_not_a_name(self):
        """"... and Maybe Redis" must NOT register persona Redis: a capitalised
        discourse word ("Maybe") leading the continuation is prose, not a name.
        (Kept inside the 3-word name-capture window so the phantom is reached —
        a longer run would be truncated by ``_ONE_NAME`` for unrelated reasons
        and would mask the guard under test.)"""
        a = detect_nl_addressing(
            content="let's hear from Iron Fox and Maybe Redis",
            persona_name="Redis",
        )
        assert a.self_named is False

    def test_split_drops_capitalized_prose_continuation(self):
        """Direct precision guard on the splitter: a capitalised prose run after
        ``and`` is not an invitee, so it must not be extracted (otherwise an
        un-named bystander matching its words is wrongly biased to silence)."""
        assert _split_names("Iron Fox and Maybe we use Redis") == ["Iron Fox"]
        assert _split_names("Iron Fox and I disagree") == ["Iron Fox"]

    def test_capitalized_namelike_word_is_still_kept(self):
        """Recall guard: a word that doubles as a real given name ("Will") is
        still a valid second invitee when capitalised — the precision fix must
        not over-correct and drop genuine names."""
        a = detect_nl_addressing(
            content="let's hear from Iron Fox and Will", persona_name="Will",
        )
        assert a.self_named is True


class TestOverToTopicPivot:
    """Review finding #1 — ``over to`` is also a common topic-pivot phrasal verb
    ("let's move over to caching", "switching over to production"), not only an
    address. A *lowercase* capture after it is an ambiguous topic, not a clear
    invitation, so it must classify as *neither* — never an ``other_named`` that
    would bias **every** bystander persona toward silence on a message that named
    no one. The capital is the signal that separates a clear address ("over to
    Iron Fox") from a topic pivot; it gates only the suppressing direction."""

    @pytest.mark.parametrize("content", [
        "let's move over to caching",
        "switching over to production now",
        "ok let's hand this over to logging",
        "what do we think — over to scaling",
    ])
    def test_lowercase_topic_pivot_suppresses_no_one(self, content):
        # No persona is named, so no persona may be biased toward silence.
        for persona in ("Iron Fox", "Ember Owl"):
            a = detect_nl_addressing(content=content, persona_name=persona)
            assert a == NLAddressing(self_named=False, other_named=False), (
                content, persona,
            )

    def test_capitalized_name_after_over_to_still_registers_other(self):
        """Recall guard: the precision fix must not over-correct — a genuinely
        capitalised name after ``over to`` still biases an un-named bystander."""
        a = detect_nl_addressing(content="over to Iron Fox", persona_name="Ember Owl")
        assert a.other_named is True

    def test_lowercase_self_address_still_pulls_the_named_persona_up(self):
        """The capital gates only the suppressing ``other`` direction: a lowercase
        self-address still lifts the *named* persona (a false speak is safe; a
        false suppression is not)."""
        a = detect_nl_addressing(content="over to iron fox", persona_name="Iron Fox")
        assert a.self_named is True


class TestPronounAnchoredListIsNotAName:
    """Review finding #2 — a continuation after a *pronoun*-anchored "list" is not
    a genuine invitee list: "let's hear from you and Postgres" names no specific
    person (the anchor "you" dissolves to *neither*; "Postgres" is a trailing
    topic, capitalised though it is). The dissolved anchor must not let the
    continuation register an ``other_named`` that biases a bystander toward
    silence. A list anchored by a *real* name is unaffected."""

    def test_pronoun_anchor_with_capitalized_topic_is_neither(self):
        a = detect_nl_addressing(
            content="let's hear from you and Postgres",
            persona_name="Ember Owl",
        )
        assert a == NLAddressing(self_named=False, other_named=False)

    def test_real_name_anchor_still_biases_a_bystander(self):
        """Recall guard: a list genuinely anchored by a real name still biases an
        un-named bystander toward silence — the fix targets only the dissolved-
        anchor case, not real multi-invitee lists."""
        a = detect_nl_addressing(
            content="let's hear from Iron Fox and Ember Owl",
            persona_name="Gray Wolf",
        )
        assert a.other_named is True
