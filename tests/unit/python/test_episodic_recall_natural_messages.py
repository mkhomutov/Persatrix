"""Episodic and notes recall from natural messages (ISSUE-0159).

Two faults kept recall from ever answering a real message. The whole message
went to FTS5 as one query, which FTS5 reads as "every word must appear", so a
sentence matched nothing. And the relevance floor ``1/(1+|bm25|) >= min_score``
ran backwards: bm25 grows with the match, so the strongest matches scored
lowest and were dropped. Now each word is one phrase joined with OR, and
``min_score`` is the share of the best match's relevance a row must reach,
the best being taken over the rows this caller may see.
"""

import logging

import pytest

from agents.memory import _notes_recall, episodic_queries
from agents.memory._fts5_query import TICK_TEXT
from agents.memory.episodic import DEFAULT_EPISODIC_MIN_SCORE, EpisodicMemory

FIREWORKS = "Agreed the harbour festival fireworks barge needs a coastguard permit"

LONG_MESSAGE = (
    "Hello everyone, thanks for joining again tonight. Before we get into the new "
    "proposals, tell me plainly where you think we stand on the fireworks, what risks "
    "you see now, and what you would want confirmed in writing before we commit any "
    "money to it, because the committee meets on Thursday and wants a clear answer."
)


# ─── A natural message finds its episode ─────────────────────────────────


@pytest.mark.parametrize("other_rows", [0, 1, 2])
async def test_a_long_message_recalls_its_episode_in_a_tiny_store(
    memory: EpisodicMemory, other_rows: int,
):
    """The headline case: one shared word in a sixty-word message is enough.

    Stores of one to three rows matter: FTS5 gives a word found in half the
    rows almost no weight, so every bm25 there is about 1e-6, and any fixed
    floor would empty recall. The floor is relative, so the best match passes.
    """
    target = await memory.store_episode(summary=FIREWORKS, context={})
    for i in range(other_rows):
        await memory.store_episode(
            summary=f"Reviewed the bakery flour supplier contract {i}", context={},
        )
    got = await memory.recall(LONG_MESSAGE, min_score=DEFAULT_EPISODIC_MIN_SCORE)
    assert [ep.id for ep in got] == [target]


async def test_an_echo_of_the_summary_recalls_the_episode_first(memory: EpisodicMemory):
    """The strongest possible match used to be the one the floor discarded,
    which kept MT-PERSONA-CONFIDENTIALITY-001 Leg 4's echo from reaching the
    episode it quotes."""
    summary = (
        "Helix rollout is paused until the security review clears; the vendor "
        "patch lands after the audit window closes next quarter"
    )
    target = await memory.store_episode(summary=summary, context={})
    for i in range(5):
        await memory.store_episode(
            summary=f"Weekly standup notes {i}: rollout of the new rota", context={},
        )
    got = await memory.recall(summary, min_score=DEFAULT_EPISODIC_MIN_SCORE)
    assert got
    assert got[0].id == target


async def test_the_default_floor_drops_a_row_sharing_only_a_common_word(
    memory: EpisodicMemory,
):
    assert DEFAULT_EPISODIC_MIN_SCORE == 0.20
    target = await memory.store_episode(summary=FIREWORKS, context={})
    await memory.store_episode(summary="Reviewed the bakery flour supplier contract", context={})
    await memory.store_episode(summary="Discussed the library reading club rota", context={})
    weak = await memory.store_episode(
        summary="The festival bakery stall needs a new awning", context={},
    )
    query = "What did we decide about the festival fireworks permit?"
    got = await memory.recall(query, min_score=DEFAULT_EPISODIC_MIN_SCORE)
    assert [ep.id for ep in got] == [target]
    assert [ep.id for ep in await memory.recall(query)] == [target, weak]


async def test_a_word_of_common_words_is_searched_as_written(memory: EpisodicMemory):
    """A message made only of common words is not dropped: it is searched for
    those words. 'NOT' is a word here, not an FTS5 operator."""
    hit = await memory.store_episode(summary="recipe for NOT burning toast", context={})
    await memory.store_episode(summary="unrelated note about toast", context={})
    assert [ep.id for ep in await memory.recall("NOT")] == [hit]


# ─── What min_score means now ────────────────────────────────────────────


