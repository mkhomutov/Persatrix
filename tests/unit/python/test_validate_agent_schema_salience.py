"""Validation tests for ``autonomy.salience_threshold`` and
``autonomy.salience_rate_max_per_sec`` — RFC 0024 PR 3b.

The two new keys land alongside ``autonomy.timers`` (PR 2) without
changing any existing accepted shape — they are additive optional keys
with defaults baked into ``EventLoop.DEFAULT_SALIENCE_*``.
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


def _write(config_dir: Path, data: dict) -> Path:
    p = config_dir / "agents.yaml"
    p.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    return p


class TestSalienceKeysAccepted:
    def test_threshold_in_unit_interval_accepted(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path,
    ) -> None:
        _write(config_dir, _wrap({
            "level": "reactive",
            "salience_threshold": 0.75,
        }))
        assert _validate_passes(str(config_dir), str(schemas_dir), str(workflow_dir))

    def test_rate_max_positive_accepted(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path,
    ) -> None:
        _write(config_dir, _wrap({
            "level": "reactive",
            "salience_rate_max_per_sec": 25,
        }))
        assert _validate_passes(str(config_dir), str(schemas_dir), str(workflow_dir))

    def test_both_salience_keys_with_timers_accepted(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path,
    ) -> None:
        """Salience knobs are independent of ``timers`` — both shapes coexist."""
        _write(config_dir, _wrap({
            "level": "semi-autonomous",
            "timers": [{
                "id": "memory_consolidation",
                "interval_seconds": 60,
                "kind": "memory_consolidation",
            }],
            "salience_threshold": 0.9,
            "salience_rate_max_per_sec": 5,
        }))
        assert _validate_passes(str(config_dir), str(schemas_dir), str(workflow_dir))

    def test_keys_omitted_accepted(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path,
    ) -> None:
        """The pre-PR-3b config shape is still valid — defaults pick up."""
        _write(config_dir, _wrap({"level": "reactive"}))
        assert _validate_passes(str(config_dir), str(schemas_dir), str(workflow_dir))


class TestSalienceKeysRejected:
    def test_threshold_above_one_rejected(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path,
    ) -> None:
        _write(config_dir, _wrap({
            "level": "reactive",
            "salience_threshold": 1.5,
        }))
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir),
        )

    def test_threshold_negative_rejected(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path,
    ) -> None:
        _write(config_dir, _wrap({
            "level": "reactive",
            "salience_threshold": -0.1,
        }))
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir),
        )

    def test_rate_max_zero_rejected(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path,
    ) -> None:
        """A zero cap would silently disable salience wakes — reject so the
        intent is explicit (``salience_threshold: 1.0`` is the disable knob)."""
        _write(config_dir, _wrap({
            "level": "reactive",
            "salience_rate_max_per_sec": 0,
        }))
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir),
        )
