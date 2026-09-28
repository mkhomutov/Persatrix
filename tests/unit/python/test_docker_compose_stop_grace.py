"""Guards the persona services' stop against Docker's 10-second kill.

A persona that stops writes the conversations it still holds open, and waits
up to ``DRAIN_TIMEOUT_SEC`` for their summaries (ISSUE-0172). Docker kills a
container 10 seconds after asking it to stop unless the service sets
``stop_grace_period``, which would cut that wait short, leave the summaries
unwritten and skip the rest of the agent's shutdown. So every service that
runs a persona (an agent server with its own memory volume at ``/app/data``)
must allow more than the wait, in every compose file.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from agents.persona_runtime.finalize_close import DRAIN_TIMEOUT_SEC

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILES = sorted(REPO_ROOT.glob("docker-compose*.yaml"))
_DURATION = re.compile(r"(\d+)([hms])")
_UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}


def _seconds(duration: str) -> int:
    """A compose duration such as ``90s`` or ``1m30s``, in seconds."""
    return sum(int(n) * _UNIT_SECONDS[unit] for n, unit in _DURATION.findall(duration))


def _persona_services(path: Path) -> dict[str, dict[str, Any]]:
    compose: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        name: service
        for name, service in (compose.get("services") or {}).items()
        if "--agent" in (service.get("command") or [])
        and any(str(v).endswith(":/app/data") for v in service.get("volumes") or [])
    }


def test_the_base_stack_runs_personas() -> None:
    assert _persona_services(REPO_ROOT / "docker-compose.yaml")


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_a_persona_service_waits_for_its_summaries(path: Path) -> None:
    short = {
        name: grace
        for name, service in _persona_services(path).items()
        if (grace := _seconds(str(service.get("stop_grace_period", "10s")))) <= DRAIN_TIMEOUT_SEC
    }
    assert short == {}, f"{path.name}: these stop within the summary wait"
