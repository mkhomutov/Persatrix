"""EXP-001 harness — arm D′'s first two meetings on real processes, at no cost (PR 5d).

The unit tests hold arm D′'s meetings against stand-ins. This one starts the
real orchestrator binary and four real persona agent processes, with every
model alias on the offline mock provider, and holds the practice series'
briefing and first plan meeting through arm D′'s own hold. It checks what
only real processes can show: the plan meeting's advisers read the file of
the briefing's transcript the harness wrote, every call of every adviser's
turn carried it, and no other call did; the call log names the prefix each
call carried by its SHA-256. The mock provider has no cache, so the runtime
joins the prefix onto the front of the system prompt instead; the practice
run shows the cache itself (check 3).

On the mock provider every salience bid is silent, so a governed discussion
closes only when the idle window runs out. So the test gives arm D′ arm B's
governance, whose discussions end at the depth cap or the round limit, with
every adviser taking turns. What it tests does not depend on governance.

Opt in with ``pytest -m requires_orchestrator`` after ``make build-orchestrator``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from evaluators.exp001.arm_d_prime import (
    ARM,
    arm_d_prime_hold,
    prefix_sha256,
    transcript_prefix,
)
from evaluators.exp001.attempts import try_directory
from evaluators.exp001.costs import CallPurpose
from evaluators.exp001.deployed_meeting import CALL_LOG, PREFIX
from evaluators.exp001.deployment import REPO, Alias
from evaluators.exp001.materials import load_series
from evaluators.exp001.panel import Panel, load_panel
from evaluators.exp001.runtime import read_call_log

pytestmark = pytest.mark.requires_orchestrator

_EXP = REPO / "evaluators" / "experiments" / "EXP-001"
_BINARY = REPO / "bin" / "persatrix-server"
_MOCK = Alias("mock", "offline", 0, 0)
_SPEAKING = {CallPurpose.REPLY, CallPurpose.MEMO}


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
    """The panel, with arm D′ moved to arm B's channel block."""
    doc = yaml.safe_load((_EXP / "panel.yaml").read_text())
    doc["channel"]["governance_on"]["arms"].remove(ARM)
    doc["channel"]["governance_off"]["arms"].append(ARM)
    path = tmp_path / "panel.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    return load_panel(path)


async def test_the_plan_meeting_carries_the_briefing_in_every_turn(
    tmp_path: Path, binary: Path,
) -> None:
    panel = _governed_as_b(tmp_path)
    series = load_series(_EXP / "practice.yaml")
    briefing, plan = series.meetings[:2]
    root = tmp_path / "runs"
    hold = arm_d_prime_hold(
        panel, series, root, binary=binary, python=Path(sys.executable), alias=_MOCK,
    )

    first = await hold(briefing, 1, 1)
    second = await hold(plan, 1, 1)

    assert first.result is not None and second.result is not None
    assert (first.errors, second.errors) == ((), ())
    assert (first.result.failures, second.result.failures) == ((), ())
    briefing_try = try_directory(root.resolve(), 1, briefing, 1)
    plan_try = try_directory(root.resolve(), 1, plan, 1)
    assert not (briefing_try / PREFIX).exists()
    prefix = (plan_try / PREFIX).read_bytes().decode("utf-8")
    assert prefix == transcript_prefix([("advice-1", first.result.transcript)])
    assert briefing.message in prefix

    earlier = read_call_log(briefing_try / CALL_LOG).records
    assert earlier and all(r.cache_prefix is None for r in earlier)
    memo_turn = second.result.memo_turn
    assert memo_turn is not None
    calls = read_call_log(plan_try / CALL_LOG, memo_turns=[memo_turn]).records
    turns = [r for r in calls if r.purpose in _SPEAKING]
    assert {r.adviser for r in turns} == {a.id for a in panel.advisers}
    assert any(r.purpose is CallPurpose.MEMO for r in turns)
    assert {r.cache_prefix for r in turns} == {prefix_sha256(prefix)}
    assert all(r.cache_prefix is None for r in calls if r.purpose not in _SPEAKING)
    assert {(r.arm, r.meeting, r.attempt, r.meeting_try) for r in calls} == {
        (ARM, plan.id, 1, 1),
    }
