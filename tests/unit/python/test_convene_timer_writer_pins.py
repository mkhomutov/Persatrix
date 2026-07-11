"""RFC 0052 §E — external-source PINS for the agents.yaml convene-timer writer.

Split from ``test_convene_timer_writer.py`` (which owns the writer's *behaviour*)
because these tests assert something different in kind: that the writer agrees with
sources it does not own. Same split-by-concern seam as
``test_server_persona_wiring{,_timers,_timers_failures}.py``, and it keeps the
behaviour module honestly under the 500-line review cap.

Three sources, three failure modes if they drift:

  * ``schemas/agent.schema.json`` — the writer's output IS ``agents.yaml``. A
    schema-invalid ``autonomy`` block is rejected at the convener's load, and the
    carefully reversible, pattern-valid timer id inside it buys nothing. Validating
    the whole merged block (the way ``test_channel_config_schema.py`` validates
    channel config) pins the entry's ``required`` keys, the
    ``additionalProperties: false`` closure, the id pattern, the interval floor, and
    the ``level`` enum in one assert.
  * ``agents/server_persona.py`` — the writer mirrors its scheduler-gate levels, its
    ``level`` default, and its ``tick_interval_seconds`` default. A silent divergence
    is a silent correctness loss: a level outside the gate builds no ``TickScheduler``
    so the convene timer never arms, and a wrong default interval changes the cadence
    of the heartbeat the carry-forward is meant to preserve *unchanged*.
  * ``agents/tick.py`` / ``agents/event_loop.py`` — the legacy tick's id and kind, and
    the busy-loop interval floor that ``register_timer`` raises below.

The Go-side half of the same discipline (the channel-name charset, the ``convene-``
prefix, the ``convene`` kind) lives in ``test_convene_timer.py``'s drift guards.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema  # type: ignore[import-untyped]
import pytest

from agents.convene_timer import STANDING_CONVENE_KIND
from agents.convene_timer_writer import ConveneSpec, merge_convene_timers

# CWD-relative repo paths (CI runs pytest from the repo root), mirroring the
# sibling test_convene_timer.py.
_SERVER_PERSONA_PY = Path("agents/server_persona.py")
_AGENT_SCHEMA = Path("schemas/agent.schema.json")

_PLANNING = ConveneSpec(channel_id="group:planning", interval_seconds=86400)


def _convene_entry(timer_id: str, interval: int) -> dict:
    return {"id": timer_id, "interval_seconds": interval, "kind": STANDING_CONVENE_KIND}


def _autonomy_schema() -> dict:
    """The ``autonomy`` block definition, usable as a validator root (no ``$ref``s)."""
    schema = json.loads(_AGENT_SCHEMA.read_text(encoding="utf-8"))
    definition: dict = schema["definitions"]["autonomy"]
    return definition


class TestEmittedBlockIsSchemaValid:
    """The writer's output is ``agents.yaml`` — it must validate against
    ``schemas/agent.schema.json``'s ``autonomy`` definition or the convener rejects it
    at load, and the reversible timer id it carries buys nothing. Validating the whole
    merged block (not just the id) pins the entry's ``required`` keys, the
    ``additionalProperties: false`` closure, the id pattern, the interval floor, and
    that ``_MIN_SCHEDULER_LEVEL`` is a member of the ``level`` enum — in one assert."""

    def _validate(self, block: dict) -> None:
        jsonschema.validate(instance=block, schema=_autonomy_schema())

    def test_bumped_convener_block_validates(self) -> None:
        self._validate(merge_convene_timers({"level": "reactive"}, [_PLANNING]))

    def test_carried_forward_tick_block_validates(self) -> None:
        # ``legacy_tick`` carries an underscore — legal under the timer-id pattern
        # (``^[a-z0-9][a-z0-9_-]*[a-z0-9]$``) though NOT under the channel-name
        # charset. That asymmetry is exactly why the id codec rejects ``convene-foo_bar``.
        block = merge_convene_timers(
            {"level": "semi-autonomous", "tick_interval_seconds": 30}, [_PLANNING]
        )
        assert {"legacy_tick", "convene-planning"} == {t["id"] for t in block["timers"]}
        self._validate(block)

    def test_block_with_passthrough_knobs_validates(self) -> None:
        self._validate(
            merge_convene_timers(
                {
                    "level": "autonomous",
                    "timers": [],
                    "salience_threshold": 0.8,
                    "max_actions_per_tick": 5,
                    "idle_after_ticks": 20,
                },
                [_PLANNING, ConveneSpec("group:arch-review", 604800)],
            )
        )

    def test_the_rejected_sub_floor_interval_would_have_been_schema_invalid(self) -> None:
        # Ties the writer's ValueError (TestConveneEntry) to the schema rule it is
        # standing in for: had the writer emitted the entry, THIS is how it would have
        # failed — at the convener's next boot, far from the config edit that caused it.
        with pytest.raises(jsonschema.ValidationError):
            self._validate(
                {"level": "semi-autonomous", "timers": [_convene_entry("convene-x9", 0)]}
            )


class TestSourceDriftGuards:
    """The writer mirrors four facts it does not own — the scheduler-gate levels, the
    default level, the default tick interval, and the busy-loop floor. Each is copied
    from ``server_persona.py`` or the schema, and a silent divergence is a silent
    correctness loss (a level outside the gate never arms; a wrong default interval
    changes the carried heartbeat's cadence). Pin them at their sources, as
    ``test_convene_timer.py`` pins the Go-side constants."""

    def test_scheduler_levels_match_the_server_persona_gate(self) -> None:
        from agents.convene_timer_writer import _SCHEDULER_LEVELS

        src = _SERVER_PERSONA_PY.read_text(encoding="utf-8")
        m = re.search(r'if level in \(([^)]*)\):', src)
        assert m, "server_persona.py scheduler-gate shape drifted"
        gate = tuple(re.findall(r'"([^"]+)"', m.group(1)))
        assert _SCHEDULER_LEVELS == gate, (
            "writer's scheduler levels drifted from server_persona's gate — a level "
            "outside the gate builds no TickScheduler, so its convene timer never arms"
        )

    def test_default_level_matches_server_persona(self) -> None:
        from agents.convene_timer_writer import _DEFAULT_LEVEL, _SCHEDULER_LEVELS

        src = _SERVER_PERSONA_PY.read_text(encoding="utf-8")
        m = re.search(r'autonomy\.get\("level", "([^"]+)"\)', src)
        assert m, "server_persona.py level-default shape drifted"
        assert _DEFAULT_LEVEL == m.group(1)
        # Load-bearing: an unlevelled convener must resolve BELOW the gate, or the
        # writer would skip the bump and its timer would silently never fire.
        assert _DEFAULT_LEVEL not in _SCHEDULER_LEVELS

    def test_default_tick_interval_matches_server_persona_and_schema(self) -> None:
        from agents.convene_timer_writer import _DEFAULT_TICK_INTERVAL_SECONDS

        src = _SERVER_PERSONA_PY.read_text(encoding="utf-8")
        m = re.search(r'autonomy\.get\("tick_interval_seconds", (\d+)\)', src)
        assert m, "server_persona.py tick-default shape drifted"
        assert _DEFAULT_TICK_INTERVAL_SECONDS == int(m.group(1))
        schema_default = _autonomy_schema()["properties"]["tick_interval_seconds"]["default"]
        assert _DEFAULT_TICK_INTERVAL_SECONDS == schema_default

    def test_min_interval_matches_the_schema_and_the_event_loop(self) -> None:
        from agents.convene_timer_writer import _MIN_INTERVAL_SECONDS
        from agents.event_loop import EventLoop

        timer_item = _autonomy_schema()["properties"]["timers"]["items"]
        assert _MIN_INTERVAL_SECONDS == timer_item["properties"]["interval_seconds"]["minimum"]
        # The runtime half of the same floor: register_timer raises below it.
        assert _MIN_INTERVAL_SECONDS == EventLoop._MIN_INTERVAL

    def test_min_scheduler_level_is_a_scheduler_level_and_schema_legal(self) -> None:
        from agents.convene_timer_writer import _MIN_SCHEDULER_LEVEL, _SCHEDULER_LEVELS

        assert _MIN_SCHEDULER_LEVEL in _SCHEDULER_LEVELS
        assert _MIN_SCHEDULER_LEVEL in _autonomy_schema()["properties"]["level"]["enum"]


class TestLegacyTickConstantsMatchTickModule:
    """Cross-module drift pin: the carried-forward tick reuses the id/kind the
    shipped ``TickScheduler`` registers, so the materialized entry is identical to
    the wake the persona would otherwise have fired (and shares its cache row)."""

    def test_legacy_tick_id_and_kind_mirror_tick_module(self) -> None:
        from agents.convene_timer_writer import _LEGACY_TICK_ID, _LEGACY_TICK_KIND
        from agents.tick import _LEGACY_TIMER_ID

        assert _LEGACY_TICK_ID == _LEGACY_TIMER_ID
        # The kind the shipped scheduler stamps on the legacy wake.
        assert _LEGACY_TICK_KIND == "tick"
