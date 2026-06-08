"""RFC 0030 Tier B (v0.3.8), PR 3 — natural-language addressing extraction.

A free-text invitation of a named person ("let's hear from Iron Fox", "over
to Ember Owl") is a *salience signal* for the leased bid in
:mod:`agents.salience_bid`: it leans the bid's bar toward or away from the
bidding persona. It is **never** a hard pre-filter — structured ``@``-mentions
remain the only deterministic directed-elsewhere drop, owned by Tier A
(``agents.response_gate``) (TB4 / amendment OQ #2).

This module is the *pure extractor* — :func:`detect_nl_addressing` and the
:class:`NLAddressing` it returns. It is carved out of ``salience_bid.py`` so
that file stays under the 500-line review cap (the same separation that pulled
the action-loop seam into ``persona_runtime/salience_gate.py``). The bid
*consumes* the signal (the bar shift + the prompt nudge) in ``salience_bid``;
this module only *detects* it.

Conservative + high-precision by construction: only a curated set of clear
invitation cues fire, and an ambiguous capture (a pronoun, an empty run)
classifies as *neither* self nor other — so it suppresses no one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = ["NLAddressing", "detect_nl_addressing"]

# A curated, high-precision set of free-text *invitation* cues, each capturing
# the named recipient that follows it. Structured ``@``-mentions are
# deliberately absent — they are Tier A's deterministic filter, not this soft
# signal.
#
# ``_ONE_NAME`` captures up to three whitespace-separated word tokens; trailing
# connective/stop words are trimmed before classification so "Iron Fox on this"
# reads as the name "Iron Fox". ``_NAME_RUN`` then allows an explicit
# ``and``/``or``/``&``/``,`` list of such names ("Iron Fox and Ember Owl",
# "Iron Fox or Ember Owl") so a multi-person invitation classifies *every*
# invitee, not just the first — the list is split back apart in
# :func:`_split_names`.
# A name word, excluding the bare connective words ``and``/``or`` so a greedy
# capture stops at the list separator ("Iron Fox| and |Ember Owl") instead of
# swallowing it — the (?:and|or)\b lookahead matches only the *whole* word, so
# a real name like "Andie" or "Ori" is still allowed. Both connectives are
# excluded here *and* listed in ``_LIST_CONNECTIVE`` below — keep the two in
# lock-step, or a separator excluded from a name word but absent from the list
# (the original ``or`` gap) drops every invitee after it and mis-penalises them.
_NAME_WORD: Final[str] = r"(?!(?:and|or)\b)[A-Za-z][\w'\-]*"
_ONE_NAME: Final[str] = rf"{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,2}}"
_LIST_CONNECTIVE: Final[str] = r"(?:\s*,\s*|\s+and\s+|\s+or\s+|\s*&\s*|\s*\+\s*)"
_NAME_RUN: Final[str] = (
    rf"(?P<name>{_ONE_NAME}(?:{_LIST_CONNECTIVE}{_ONE_NAME})*)"
)
# Splits a captured name run back into its individual invitees on the same
# connectives ``_NAME_RUN`` joined them with.
_LIST_SPLIT: Final[re.Pattern[str]] = re.compile(
    r"\s*,\s*|\s+and\s+|\s+or\s+|\s*&\s*|\s*\+\s*", re.IGNORECASE,
)
_ADDRESS_CUES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"let'?s\s+hear\s+from\s+" + _NAME_RUN, re.IGNORECASE),
    re.compile(r"let\s+us\s+hear\s+from\s+" + _NAME_RUN, re.IGNORECASE),
    re.compile(r"\bover\s+to\s+" + _NAME_RUN, re.IGNORECASE),
    re.compile(
        r"\bhand(?:ing)?\s+(?:it\s+|this\s+)?(?:over\s+)?to\s+" + _NAME_RUN,
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwhat\s+(?:do|does|would|will)\s+" + _NAME_RUN + r"\s+(?:think|say)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bcurious\s+(?:what|to\s+hear\s+what)\s+" + _NAME_RUN + r"\b",
        re.IGNORECASE,
    ),
)

# Words that, although they can be *captured* by a cue, are not a named
# recipient — second-person/group references address no specific persona, so
# they resolve to "no addressing" rather than suppressing anyone. Includes the
# common group nouns ("team", "folks", "group", "guys", "crew") so addressing
# the whole channel ("over to the team") invites everyone / no one rather than
# registering a phantom recipient that would bias others toward silence.
_NON_NAME_TOKENS: Final[frozenset[str]] = frozenset({
    "you", "u", "we", "us", "all", "everyone", "everybody", "anyone",
    "someone", "anybody", "somebody", "them", "they", "folks", "team",
    "people", "yall", "me", "i", "myself", "here", "there",
    "group", "guys", "gang", "crew", "squad", "others", "channel",
    "room", "chat", "two", "both",
})

# Determiners / possessives / quantifiers that can precede a group noun ("the
# team", "our folks", "the whole crew", "both of you") or a name ("the Iron
# Fox"). Stripped before the non-name test so a leading article cannot leak a
# group reference through as a phantom named recipient (review finding #3),
# while a genuine name after the article still survives the strip.
_NAME_FILLERS: Final[frozenset[str]] = frozenset({
    "the", "a", "an", "our", "my", "your", "their", "his", "her", "its",
    "whole", "entire", "rest", "of", "both", "all",
})

# Trailing connective/stop words trimmed off a captured name run so a greedy
# capture ("Iron Fox on this") collapses to the bare name ("Iron Fox"). ("and"
# is deliberately absent — ``_NAME_WORD``'s ``(?!(?:and|or)\b)`` lookahead means
# a captured name can never *end* in a bare "and", so trimming it would be dead.)
_NAME_TRAILERS: Final[frozenset[str]] = frozenset({
    "on", "about", "regarding", "re", "for", "this", "that", "here",
    "please", "think", "thinks", "say", "says", "too", "as",
})

# Words that, at the *start* of a list continuation, mark it as trailing prose
# rather than another invitee ("Iron Fox and ask Redis", "... and we should pick
# Redis"). Used by :func:`_split_names` to stop the invitee list at a prose run
# *regardless of casing* — so an all-lowercase chat list ("iron fox and ember
# owl") still recovers every invitee (review finding #2) while a verb/discourse-
# led continuation is still rejected (the precision the Title-case rule bought).
_PROSE_LEADERS: Final[frozenset[str]] = _NON_NAME_TOKENS | frozenset({
    "and", "or", "but", "so", "then", "also", "plus", "if", "when", "while",
    "because", "the", "a", "an", "this", "that", "these", "those",
    "is", "are", "was", "were", "be", "been",
    "do", "does", "did", "can", "could", "should", "would", "will",
    "may", "might", "must", "shall",
    "ask", "tell", "see", "check", "get", "let", "lets", "go", "make",
    "take", "give", "pick", "use", "add", "maybe", "perhaps", "please", "just",
})

# Words that are *never* a name even when capitalised — so a Title-cased
# continuation led by one of them is prose, not an invitee, regardless of
# casing (review finding #2). Without this, the ``_split_names`` Title-case
# branch trusted any capital: a sentence-initial cap ("... and Maybe Redis"),
# the always-capitalised pronoun "I" ("... and I think Redis"), or a leading
# verb ("... and Add Redis") all manufactured a phantom recipient — and an
# un-named bystander matching that phantom's words was wrongly biased toward
# silence, breaking the "suppresses no one on an ambiguity" invariant.
#
# This is the whole prose-leader set *minus* the handful of words that double as
# real given names ("Will", "May") — those keep the Title-case rescue so a
# genuine second invitee is not dropped (a drop would, symmetrically, suppress
# *that* persona). Everything else is treated as prose the moment it leads a
# continuation, capital or not.
_NAMELIKE_PROSE_WORDS: Final[frozenset[str]] = frozenset({"will", "may"})
_NEVER_NAME_LEADERS: Final[frozenset[str]] = _PROSE_LEADERS - _NAMELIKE_PROSE_WORDS


@dataclass(frozen=True, slots=True)
class NLAddressing:
    """The natural-language-addressing signal for one bidding persona (PR 3).

    Attributes:
        self_named: A free-text cue invites *this* persona by name → bias the
            bid's bar toward speaking. Takes precedence over ``other_named``
            (being named is a speak signal).
        other_named: A free-text cue invites *a different* named person → bias
            the bar toward silence (but never a hard drop — a decisive score
            still clears).
    """

    self_named: bool
    other_named: bool


def _name_tokens(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", text.lower()))


def _clean_name(captured: str) -> str:
    """Trim trailing connective/stop words off a greedy name capture."""
    words = captured.split()
    while words and words[-1].lower().strip(".,!?;:") in _NAME_TRAILERS:
        words.pop()
    return " ".join(words)


def _split_names(captured: str) -> list[str]:
    """Split a captured name run ("Iron Fox and Ember Owl") into its invitees.

    The first name is *anchored* by the cue, so it is always kept. Subsequent
    list members are speculative — the connective could equally introduce
    trailing prose ("... and we should pick Redis"). A continuation is kept when:

    * it is **not** led by a never-a-name word — a pronoun, discourse marker,
      article, or copula is prose whatever its casing ("... and I think ...",
      "... and Maybe ...", review finding #2); then
    * it is either Title-cased (a plausible name, incl. an English-word name
      like "Will") *or* — for an all-lowercase chat list where casing carries no
      signal — not led by any prose/discourse word.

    A lower-cased prose-led continuation, or *any* never-a-name leader, stops
    the list. This recovers the genuine multi-invitee case, in either casing,
    without trusting a sentence-initial capital as a name and manufacturing a
    phantom recipient out of prose (high precision)."""
    parts = _LIST_SPLIT.split(captured)
    if not parts:
        return []
    names = [parts[0]]
    for part in parts[1:]:
        stripped = part.strip()
        if not stripped:
            break
        first_tok = stripped.split()[0].lower().strip(".,!?;:'\"")
        # A never-a-name leader is prose regardless of capitalisation — the
        # Title-case rescue below applies only to words that might be names.
        if first_tok in _NEVER_NAME_LEADERS:
            break
        if stripped[:1].isupper() or first_tok not in _PROSE_LEADERS:
            names.append(stripped)
        else:
            break
    return names


def detect_nl_addressing(*, content: str, persona_name: str) -> NLAddressing:
    """Extract the free-text addressing signal from the inbound message (PR 3).

    Conservative + high-precision: scans ``content`` for a curated set of
    invitation cues ("let's hear from …", "over to …", "what does … think"),
    classifies each captured name as *this* persona or *another*, and returns
    the combined signal. An ambiguous capture (a pronoun / non-name) or no cue
    at all yields ``NLAddressing(False, False)`` — it suppresses no one. This
    is **only** a bid bias (see :func:`agents.salience_bid.evaluate_salience`);
    structured ``@``-mentions remain Tier A's deterministic directed-elsewhere
    drop (TB4 / amendment OQ #2).
    """
    if not content or not persona_name:
        return NLAddressing(self_named=False, other_named=False)

    # Normalise the curly apostrophe chat clients autocorrect ``let's`` into
    # (U+2019 / U+02BC) to a straight ``'`` so the ``let'?s`` cue still fires on
    # the most common real-world rendering of the headline invitation.
    content = content.replace("’", "'").replace("ʼ", "'")

    persona_toks = _name_tokens(persona_name)
    if not persona_toks:
        return NLAddressing(self_named=False, other_named=False)

    self_named = False
    other_named = False
    for pattern in _ADDRESS_CUES:
        for match in pattern.finditer(content):
            for candidate in _split_names(match.group("name")):
                toks = _name_tokens(_clean_name(candidate))
                # Strip leading determiners ("the team", "our folks") so a group
                # reference is judged on its noun alone, not leaked through by a
                # bare article (review finding #3).
                core = toks - _NAME_FILLERS
                # A pronoun / group reference or an empty capture is not a named
                # recipient — ignore it (no one is suppressed on an ambiguity).
                if not core or core <= _NON_NAME_TOKENS:
                    continue
                # Subset either way: a first-name invitation ("Fox") matches the
                # full-name persona ("Iron Fox"), and vice-versa. NOTE: this is
                # deliberately lenient on a shared token — a persona named "Fox"
                # also matches an invitation of a *different* "Fox Hound". That
                # ambiguity favours a false *speak* (never a false suppression),
                # so it is safe for a signal that must never hard-drop a turn.
                if persona_toks <= core or core <= persona_toks:
                    self_named = True
                else:
                    other_named = True

    return NLAddressing(self_named=self_named, other_named=other_named)
