"""Validation tests for ``autonomy.timers`` — RFC 0024 PR 2.

The new ``timers`` array adds per-agent scheduled timers as a peer of the
legacy ``tick_interval_seconds`` knob.  Phase 5 / v0.4.0 emits the
deprecation warning on ``tick_interval_seconds``; Phase 2 ships them
side-by-side so the v0.3.3 schema is *additive* — no rejection of
existing v0.3.2 configs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agents.validate import validate_config_dir


def _validate_passes(*args: str) -> bool:
    ok, _, _ = validate_config_dir(*args)
    return ok


@pytest.fixture()
def schemas_dir(tmp_path: Path) -> Path:
    real_schemas = Path("schemas")
    dest = tmp_path / "schemas"
    dest.mkdir()
    for schema_file in real_schemas.glob("*.json"):
        (dest / schema_file.name).write_text(
            schema_file.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return dest


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    return d


@pytest.fixture()
def workflow_dir(tmp_path: Path) -> Path:
    d = tmp_path / "workflows"
    d.mkdir()
    return d


def _write(config_dir: Path, data: dict) -> Path:
    p = config_dir / "agents.yaml"
    p.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    return p


_BASE_PERSONA: dict = {
    "id": "ember-owl",
    "type": "persona",
    "name": "Ember Owl",
    "role": "VP of Engineering",
    "model": "claude-sonnet-4-20250514",
    "persona": {
        "title": "VP of Engineering",
        "background": "15 years of experience.",
        "behavior": {},
    },
}


def _wrap(autonomy: dict) -> dict:
    return {
        "schema_version": "0.2",
        "agents": [{**_BASE_PERSONA, "autonomy": autonomy}],
    }


class TestTimersSchemaAccepted:
    def test_empty_timers_list_accepted(
        self,
        config_dir: Path,
        schemas_dir: Path,
        workflow_dir: Path,
    ) -> None:
        """Stock personas ship ``timers: []`` — the no-timers default."""
        _write(config_dir, _wrap({"level": "reactive", "timers": []}))
        assert _validate_passes(str(config_dir), str(schemas_dir), str(workflow_dir))

    def test_single_timer_accepted(
        self,
        config_dir: Path,
        schemas_dir: Path,
        workflow_dir: Path,
    ) -> None:
        _write(
            config_dir,
            _wrap({
                "level": "semi-autonomous",
                "timers": [
                    {
                        "id": "memory_consolidation",
                        "interval_seconds": 60,
                        "kind": "memory_consolidation",
                    },
                ],
            }),
        )
        assert _validate_passes(str(config_dir), str(schemas_dir), str(workflow_dir))

    def test_timer_with_jitter_accepted(
        self,
        config_dir: Path,
        schemas_dir: Path,
        workflow_dir: Path,
    ) -> None:
        _write(
            config_dir,
            _wrap({
                "level": "semi-autonomous",
                "timers": [
                    {
                        "id": "memory_consolidation",
                        "interval_seconds": 60,
                        "kind": "memory_consolidation",
                        "jitter_max_seconds": 5,
                    },
                ],
            }),
        )
        assert _validate_passes(str(config_dir), str(schemas_dir), str(workflow_dir))

    def test_timers_and_tick_interval_coexist(
        self,
        config_dir: Path,
        schemas_dir: Path,
        workflow_dir: Path,
    ) -> None:
        """Both knobs accepted side-by-side — runtime picks ``timers`` and
        logs INFO. Phase 5 (v0.4.0) emits the deprecation warning on
        ``tick_interval_seconds``."""
        _write(
            config_dir,
            _wrap({
                "level": "semi-autonomous",
                "tick_interval_seconds": 60,
                "timers": [
                    {
                        "id": "memory_consolidation",
                        "interval_seconds": 30,
                        "kind": "memory_consolidation",
                    },
                ],
            }),
        )
        assert _validate_passes(str(config_dir), str(schemas_dir), str(workflow_dir))


class TestTimersSchemaRejected:
    """Per RFC 0024 §Security Considerations — the schema rejects intervals
    below the ``_MIN_INTERVAL`` (1s) floor to prevent the busy-loop class.
    Defense-in-depth: ``EventLoop.register_timer`` also rejects at the API
    boundary (see ``test_event_loop_timers.py``)."""

    def test_interval_below_min_rejected(
        self,
        config_dir: Path,
        schemas_dir: Path,
        workflow_dir: Path,
    ) -> None:
        _write(
            config_dir,
            _wrap({
                "level": "semi-autonomous",
                "timers": [
                    {
                        "id": "too-fast",
                        "interval_seconds": 0.5,
                        "kind": "memory_consolidation",
                    },
                ],
            }),
        )
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir),
        )

    def test_zero_interval_rejected(
        self,
        config_dir: Path,
        schemas_dir: Path,
        workflow_dir: Path,
    ) -> None:
        _write(
            config_dir,
            _wrap({
                "level": "semi-autonomous",
                "timers": [
                    {
                        "id": "zero",
                        "interval_seconds": 0,
                        "kind": "any",
                    },
                ],
            }),
        )
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir),
        )

    def test_missing_required_fields_rejected(
        self,
        config_dir: Path,
        schemas_dir: Path,
        workflow_dir: Path,
    ) -> None:
        # Missing ``id``
        _write(
            config_dir,
            _wrap({
                "level": "semi-autonomous",
                "timers": [
                    {"interval_seconds": 60, "kind": "any"},
                ],
            }),
        )
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir),
        )

    def test_additional_properties_rejected(
        self,
        config_dir: Path,
        schemas_dir: Path,
        workflow_dir: Path,
    ) -> None:
        _write(
            config_dir,
            _wrap({
                "level": "semi-autonomous",
                "timers": [
                    {
                        "id": "extra",
                        "interval_seconds": 60,
                        "kind": "any",
                        "not_a_real_field": True,
                    },
                ],
            }),
        )
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir),
        )
