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

    Small stores matter: FTS5 gives a word found in half the rows or more
    almost no weight, so in a store of one or two rows every bm25 is about
    1e-6 and any fixed floor would empty recall. A third row gives the shared
    word real weight. The floor is relative, so the best match passes in all
    three.
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


@pytest.mark.parametrize("stored", ["Menu planning at the café", "Menu planning at the cafe"])
async def test_an_accented_word_finds_its_episode(memory: EpisodicMemory, stored: str):
    """The index folds accents (unicode61), so the query must keep the
    whole word for the tokenizer to fold, not cut it at the accent."""
    hit = await memory.store_episode(summary=stored, context={})
    await memory.store_episode(summary="Reviewed the bakery flour order", context={})
    assert [ep.id for ep in await memory.recall("Which café did we pick?")] == [hit]


async def test_a_message_in_another_script_is_searched(memory: EpisodicMemory):
    """A message with no ASCII letter used to fall back to the most recent
    episodes, whatever they were about."""
    hit = await memory.store_episode(summary="Обсудили бюджет фестиваля", context={})
    await memory.store_episode(summary="Reviewed the bakery flour order", context={})
    assert [ep.id for ep in await memory.recall("Какой бюджет фестиваля?")] == [hit]


async def test_several_common_words_recall_nothing(memory: EpisodicMemory):
    """They match nearly every row, and the relative floor always admits the
    best of them, so they would inject an arbitrary episode."""
    await memory.store_episode(summary="We will do it now and then", context={})
    await memory.store_note("plan", "Do it now, before the market opens")
    assert await memory.recall("Do it now", min_score=DEFAULT_EPISODIC_MIN_SCORE) == []
    assert await memory.recall_notes("Do it now", min_score=0.2) == []


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


async def test_a_withheld_episode_does_not_floor_out_an_allowed_one(
    memory: EpisodicMemory,
):
    """The §D gate runs after the search, so the caller names the levels
    the acting turn may inject and only those rows set the bar. The
    withheld row stays a candidate: the gate's projection branch needs to
    see it to serve a cleared-down stand-in."""
    secret = await memory.store_episode(
        summary="zephyr launch codename budget plan", context={},
        protection_level="restricted",
    )
    public = await memory.store_episode(
        summary="budget review", context={}, protection_level="public",
    )
    got = await memory.recall(
        "zephyr launch codename budget", min_score=1.0,
        floor_protection_levels=["public"],
    )
    assert [ep.id for ep in got] == [secret, public]


async def test_with_no_allowed_candidate_the_withheld_rows_set_the_bar(
    memory: EpisodicMemory,
):
    """Nothing allowed matched, so no allowed row can be floored out; the
    withheld rows still reach the gate for their stand-ins."""
    secret = await memory.store_episode(
        summary="zephyr launch codename", context={}, protection_level="restricted",
    )
    await memory.store_episode(
        summary="zephyr", context={}, protection_level="restricted",
    )
    got = await memory.recall(
        "zephyr launch codename", min_score=1.0, floor_protection_levels=["public"],
    )
    assert [ep.id for ep in got] == [secret]


# ─── The words the system writes into every row match nothing ────────────

# What the close path stores for a two-turn conversation (RFC 0020 §D: the
# context holds no message body, only the record's own bookkeeping).
CLOSED_CONVERSATION_CONTEXT = {
    "scope": "group:harbour-room",
    "close_reason": "idle_gap",
    "turn_count": 2,
    "governance_interaction_id": "",
    "turns": [
        {"at": 1790929200.49, "payload": {
            "summary": "Event: channel_message \u2192 Actions: ['send_channel_message']",
            "event_type": "channel_message", "sender": "iron-fox",
            "channel_id": "group:harbour-room", "timestamp": 1790929200.49,
            "participant_type": "user",
        }},
        {"at": 1790929260.12, "payload": {
            "summary": "Event: mention \u2192 Actions: ['do_nothing']",
            "event_type": "mention", "sender": "iron-fox",
            "channel_id": "group:harbour-room", "timestamp": 1790929260.12,
            "participant_type": "agent",
        }},
    ],
}


