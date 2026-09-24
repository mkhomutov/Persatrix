"""EXP-001 harness — one arm-B meeting on a real deployment, at no cost (PR 5a).

The unit tests hold a meeting against stand-ins. This one starts the real
orchestrator binary and four real persona agent processes, with every model
alias on the offline mock provider, so it spends nothing and needs no key.
It checks what only the real processes can show: the config the harness
writes boots, the advisers register and discuss, the orchestrator logs the
close, the memo turn's REST calls are accepted, the chair answers the
request, and every adviser's calls land in the meeting's call log under the
meeting's tags.

Opt in with ``pytest -m requires_orchestrator`` after ``make build-orchestrator``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from evaluators.exp001.channel_arm import CALL_LOG, RECORD, run_meeting
from evaluators.exp001.deployment import REPO, Alias
from evaluators.exp001.materials import MeetingKind, load_series
from evaluators.exp001.panel import load_panel
from evaluators.exp001.runtime import read_call_log

pytestmark = pytest.mark.requires_orchestrator

_EXP = REPO / "evaluators" / "experiments" / "EXP-001"
_BINARY = REPO / "bin" / "persatrix-server"
_MOCK = Alias("mock", "offline", 0, 0)


@pytest.fixture
def binary() -> Path:
    if not _BINARY.is_file():
        pytest.skip(f"{_BINARY} is missing; run `make build-orchestrator`")
    return _BINARY


async def test_an_arm_b_meeting_on_the_mock_provider(tmp_path: Path, binary: Path) -> None:
    panel = load_panel(_EXP / "panel.yaml")
    series = load_series(_EXP / "practice.yaml")
    plan = next(m for m in series.meetings if m.kind is MeetingKind.PLAN)

    result = await run_meeting(
        panel, "B", series, plan, attempt=1, directory=tmp_path, binary=binary,
        python=Path(sys.executable), alias=_MOCK,
    )

    assert result.failures == ()
    assert result.trigger in {"end_votes", "structural"}
    assert result.opened.content == plan.message
    assert result.memo is not None and result.memo.sender == panel.chair.id
    assert result.memo_turn is not None and result.memo_turn.asked_at <= result.memo.at
    assert (tmp_path / RECORD).is_file()
    log = read_call_log(tmp_path / CALL_LOG, memo_turns=[result.memo_turn])
    assert log.failures == ()
    assert {r.adviser for r in log.records} == {a.id for a in panel.advisers}
    assert {(r.arm, r.series, r.meeting, r.attempt) for r in log.records} == {
        ("B", series.id, plan.id, 1),
    }
    assert any(r.purpose.value == "memo" and r.adviser == panel.chair.id for r in log.records)
