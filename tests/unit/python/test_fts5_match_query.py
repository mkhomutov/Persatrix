"""Tests for the FTS5 MATCH builder episodic and notes recall share (ISSUE-0159).

Recall used to pass the whole sanitised message to FTS5, which reads a list
of words as "every word must appear", so a natural sentence matched nothing.
The builder turns a message into one quoted phrase per word, joined with OR,
with common words trimmed and a cap on the phrase count.
"""

import re

import pytest

from agents.memory._fts5_query import (
    EPISODE_STRUCTURAL_WORDS,
    MAX_MATCH_PHRASES,
    TICK_TEXT,
    fts5_match_query,
)
from agents.memory.boundary_detectors import REASON_IDLE_GAP, REASON_STRUCTURAL
from agents.memory.interaction_janitor import (
    SUMMARY_PENDING_TEXT,
    SUMMARY_UNAVAILABLE_TEXT,
)
from agents.memory.interaction_types import (
    LIVE_DUPLICATE_TURN_KEY,
    ROOM_CLOSE_TURN_KEY,
)
from agents.persona_runtime.prompt_assembly import _PromptAssemblyMixin
from agents.persona_types import ActionType, AgentEvent, EventType


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


# ─── Episode search leaves out the words the system writes itself ────────


def _episode_query(raw: str) -> str | None:
    return fts5_match_query(raw, structural=EPISODE_STRUCTURAL_WORDS)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # How an agent's message reaches recall: "message" would match
        # every closed conversation's stored context.
        (
            "Message from cobalt-wren:\n\nWhat do you think about the event?",
            '"cobalt wren" OR "think"',
        ),
        # A person's message: the wrapper's words go, the speaker stays.
        ('<|user_message user_id="local"|>', '"user id local"'),
        # A word made only of system words goes; one that only starts with
        # one stays whole, so its parts must still sit together.
        ("send_channel_message channel_id fireworks", '"fireworks"'),
        ("message-board", '"message board"'),
        # A message of nothing but common and system words is searched as
        # written, so an operator can still look for an event type.
        ("task_assigned", '"task assigned"'),
        ("is the message?", '"is" OR "the" OR "message"'),
        ("NOT", '"not"'),
    ],
)
def test_episode_search_drops_the_words_the_system_writes(raw, expected):
    assert _episode_query(raw) == expected


def test_notes_search_keeps_those_words():
    """Notes hold only what was written into them, so nothing is dropped."""
    assert fts5_match_query("channel message event") == (
        '"channel" OR "message" OR "event"'
    )


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z0-9]+", text)}


def test_the_system_words_cover_every_value_the_runtime_stores():
    """Every event and action type, the usual close reasons, the scope
    prefixes, the summary placeholders and the JSON literals reach episode
    rows on most turns, so each must be on the list. The rare close reasons
    (cost, topic shift, turn cap, shutdown, catch-up) are left off on
    purpose: they reach few rows, and "cost" and "topic" are real words."""
    stored: set[str] = set()
    for value in [e.value for e in EventType] + [a.value for a in ActionType]:
        stored |= _words(value)
    for text in (
        REASON_STRUCTURAL, REASON_IDLE_GAP, ROOM_CLOSE_TURN_KEY,
        LIVE_DUPLICATE_TURN_KEY, SUMMARY_PENDING_TEXT,
        SUMMARY_UNAVAILABLE_TEXT, "thread: group: dm:", "null true false",
    ):
        stored |= _words(text)
    assert stored <= EPISODE_STRUCTURAL_WORDS
    assert {"cost", "topic"}.isdisjoint(EPISODE_STRUCTURAL_WORDS)