async def test_a_message_sharing_only_system_words_recalls_nothing(
    memory: EpisodicMemory,
):
    """Every closed conversation stores keys and values such as "message",
    "channel", "event" and "summary", and every tick stores "Event: tick ->
    Actions: [...]". A message sharing only those words with the store
    used to bring back five unrelated episodes and count each as used."""
    for topic in ("bakery flour supplier", "library reading club", "harbour lanterns"):
        await memory.store_episode(
            summary=f"Discussed the {topic}", context=CLOSED_CONVERSATION_CONTEXT,
        )
    for _ in range(3):
        await memory.store_episode(
            summary="Event: tick \u2192 Actions: ['send_channel_message']",
            context={"event": {}, "sender": None, "close_reason": "structural"},
        )
    query = "Message from cobalt-wren:\n\nAny thoughts on the event summary?"
    assert await memory.recall(query, min_score=DEFAULT_EPISODIC_MIN_SCORE) == []


async def test_a_task_is_still_found_by_the_words_of_its_payload(
    memory: EpisodicMemory,
):
    """A single-turn episode keeps its content only in the stored event, so
    content words must still search the context column."""
    task = await memory.store_episode(
        summary="Event: task_assigned \u2192 Actions: ['do_nothing']",
        context={"event": {"task": "Draft the lantern budget"},
                 "sender": "orchestrator", "close_reason": "structural"},
    )
    await memory.store_episode(
        summary="Discussed the bakery flour supplier",
        context=CLOSED_CONVERSATION_CONTEXT,
    )
    got = await memory.recall(
        "Message from cobalt-wren:\n\nHow is the lantern budget going?",
        min_score=DEFAULT_EPISODIC_MIN_SCORE,
    )
    assert [ep.id for ep in got] == [task]


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


async def test_the_tick_never_reaches_the_engine(memory: EpisodicMemory, caplog):
    """An empty MATCH is an FTS5 syntax error, which would fall back to a
    LIKE search for the whole tick sentence and log a warning every tick."""
    await memory.store_episode(summary=f"Echoed: {TICK_TEXT}", context={})
    await memory.store_note("tick", f"Echoed: {TICK_TEXT}")
    with caplog.at_level(logging.WARNING):
        assert await memory.recall(TICK_TEXT) == []
        assert await memory.recall_notes(TICK_TEXT) == []
    assert "falling back to LIKE" not in caplog.text


# ─── The search index drives the query ───────────────────────────────────


async def _search_work(memory: EpisodicMemory, search) -> int:
    """SQLite virtual-machine steps (in hundreds) one search costs."""
    steps = 0

    def count() -> int:
        nonlocal steps
        steps += 1
        return 0

    assert memory._db is not None
    await memory._db.set_progress_handler(count, 100)
    try:
        assert await search()
    finally:
        # sqlite3 clears the handler on None; aiosqlite types it as required.
        await memory._db.set_progress_handler(None, 100)  # type: ignore[arg-type]
    return steps


_FORTY_WORDS = " ".join(f"word{i}" for i in range(38)) + " fireworks permit"


@pytest.mark.parametrize("tier", ["episodes", "notes"])
async def test_search_work_does_not_grow_with_rows_that_do_not_match(
    memory: EpisodicMemory, tier: str,
):
    """With the relative floor's inner query, SQLite could walk the agent's
    rows and run the full-text match once per row, so a 40-word message
    over a few thousand episodes took seconds. The join is written so the
    full-text index stays the outer loop: rows that share no word cost
    nothing."""
    async def store(text: str) -> None:
        if tier == "episodes":
            await memory.store_episode(summary=text, context={"k": "v"})
        else:
            await memory.store_note("topic", text)

    async def search():
        if tier == "episodes":
            return await memory.recall(_FORTY_WORDS, min_score=0.2)
        return await memory.recall_notes(_FORTY_WORDS, min_score=0.2)

    await store(FIREWORKS)
    for i in range(50):
        await store(f"Reviewed the bakery flour supplier contract {i}")
    small = await _search_work(memory, search)
    for i in range(50, 450):
        await store(f"Reviewed the bakery flour supplier contract {i}")
    large = await _search_work(memory, search)
    assert large < 2 * small + 10


# ─── An engine error still falls back to a LIKE search ───────────────────


async def test_an_engine_error_falls_back_to_like_for_episodes(
    memory: EpisodicMemory, monkeypatch, caplog,
):
    """Quoted phrases cannot raise, so the fallback is reached by making the
    builder return a query FTS5 rejects."""
    hit = await memory.store_episode(summary=FIREWORKS, context={})
    monkeypatch.setattr(
        episodic_queries, "fts5_match_query", lambda _raw, **_kw: "AND",
    )
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
