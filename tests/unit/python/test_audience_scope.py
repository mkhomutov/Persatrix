"""ISSUE-0132 — when the audience check runs, and what it spends doing it.

Sibling of ``test_audience_gate.py``, which pins the *verdicts*.  This
module pins the two questions that come before one: for which turns does
the check resolve at all, and how many roster round trips does resolving
it cost?  Both are scope-lock-2 obligations, and both were review
findings on PR A2 — a skip keyed on the wrong fact fails OPEN, and a
fetch spent on an entry nobody will judge is the N+1 shape
``channel_roster`` was split to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from agents.persona_runtime.audience import (
    AUDIENCE_OFF,
    AUDIENCE_SHADOW,
    AudienceVerdict,
    resolve_turn_audience,
)
from agents.persona_runtime.channel_roster import (
    ChannelRoster,
    DirectoryStatus,
    RosterMember,
    member_ids_from_meta,
)

DM = "dm:alice:iron-fox"
ROOM = "group:standup"
OTHER = "group:pair"


@dataclass(frozen=True)
class _Entry:
    id: str
    protection_level: str | None
    source_channel_id: str | None


class _Fetcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch_members(self, channel_id: str) -> dict[str, Any] | None:
        self.calls.append(channel_id)
        return {"id": channel_id, "members": [{"id": "alice"}, {"id": "iron-fox"}]}

    async def fetch_directory(self) -> list[dict[str, Any]] | None:
        return None


def _roster(channel_id: str) -> ChannelRoster:
    return ChannelRoster(
        channel_id=channel_id,
        channel_meta={"id": channel_id},
        members=(
            RosterMember(id="alice", name="alice", role="", is_self=False),
            RosterMember(id="iron-fox", name="iron-fox", role="", is_self=True),
            RosterMember(id="bob", name="bob", role="", is_self=False),
        ),
        directory=DirectoryStatus.RESOLVED,
    )


async def _resolve(
    *,
    acting_channel_id: str | None = ROOM,
    classification: str | None = "internal",
    roster: ChannelRoster | None = ...,  # type: ignore[assignment]
    fetcher: _Fetcher | None = None,
    candidates: tuple[tuple[str, list[_Entry]], ...] = (
        ("facts", [_Entry("f1", "internal", DM)]),
    ),
):
    return await resolve_turn_audience(
        fetcher if fetcher is not None else _Fetcher(),
        _roster(acting_channel_id) if roster is ... and acting_channel_id else roster,
        mode=AUDIENCE_SHADOW,
        acting_channel_id=acting_channel_id,
        acting_classification=classification,
        candidates=candidates,
        agent_id="iron-fox",
    )


# ─── when the check runs ───────────────────────────────────


@pytest.mark.asyncio
async def test_a_public_floor_turn_resolves_no_audience_and_fetches_nothing() -> None:
    """§A rule (b): an unstamped turn acts at the ``public`` FLOOR, where
    every above-``public`` entry is withheld by §D before the audience
    clause and every ``public`` one is exempt.  No candidate can carry a
    verdict, so the whole resolution — and its round trips — is skipped.
    """
    fetcher = _Fetcher()
    assert await _resolve(classification=None, fetcher=fetcher) is None
    assert await _resolve(classification="public", fetcher=fetcher) is None
    assert fetcher.calls == []


@pytest.mark.asyncio
async def test_off_mode_resolves_nothing() -> None:
    audience = await resolve_turn_audience(
        _Fetcher(), _roster(ROOM), mode=AUDIENCE_OFF,
        acting_channel_id=ROOM, acting_classification="internal",
        candidates=(("facts", [_Entry("f1", "internal", DM)]),),
        agent_id="iron-fox",
    )
    assert audience is None


@pytest.mark.asyncio
async def test_a_stamped_turn_without_a_channel_is_unknown_not_skipped() -> None:
    """The fail-open this closes: ``acting_classification_for_event``
    reads the wire stamp and never looks at ``channel_id``, so a turn CAN
    act at ``internal`` with no channel id.  Keying the skip on the id
    would let such a turn admit every ``internal`` entry with the
    AND-condition never applied.  It resolves an unknown audience
    instead — recorded, and (not being an enforced verdict) admitted.
    """
    audience = await _resolve(acting_channel_id=None, roster=None)

    assert audience is not None
    assert audience.acting_channel_id is None
    assert audience.verdict(
        tier="facts", protection_level="internal", source_channel_id=DM,
    ) is AudienceVerdict.WITHHOLD_UNKNOWN_FETCH_FAILED


# ─── what resolving it costs ───────────────────────────────


@pytest.mark.asyncio
async def test_only_rooms_the_gate_will_judge_are_fetched() -> None:
    """The fetch set is the JUDGED set: a ``public`` entry is exempt, an
    above-rank entry is withheld before the audience clause, an
    unparseable label is a rule-(c) casualty, and ``notes`` carry no
    provenance.  None of the four can produce a verdict, so none of them
    is worth a round trip."""
    fetcher = _Fetcher()
    audience = await _resolve(
        fetcher=fetcher,
        candidates=(
            ("facts", [_Entry("public", "public", "group:a")]),
            ("facts", [_Entry("above", "secret", "group:b")]),
            ("facts", [_Entry("bad", "not-a-level", "group:c")]),
            ("notes", [_Entry("note", "internal", "group:d")]),
            ("facts", [_Entry("judged", "internal", OTHER)]),
        ),
    )

    assert fetcher.calls == [OTHER]
    assert audience is not None
    assert (audience.fetches, audience.source_rooms) == (1, 1)


@pytest.mark.asyncio
async def test_a_lost_acting_roster_spends_no_source_fetches() -> None:
    """With the acting audience unknown every verdict is fetch-failed
    before ``source`` is consulted, so K round trips cannot change one."""
    fetcher = _Fetcher()
    audience = await _resolve(roster=None, fetcher=fetcher)

    assert fetcher.calls == []
    assert audience is not None
    assert audience.fetches == 0
    # …and the source room is still REPORTED, so the cost bound can tell
    # "resolved for free" apart from "never asked".
    assert audience.source_rooms == 1
    assert audience.verdict(
        tier="facts", protection_level="internal", source_channel_id=DM,
    ) is AudienceVerdict.WITHHOLD_UNKNOWN_FETCH_FAILED


@pytest.mark.asyncio
async def test_no_fetcher_reports_no_fetches() -> None:
    """``fetches`` names round trips actually issued, so an agent with no
    roster fetcher wired must report zero — not one per source room."""
    audience = await resolve_turn_audience(
        None, _roster(ROOM), mode=AUDIENCE_SHADOW,
        acting_channel_id=ROOM, acting_classification="internal",
        candidates=(("facts", [_Entry("f1", "internal", DM)]),),
        agent_id="iron-fox",
    )
    assert audience is not None
    assert (audience.fetches, audience.source_rooms) == (0, 1)


@pytest.mark.asyncio
async def test_the_acting_room_is_free_so_fetches_can_trail_source_rooms() -> None:
    """The pre-seed the A1 rail pays for: a same-room candidate counts as
    a source room and costs no round trip, so ``fetches`` is a floor of
    ``source_rooms``, never an equality."""
    fetcher = _Fetcher()
    audience = await _resolve(
        fetcher=fetcher,
        candidates=(("facts", [_Entry("same", "internal", ROOM)]),),
    )
    assert fetcher.calls == []
    assert audience is not None
    assert (audience.fetches, audience.source_rooms) == (0, 1)


# ─── the id-set parse the check reads a source room through ───


class TestMemberIdsFromMeta:
    """``member_ids_from_meta`` guards, pinned directly rather than
    through a happy-path fake: the ``or None`` at the end is what keeps
    "unknown audience" and "empty room" from collapsing, and a collapse
    would turn a lost roster into a spurious ``withhold-disjoint``."""

    def test_reads_the_member_ids(self) -> None:
        meta = {"id": ROOM, "members": [{"id": "alice"}, {"id": "bob"}]}
        assert member_ids_from_meta(meta) == frozenset({"alice", "bob"})

    @pytest.mark.parametrize(
        "meta",
        [
            pytest.param(None, id="absent"),
            pytest.param([], id="not-a-dict"),
            pytest.param({"id": ROOM}, id="no-members-key"),
            pytest.param({"members": "alice"}, id="members-not-a-list"),
            pytest.param({"members": []}, id="empty-room"),
            pytest.param({"members": ["alice"]}, id="entries-not-dicts"),
            pytest.param({"members": [{"id": ""}]}, id="blank-id"),
            pytest.param({"members": [{"id": 7}]}, id="non-string-id"),
        ],
    )
    def test_every_unusable_shape_is_unknown_never_empty(self, meta: Any) -> None:
        assert member_ids_from_meta(meta) is None

    def test_unusable_entries_are_skipped_not_fatal(self) -> None:
        meta = {"members": [{"id": "alice"}, "bob", {"id": ""}, {"name": "x"}]}
        assert member_ids_from_meta(meta) == frozenset({"alice"})
