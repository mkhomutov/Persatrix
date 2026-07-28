"""RFC 0037 §G (v0.3.12 PR 7) — the leak-tripwire core.

The executor-side half of Phase 3: normalized span hashing, the per-turn
:class:`TripwireWatch` carried to the ``ActionExecutor``, and the
``channel.confidentiality_tripwire`` audit emit (RFC 0009 shape, the
``agent.deliberated`` structured-log-egress precedent — there is no
Python→Go audit RPC, so the Go constant is a reserved registry entry).

Load-bearing contracts pinned here:

* **Lexical, not semantic.** A hit is a normalized verbatim span of at
  least :data:`TRIPWIRE_SPAN_WORDS` words — case / whitespace /
  punctuation folded, nothing fuzzier (§G "a normalized substring match
  over spans above a length threshold").
* **Metadata only — never the text.** The watch carries span *hashes*;
  the audit record names the persona, the target channel, the entry and
  its protection level, and a match count. The protected text can appear
  in neither (§G "not the leaked text itself").
* **A smoke detector, not a lock.** The tripwire never blocks, never
  raises — a poisoned watch or a hashing failure degrades to silence
  around a publish that proceeds unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agents.channel_wire_metadata import DispatchContext
from agents.confidentiality_tripwire import (
    AUDIT_EVENT_TRIPWIRE,
    TRIPWIRE_SPAN_WORDS,
    TRIPWIRE_WATCH_METADATA_KEY,
    TripwireWatch,
    TripwireWatchEntry,
    find_tripwire_hits,
    run_channel_message_tripwire,
    span_hashes,
    stamp_tripwire_watch,
    tripwire_watch_from_event,
)
from agents.dispatch import ActionExecutor
from agents.persona_types import ActionType, AgentAction, AgentEvent, EventType

_REPO_ROOT = Path(__file__).resolve().parents[3]

# A distinctive protected sentence — 12 normalized words, comfortably above
# the span threshold. If any fragment of it appears in an audit record the
# §G metadata-only wall has been breached.
_SECRET = (
    "Project Nightjar acquires Meadowlark Systems for ninety million "
    "dollars closing next quarter"
)
_BENIGN = "Let us schedule the retro for Thursday and invite the new team"


def _watch(
    *,
    acting: str | None = "internal",
    level: str = "restricted",
    content: str = _SECRET,
) -> TripwireWatch:
    return TripwireWatch(
        acting=acting,
        entries=(
            TripwireWatchEntry(
                tier="episodic",
                entry_id="ep-1",
                protection_level=level,
                span_hashes=span_hashes(content),
            ),
        ),
    )


def _audit_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        rec
        for rec in caplog.records
        if getattr(rec, "audit", None) is True
        and rec.getMessage() == AUDIT_EVENT_TRIPWIRE
    ]


class TestSpanHashes:
    def test_verbatim_copy_shares_hashes(self) -> None:
        assert span_hashes(_SECRET) & span_hashes(f"FYI: {_SECRET}, apparently.")

    def test_normalization_folds_case_whitespace_punctuation(self) -> None:
        mangled = (
            "project NIGHTJAR   acquires,\n Meadowlark - Systems for "
            "NINETY million dollars; closing next QUARTER!"
        )
        assert span_hashes(_SECRET) & span_hashes(mangled)

    def test_below_threshold_span_produces_no_hashes(self) -> None:
        seven_words = "one two three four five six seven"
        assert span_hashes(seven_words) == frozenset()

    def test_exact_threshold_span_produces_one_hash(self) -> None:
        exact = " ".join(f"w{i}" for i in range(TRIPWIRE_SPAN_WORDS))
        assert len(span_hashes(exact)) == 1

    def test_disjoint_texts_share_nothing(self) -> None:
        assert not span_hashes(_SECRET) & span_hashes(_BENIGN)

    def test_seven_word_copy_is_not_a_hit(self) -> None:
        # The leading 7 words of the secret inside otherwise-novel prose:
        # below the threshold, so no span hash can match.
        seven = " ".join(_SECRET.split()[: TRIPWIRE_SPAN_WORDS - 1])
        assert not span_hashes(_SECRET) & span_hashes(
            f"They said {seven} or something entirely different after that"
        )


class TestFindHits:
    def test_hit_on_verbatim_span(self) -> None:
        hits = find_tripwire_hits(_watch(), f"Heads up team: {_SECRET}.")
        assert [(h.entry.entry_id, h.matched_spans > 0) for h in hits] == [
            ("ep-1", True),
        ]

    def test_silent_on_benign_traffic(self) -> None:
        assert find_tripwire_hits(_watch(), _BENIGN) == []

    def test_entry_without_hashes_never_hits(self) -> None:
        watch = TripwireWatch(
            acting="internal",
            entries=(
                TripwireWatchEntry(
                    tier="facts",
                    entry_id="f-1",
                    protection_level="secret",
                    span_hashes=frozenset(),
                ),
            ),
        )
        assert find_tripwire_hits(watch, _SECRET) == []


class TestStampAndLift:
    def test_stamp_then_lift_round_trips(self) -> None:
        watch = _watch()
        metadata: dict[str, object] = {}
        stamp_tripwire_watch(metadata, watch)
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hi"},
            channel_id="group:planning",
            sender_id="alice",
            metadata=metadata,
        )
        assert tripwire_watch_from_event(event) is watch

    def test_stamp_none_writes_nothing(self) -> None:
        metadata: dict[str, object] = {}
        stamp_tripwire_watch(metadata, None)
        assert TRIPWIRE_WATCH_METADATA_KEY not in metadata

    def test_lift_tolerates_absent_and_garbage(self) -> None:
        clean = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={},
            channel_id="c",
            sender_id="s",
        )
        assert tripwire_watch_from_event(clean) is None
        poisoned = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={},
            channel_id="c",
            sender_id="s",
            metadata={TRIPWIRE_WATCH_METADATA_KEY: {"not": "a watch"}},
        )
        assert tripwire_watch_from_event(poisoned) is None

    def test_dispatch_context_lifts_watch_structurally(self) -> None:
        watch = _watch()
        metadata: dict[str, object] = {}
        stamp_tripwire_watch(metadata, watch)
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hi"},
            channel_id="group:planning",
            sender_id="alice",
            metadata=metadata,
        )
        context = DispatchContext.for_event(event, cascade_depth=1)
        assert context.origin_tripwire_watch is watch

    def test_dispatch_context_default_is_unwatched(self) -> None:
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={},
            channel_id="c",
            sender_id="s",
        )
        assert DispatchContext.for_event(
            event, cascade_depth=1
        ).origin_tripwire_watch is None
        assert DispatchContext(cascade_depth=1).origin_tripwire_watch is None


class TestAuditEmit:
    def test_hit_emits_metadata_only_audit_record(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.INFO):
            run_channel_message_tripwire(
                watch=_watch(),
                agent_id="ember-owl",
                channel_id="group:planning",
                content=f"Quick update: {_SECRET} — thoughts?",
            )
        records = _audit_records(caplog)
        assert len(records) == 1
        rec = records[0]
        assert rec.agent_id == "ember-owl"  # type: ignore[attr-defined]
        assert rec.channel_id == "group:planning"  # type: ignore[attr-defined]
        assert rec.entry_tier == "episodic"  # type: ignore[attr-defined]
        assert rec.entry_id == "ep-1"  # type: ignore[attr-defined]
        assert rec.protection_level == "restricted"  # type: ignore[attr-defined]
        assert rec.acting_classification == "internal"  # type: ignore[attr-defined]
        assert rec.matched_spans >= 1  # type: ignore[attr-defined]
        # §G metadata-only wall: no fragment of the protected text may ride
        # the record — not the message, not any extra value.
        rendered = repr(rec.__dict__)
        assert "Nightjar" not in rendered
        assert "Meadowlark" not in rendered

    def test_silent_on_benign_traffic(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.INFO):
            run_channel_message_tripwire(
                watch=_watch(),
                agent_id="ember-owl",
                channel_id="group:planning",
                content=_BENIGN,
            )
        assert _audit_records(caplog) == []

    def test_no_watch_is_a_no_op(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            run_channel_message_tripwire(
                watch=None,
                agent_id="ember-owl",
                channel_id="group:planning",
                content=_SECRET,
            )
        assert _audit_records(caplog) == []

    def test_one_record_per_implicated_entry(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        other = "The rollout freeze starts Monday and covers every region until February"
        watch = TripwireWatch(
            acting="internal",
            entries=(
                TripwireWatchEntry(
                    tier="episodic",
                    entry_id="ep-1",
                    protection_level="restricted",
                    span_hashes=span_hashes(_SECRET),
                ),
                TripwireWatchEntry(
                    tier="notes",
                    entry_id="n-1",
                    protection_level="secret",
                    span_hashes=span_hashes(other),
                ),
            ),
        )
        with caplog.at_level(logging.INFO):
            run_channel_message_tripwire(
                watch=watch,
                agent_id="ember-owl",
                channel_id="group:planning",
                content=f"{_SECRET}. Also: {other}.",
            )
        assert {
            (r.entry_tier, r.entry_id) for r in _audit_records(caplog)  # type: ignore[attr-defined]
        } == {("episodic", "ep-1"), ("notes", "n-1")}

    def test_never_raises_on_poisoned_watch(self) -> None:
        poisoned = TripwireWatch(
            acting="internal",
            entries=(
                TripwireWatchEntry(
                    tier="episodic",
                    entry_id="ep-1",
                    protection_level="restricted",
                    span_hashes=None,  # type: ignore[arg-type]
                ),
            ),
        )
        # Must degrade silently — the smoke detector never takes down egress.
        run_channel_message_tripwire(
            watch=poisoned,
            agent_id="ember-owl",
            channel_id="group:planning",
            content=_SECRET,
        )


class TestExecutorIntegration:
    """The tripwire runs inside ``ActionExecutor`` on ``SEND_CHANNEL_MESSAGE``
    and never blocks the publish (§G "the message is not blocked")."""

    @staticmethod
    def _action(content: str) -> AgentAction:
        return AgentAction(
            action_type=ActionType.SEND_CHANNEL_MESSAGE,
            payload={"channel_id": "group:planning", "content": content},
        )

    @staticmethod
    def _executor() -> tuple[ActionExecutor, AsyncMock]:
        publisher = AsyncMock()
        publisher.publish = AsyncMock(return_value=None)
        return ActionExecutor(channel_publisher=publisher), publisher

    @pytest.mark.asyncio
    async def test_fires_and_still_publishes(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        executor, publisher = self._executor()
        context = DispatchContext(cascade_depth=1, origin_tripwire_watch=_watch())
        with caplog.at_level(logging.INFO):
            results = await executor.execute(
                "ember-owl", [self._action(f"Team: {_SECRET}.")], context=context,
            )
        assert results[0]["status"] == "published"
        publisher.publish.assert_awaited_once()
        assert len(_audit_records(caplog)) == 1

    @pytest.mark.asyncio
    async def test_silent_on_benign_publish(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        executor, publisher = self._executor()
        context = DispatchContext(cascade_depth=1, origin_tripwire_watch=_watch())
        with caplog.at_level(logging.INFO):
            results = await executor.execute(
                "ember-owl", [self._action(_BENIGN)], context=context,
            )
        assert results[0]["status"] == "published"
        publisher.publish.assert_awaited_once()
        assert _audit_records(caplog) == []

    @pytest.mark.asyncio
    async def test_tripwire_failure_never_blocks_publish(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import agents.confidentiality_tripwire as tripwire_mod

        def _boom(*args: object, **kwargs: object) -> frozenset[str]:
            raise RuntimeError("hashing exploded")

        monkeypatch.setattr(tripwire_mod, "span_hashes", _boom)
        executor, publisher = self._executor()
        context = DispatchContext(cascade_depth=1, origin_tripwire_watch=_watch())
        results = await executor.execute(
            "ember-owl", [self._action(_SECRET)], context=context,
        )
        assert results[0]["status"] == "published"
        publisher.publish.assert_awaited_once()


class TestGoRegistryDriftPin:
    """The audit event name is registered Go-side (reserved constant — the
    ``agent.deliberated`` precedent) and must not drift from the Python
    emit's literal."""

    def test_go_constant_matches_python_literal(self) -> None:
        go_source = (
            _REPO_ROOT / "internal" / "security" / "audit_event.go"
        ).read_text(encoding="utf-8")
        assert f'"{AUDIT_EVENT_TRIPWIRE}"' in go_source
        assert AUDIT_EVENT_TRIPWIRE == "channel.confidentiality_tripwire"