@pytest.mark.parametrize("min_score", [None, 0.0, 0.2, 1.0])
async def test_a_single_match_survives_any_floor(memory: EpisodicMemory, min_score):
    only = await memory.store_episode(
        summary="bioluminescent plankton coastal observation", context={}, importance=0.9,
    )
    got = await memory.recall(
        "What did we log about the bioluminescent plankton?", min_score=min_score,
    )
    assert [ep.id for ep in got] == [only]


async def test_an_identifier_is_one_phrase(memory: EpisodicMemory):
    alpha = await memory.store_note("alpha", "unique-alpha-xyzzy")
    await memory.store_note("beta", "unique-beta-xyzzy")
    assert [n.id for n in await memory.recall_notes("unique-alpha-xyzzy")] == [alpha]
    assert await memory.recall_notes("unique-zeta-xyzzy") == []


# ─── The best match is taken only over rows this caller may see ──────────


async def test_another_tenants_stronger_match_does_not_floor_out_ours(
    memory: EpisodicMemory,
):
    await memory.store_episode(
        summary="harbour fireworks permit barge coastguard", context={},
        principal_id="tenant-b",
    )
    ours = await memory.store_episode(summary="fireworks budget draft", context={})
    got = await memory.recall("harbour fireworks permit", min_score=1.0)
    assert [ep.id for ep in got] == [ours]


async def test_a_walled_out_sessions_stronger_match_does_not_floor_out_ours(
    memory: EpisodicMemory,
):
    await memory.store_episode(
        summary="harbour fireworks permit barge coastguard", context={}, session_id="run-b",
    )
    ours = await memory.store_episode(
        summary="fireworks budget draft", context={}, session_id="run-a",
    )
    got = await memory.recall("harbour fireworks permit", min_score=1.0, sessions=["run-a"])
    assert [ep.id for ep in got] == [ours]


async def test_a_withheld_note_does_not_floor_out_an_allowed_one(memory: EpisodicMemory):
    await memory.store_note(
        "zephyr", "zephyr launch codename budget plan", protection_level="restricted",
    )
    public = await memory.store_note("budget", "budget review", protection_level="public")
    got = await memory.recall_notes(
        "zephyr launch codename budget", min_score=1.0,
        allowed_protection_levels=["public"],
    )
    assert [n.id for n in got] == [public]


# ─── A tick still recalls nothing ────────────────────────────────────────


async def test_the_tick_sentence_recalls_nothing_in_either_tier(memory: EpisodicMemory):
    """Rows that share the tick's words, in a public room, would otherwise
    match and end the RFC 0017 §F short-circuit, costing a model call per
    tick."""
    await memory.store_episode(
        summary="Event: tick -> Actions: ['do_nothing']",
        context={"actions": ["do_nothing"]}, protection_level="public",
    )
    await memory.store_episode(
        summary="Weekly review of the goals; we decide next actions Friday",
        context={}, protection_level="public",
    )
    await memory.store_note(
        "goals", "Review the quarterly goals and decide next actions.",
        protection_level="public",
    )
    assert await memory.recall(TICK_TEXT, min_score=DEFAULT_EPISODIC_MIN_SCORE) == []
    assert await memory.recall(TICK_TEXT) == []
    assert await memory.recall_notes(TICK_TEXT) == []


# ─── An engine error still falls back to a LIKE search ───────────────────


async def test_an_engine_error_falls_back_to_like_for_episodes(
    memory: EpisodicMemory, monkeypatch, caplog,
):
    """Quoted phrases cannot raise, so the fallback is reached by making the
    builder return a query FTS5 rejects."""
    hit = await memory.store_episode(summary=FIREWORKS, context={})
    monkeypatch.setattr(episodic_queries, "fts5_match_query", lambda _raw: "AND")
    with caplog.at_level(logging.WARNING):
        got = await memory.recall("coastguard permit")
    assert [ep.id for ep in got] == [hit]
    assert "falling back to LIKE" in caplog.text


async def test_an_engine_error_falls_back_to_like_for_notes(
    memory: EpisodicMemory, monkeypatch, caplog,
):
    hit = await memory.store_note("permit", FIREWORKS)
    monkeypatch.setattr(_notes_recall, "fts5_match_query", lambda _raw: "AND")
    with caplog.at_level(logging.WARNING):
        got = await memory.recall_notes("coastguard permit")
    assert [n.id for n in got] == [hit]
    assert "falling back to LIKE" in caplog.text
