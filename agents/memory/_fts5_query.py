"""The FTS5 MATCH text episodic and notes recall send (ISSUE-0159).

Recall used to send the whole sanitised message as the MATCH text. FTS5 reads
a list of words as "every word must appear", so a natural sentence matched
nothing. :func:`fts5_match_query` sends one quoted phrase per word instead,
joined with OR, and lets bm25 rank the rows that share any of them:

* a word becomes the phrase of its runs of letters and digits, lower-cased,
  so ``unique-alpha-xyzzy`` stays one phrase, ``"unique alpha xyzzy"``, and
  its parts must sit together;
* common words are trimmed from the end of a word, which drops "it's" and
  turns "operator's" into "operator" but keeps "up-to-date" whole, and a
  word left as one single character is dropped; a message made only of
  common words is searched for those words as written;
* each phrase is searched once, and only the first :data:`MAX_MATCH_PHRASES`;
* a quoted phrase holds only ``[a-z0-9 ]``, so no message can produce FTS5
  syntax (``NOT``, ``*``, a stray quote) or raise a syntax error.

Memory code never imports persona code, so the tick sentence is spelled here
a second time; a test pins it to the persona runtime's own.
"""

from __future__ import annotations

import re

__all__ = [
    "FTS5_SANITIZE",
    "FTS5_STOPWORDS",
    "MAX_MATCH_PHRASES",
    "TICK_TEXT",
    "fts5_match_query",
]

# Strips everything but ASCII letters, digits and whitespace. Kept for the
# callers that still sanitise a raw query themselves (the LIKE fallbacks'
# logs and tests); the MATCH text is built from ``_RUN`` below.
FTS5_SANITIZE = re.compile(r"[^a-zA-Z0-9\s]+")

_RUN = re.compile(r"[A-Za-z0-9]+")

# NLTK's English stopword list without its 26 apostrophe forms ("don't",
# "it's"), which can never equal a run of letters: their parts ("don", "t")
# are in the list already.
FTS5_STOPWORDS: frozenset[str] = frozenset("""
a about above after again against ain all am an and any are aren as at be
because been before being below between both but by can couldn d did didn do
does doesn doing don down during each few for from further had hadn has hasn
have haven having he her here hers herself him himself his how i if in into
is isn it its itself just ll m ma me mightn more most mustn my myself needn
no nor not now o of off on once only or other our ours ourselves out over own
re s same shan she should shouldn so some such t than that the their theirs
them themselves then there these they this those through to too under until
up ve very was wasn we were weren what when where which while who whom why
will with won wouldn y you your yours yourself yourselves
""".split())

# Measured on EXP-001's arm D replay: 32 to 48 give the same recall, and the
# cap mostly cuts the shared tail of a long template message.
MAX_MATCH_PHRASES = 40

# A second copy of ``prompt_assembly._format_event``'s TICK text, pinned by
# ``test_fts5_match_query.py``. A tick searches nothing, so it admits no
# memory and the RFC 0017 §F empty-context short-circuit still skips the call.
TICK_TEXT = "Autonomous tick: review your goals and decide on next actions."


def fts5_match_query(raw: str) -> str | None:
    """Return the MATCH text for *raw*, ``""`` for no rows, or ``None``.

    ``None`` means *raw* holds no letter or digit: the caller keeps its own
    fallback (recency for episodes, a LIKE search for notes). ``""`` means
    search nothing: the caller returns no rows without querying.
    """
    if not _RUN.search(raw):
        return None
    if raw.strip() == TICK_TEXT:
        return ""
    phrases = _phrases(raw, trim=True) or _phrases(raw, trim=False)
    return " OR ".join(f'"{p}"' for p in phrases[:MAX_MATCH_PHRASES])


def _phrases(raw: str, *, trim: bool) -> list[str]:
    """One phrase per whitespace-separated word, de-duplicated in order."""
    seen: dict[str, None] = {}
    for word in raw.split():
        runs = [r.lower() for r in _RUN.findall(word)]
        if trim:
            while runs and runs[-1] in FTS5_STOPWORDS:
                runs.pop()
            if len(runs) == 1 and len(runs[0]) == 1:
                continue
        if runs:
            seen.setdefault(" ".join(runs), None)
    return list(seen)
