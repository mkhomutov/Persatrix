"""EXP-001 harness — a practice run on real processes, at no cost (PR 6b).

The unit tests hold the practice run against stand-in holds. This one holds
the practice series for arms A and B as the command does offline: arm A's
calls and the advisers' on the offline mock provider, B's meetings on the
real orchestrator binary and four real persona agent processes, so it spends
nothing and needs no key. It checks what only real records show: every
meeting is kept and read back, the chair's memo calls are told apart in the
call logs, the packets are drawn from the memos the chair wrote, and a run
started again holds nothing twice.

Opt in with ``pytest -m requires_orchestrator`` after ``make build-orchestrator``.
It takes about three minutes.
"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from agents.llm_client import LLMClient
from agents.llm_offline import MockProvider
from evaluators.exp001.__main__ import OFFLINE
from evaluators.exp001.costs import CallPurpose
from evaluators.exp001.deployment import REPO
from evaluators.exp001.materials import MeetingKind, load_materials
from evaluators.exp001.packets import load_adviser_names
from evaluators.exp001.pairs import pair_calls, read_pair
from evaluators.exp001.panel import load_panel
from evaluators.exp001.practice import run_practice
from evaluators.exp001.rating import read_packets

pytestmark = pytest.mark.requires_orchestrator

_EXP = REPO / "evaluators" / "experiments" / "EXP-001"
_BINARY = REPO / "bin" / "persatrix-server"


@pytest.fixture
def binary() -> Path:
    if not _BINARY.is_file():
        pytest.skip(f"{_BINARY} is missing; run `make build-orchestrator`")
    return _BINARY


async def _practice(root: Path, binary: Path) -> dict[str, Any]:
    return await run_practice(
        root, ("A", "B"), panel=load_panel(_EXP / "panel.yaml"),
        series=load_materials(_EXP).practice, names=load_adviser_names(_EXP / "panel.yaml"),
        client=LLMClient(MockProvider()), binary=binary, alias=OFFLINE, prompts=None,
    )


async def test_a_practice_run_of_arms_a_and_b_on_the_mock_provider(
    tmp_path: Path, binary: Path,
) -> None:
    series = load_materials(_EXP).practice
    report = await _practice(tmp_path, binary)

    rows = report["meetings"]
    assert [(r["arm"], r["meeting"]) for r in rows] == [
        (arm, m.id) for arm in ("A", "B") for m in series.meetings
    ]
    assert not any(r["cut_short"] or r["failures"] or r["answer_missing"] for r in rows)
    assert report["check_3"]["findings"] == []
    assert report["judge"] is None
    # Arm A's calls name the mock's model too, so nothing held offline is priced.
    assert set(report["usage"]["models"]) == {"offline"}

    b = tmp_path / "pairs" / series.id / "B"
    run = read_pair(b)
    assert run is not None and run.finished_attempt == 1
    memos = Counter(r.meeting for r in pair_calls(b, run).records
                    if r.purpose is CallPurpose.MEMO)
    assert memos == {m.id: 1 for m in series.meetings if m.kind is not MeetingKind.BRIEFING}

    drawn = read_packets(tmp_path)
    answered = [m for m in series.meetings if m.kind is not MeetingKind.BRIEFING]
    assert len(drawn.packets) == 2 * len(answered)

    started = time.monotonic()
    assert await _practice(tmp_path, binary) == report
    assert time.monotonic() - started < 30  # nothing held again
