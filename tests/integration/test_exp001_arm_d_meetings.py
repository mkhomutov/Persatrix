"""EXP-001 harness — arm D's first two meetings on one real deployment, at no cost (PR 5c).

The unit tests hold arm D's meetings against stand-ins. This one starts the
real orchestrator binary and four real persona agent processes, with every
model alias on the offline mock provider, and holds the practice series'
briefing and first plan meeting on one deployment. It checks what only real
processes can show: the briefing leaves memory in every adviser's store; the
restart for the plan meeting replays no message and leaves every store as it
was, which the harness checks before the operator speaks (check 2); and the
plan meeting still runs to its memo, its calls under its own tags.

On the mock provider every salience bid is silent, so a governed discussion
closes only when the idle window runs out, and that close reaches no adviser.
So the test gives arm D arm B's governance, whose discussions end at the
depth cap or the round limit, a close every adviser is told of. What it
tests, the deployment and its restarts, does not depend on governance.

Opt in with ``pytest -m requires_orchestrator`` after ``make build-orchestrator``.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

from evaluators.exp001 import arm_d
from evaluators.exp001.arm_d import STORES_BEFORE, catch_up_replayed, memory_rows, run_meeting
from evaluators.exp001.channel_arm import Limits
from evaluators.exp001.deployed_meeting import CALL_LOG
from evaluators.exp001.deployment import REPO, Alias
from evaluators.exp001.materials import load_series
from evaluators.exp001.panel import Panel, load_panel
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


@pytest.fixture(autouse=True)
def _no_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """No collector runs here, so each adviser's exporters would spend 15 to
    28 seconds of every stop retrying it; the processes inherit this."""
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")


def _governed_as_b(tmp_path: Path) -> Panel:
    """The panel, with arm D moved to arm B's channel block."""
    doc = yaml.safe_load((_EXP / "panel.yaml").read_text())
    doc["channel"]["governance_on"]["arms"].remove("D")
    doc["channel"]["governance_off"]["arms"].append("D")
    path = tmp_path / "panel.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    return load_panel(path)


async def test_arm_d_holds_two_meetings_on_one_deployment(tmp_path: Path, binary: Path) -> None:
    panel = _governed_as_b(tmp_path)
    series = load_series(_EXP / "practice.yaml")
    briefing, plan = series.meetings[:2]
    here = tmp_path / "attempt-1"

    results = [
        await run_meeting(
            panel, series, meeting, attempt=1, meeting_try=1,
            directory=here / meeting.id / "try-1", deployment=here / "deployment",
            before=here / meeting.id / STORES_BEFORE, binary=binary,
            python=Path(sys.executable), alias=_MOCK,
        )
        for meeting in (briefing, plan)
    ]

    assert [r.failures for r in results] == [(), ()]
    memo, memo_turn = results[1].memo, results[1].memo_turn
    assert memo is not None and memo.sender == panel.chair.id and memo_turn is not None
    for adviser in panel.advisers:
        kept = memory_rows(here / plan.id / STORES_BEFORE / "memory" / f"{adviser.id}.db")
        assert kept is not None and kept["episodes"], f"{adviser.id} kept no briefing memory"
        log = here / plan.id / "try-1" / "logs" / f"{adviser.id}.log"
        assert catch_up_replayed(log, adviser.id) == 0
    calls = read_call_log(here / plan.id / "try-1" / CALL_LOG, memo_turns=[memo_turn])
    assert calls.failures == ()
    assert {(r.arm, r.meeting, r.attempt, r.meeting_try) for r in calls.records} == {
        ("D", plan.id, 1, 1),
    }


def _close_reasons(db: Path) -> list[str]:
    with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as store:
        rows = store.execute("SELECT context_json FROM episodes").fetchall()
    return [str(json.loads(context or "{}").get("close_reason")) for (context,) in rows]


async def test_a_briefing_that_ends_by_the_idle_window_keeps_its_memory(
    tmp_path: Path, binary: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle close tells no adviser, so each still holds the conversation
    open when the harness stops it, and the stop writes it (ISSUE-0172).
    Arm D's own governance ends every discussion this way on the mock; the
    test shortens the channels' idle window, and the harness's with it, to
    30 seconds. It still takes about six minutes: after the close the
    harness waits up to five for the turns still in flight, and a turn cut
    short by the stop never records the message it was answering."""
    idle = 30
    channels = arm_d.series_channels
    monkeypatch.setattr(arm_d, "series_channels", lambda panel, series: [
        {**c, "interaction_idle_timeout_seconds": idle} for c in channels(panel, series)
    ])
    panel = load_panel(_EXP / "panel.yaml")
    series = load_series(_EXP / "practice.yaml")
    briefing = series.meetings[0]
    here = tmp_path / "attempt-1"

    result = await run_meeting(
        panel, series, briefing, attempt=1, meeting_try=1,
        directory=here / briefing.id / "try-1", deployment=here / "deployment",
        before=here / briefing.id / STORES_BEFORE, binary=binary,
        python=Path(sys.executable), alias=_MOCK,
        limits=Limits(idle=dt.timedelta(seconds=idle)),
    )

    assert (result.trigger, result.failures) == ("idle", ())
    reasons = {
        a.id: _close_reasons(here / "deployment" / "memory" / f"{a.id}.db")
        for a in panel.advisers
    }
    assert all(reasons.values()), reasons
    assert "shutdown" in {r for kept in reasons.values() for r in kept}, reasons
