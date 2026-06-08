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
# ``_NAME_RUN`` captures up to three whitespace-separated word tokens after a
# cue; trailing connective/stop words are trimmed before classification so
# "Iron Fox on this" reads as the name "Iron Fox".
_NAME_RUN: Final[str] = r"(?P<name>[A-Za-z][\w'\-]*(?:\s+[A-Za-z][\w'\-]*){0,2})"
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
# they resolve to "no addressing" rather than suppressing anyone.
_NON_NAME_TOKENS: Final[frozenset[str]] = frozenset({
    "you", "u", "we", "us", "all", "everyone", "everybody", "anyone",
    "someone", "anybody", "somebody", "them", "they", "folks", "team",
    "people", "yall", "me", "i", "myself", "here", "there",
})

# Trailing connective/stop words trimmed off a captured name run so a greedy
# capture ("Iron Fox on this") collapses to the bare name ("Iron Fox").
_NAME_TRAILERS: Final[frozenset[str]] = frozenset({
    "on", "about", "regarding", "re", "for", "this", "that", "here",
    "please", "think", "thinks", "say", "says", "and", "too", "as",
})


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

    persona_toks = _name_tokens(persona_name)
    if not persona_toks:
        return NLAddressing(self_named=False, other_named=False)

    self_named = False
    other_named = False
    for pattern in _ADDRESS_CUES:
        for match in pattern.finditer(content):
            toks = _name_tokens(_clean_name(match.group("name")))
            # A pronoun / group reference or an empty capture is not a named
            # recipient — ignore it (no one is suppressed on an ambiguity).
            if not toks or toks <= _NON_NAME_TOKENS:
                continue
            # Subset either way: a first-name invitation ("Fox") matches the
            # full-name persona ("Iron Fox"), and vice-versa.
            if persona_toks <= toks or toks <= persona_toks:
                self_named = True
            else:
                other_named = True

    return NLAddressing(self_named=self_named, other_named=other_named)
