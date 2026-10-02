"""Tests for the FTS5 MATCH builder episodic and notes recall share (ISSUE-0159).

Recall used to pass the whole sanitised message to FTS5, which reads a list
of words as "every word must appear", so a natural sentence matched nothing.
The builder turns a message into one quoted phrase per word, joined with OR,
with common words trimmed and a cap on the phrase count.
"""

import pytest

from agents.memory._fts5_query import (
    MAX_MATCH_PHRASES,
    TICK_TEXT,
    fts5_match_query,
)
from agents.persona_runtime.prompt_assembly import _PromptAssemblyMixin
from agents.persona_types import AgentEvent, EventType


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # A natural question keeps its content words.
        (
            "Can you remind me what we said about the fireworks?",
            '"remind" OR "said" OR "fireworks"',
        ),
        # An identifier stays one phrase, so its parts must sit together.
        ("unique-alpha-xyzzy", '"unique alpha xyzzy"'),
        ("v0.3.16", '"v0 3 16"'),
        ("ISSUE-0159", '"issue 0159"'),
        # Common words are trimmed from the end of a word only, so a
        # compound that starts with one stays whole.
        ("operator's", '"operator"'),
        ("it's done, don't worry", '"done" OR "worry"'),
        ("up-to-date out-of-scope", '"up to date" OR "out of scope"'),
        # A lone one-character word is dropped; two-character words stay.
        ("1. Are there dates?", '"dates"'),
        ("Option B", '"option"'),
        ("Is the CI green on PR 1024?", '"ci" OR "green" OR "pr" OR "1024"'),
        ("tick: scheduler", '"tick" OR "scheduler"'),
        # A word is searched once, whatever its case.
        ("Plan plan PLAN", '"plan"'),
        # When trimming leaves nothing, the words are searched as written.
        ("from", '"from"'),
        ("NOT", '"not"'),
        ("is it?", '"is" OR "it"'),
        ("ok will do", '"ok"'),
        # Delimiters and figures split into their runs of letters and digits.
        (
            '<|user_message user_id="local"|>',
            '"user message" OR "user id local"',
        ),
        (
            "$1,200 for 2 000 programmes at $0.60",
            '"1 200" OR "000" OR "programmes" OR "0 60"',
        ),
        # Only the exact tick sentence is special.
        (
            "Autonomous tick: review the goals",
            '"autonomous" OR "tick" OR "review" OR "goals"',
        ),
    ],
)
def test_builds_one_quoted_phrase_per_word_joined_with_or(raw, expected):
    assert fts5_match_query(raw) == expected


@pytest.mark.parametrize("raw", ["*", ".,<>|!@#$%", "", "   "])
def test_text_without_letters_or_digits_builds_no_query(raw):
    """None tells the caller to keep its shipped fallback (recency for
    episodes, a LIKE search for notes)."""
    assert fts5_match_query(raw) is None


@pytest.mark.parametrize("raw", [TICK_TEXT, f"  {TICK_TEXT}\n"])
def test_the_tick_sentence_searches_nothing(raw):
    """An empty string tells the caller to return no rows, so a tick never
    admits memory and the empty-context short-circuit keeps skipping the
    model call (RFC 0017 §F)."""
    assert fts5_match_query(raw) == ""


def test_the_tick_sentence_matches_the_persona_runtime():
    """The memory package keeps its own copy of the tick sentence, because
    memory code never imports persona code. This pins the two together."""
    event = AgentEvent(event_type=EventType.TICK)
    assert _PromptAssemblyMixin._format_event(None, event) == TICK_TEXT


def test_only_the_first_phrases_up_to_the_cap_are_searched():
    words = [f"word{i}" for i in range(MAX_MATCH_PHRASES + 1)]
    query = fts5_match_query(" ".join(words))
    assert query == " OR ".join(f'"{w}"' for w in words[:MAX_MATCH_PHRASES])
    assert MAX_MATCH_PHRASES == 40
