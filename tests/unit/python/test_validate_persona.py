"""Tests for persona-specific validation.

Covers behavior, autonomy, memory, additional properties, and range
constraints.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agents.validate import validate_config_dir


def _validate_passes(*args: str) -> bool:
    ok, _, _ = validate_config_dir(*args)
    return ok


# ── Fixtures ────────────────────────────────────────────


@pytest.fixture()
def schemas_dir(tmp_path: Path) -> Path:
    """Copy real schemas into a temp directory."""
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


def _write_agents_yaml(config_dir: Path, data: dict) -> Path:
    p = config_dir / "agents.yaml"
    p.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    return p


def _persona_agent(extra: dict | None = None) -> dict:
    agent: dict = {
        "id": "ember-owl",
        "type": "persona",
        "name": "Sarah",
        "role": "Lead",
        "model": "claude-sonnet-4-20250514",
        "persona": {
            "title": "Lead",
            "background": "Engineer.",
            "behavior": {},
        },
    }
    if extra:
        agent.update(extra)
    return {"schema_version": "0.2", "agents": [agent]}


# ── Behavior dimensions ─────────────────────────────────


class TestBehaviorDimensions:
    def test_invalid_directness_value_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = _persona_agent()
        data["agents"][0]["persona"]["behavior"]["directness"] = "very-direct"  # invalid
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_omitted_behavior_dimensions_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """Omitted dimensions should default to middle values — valid."""
        data = _persona_agent()
        _write_agents_yaml(config_dir, data)
        assert _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_unknown_behavior_dimension_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = _persona_agent()
        data["agents"][0]["persona"]["behavior"]["aggressiveness"] = "high"  # not a real dimension
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )


# ── Autonomy validation ─────────────────────────────────


class TestAutonomyValidation:
    def test_invalid_autonomy_level_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = _persona_agent({"autonomy": {"level": "fully-autonomous"}})  # invalid
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_valid_autonomy_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = _persona_agent({
            "autonomy": {
                "level": "autonomous",
                "tick_interval_seconds": 30,
                "max_actions_per_tick": 5,
                "idle_after_ticks": 20,
            }
        })
        _write_agents_yaml(config_dir, data)
        assert _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )


# ── Memory validation ───────────────────────────────────


class TestMemoryValidation:
    def test_invalid_retention_days_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = _persona_agent({"memory": {"episodic": {"retention_days": 0}}})  # minimum is 1
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_invalid_decay_rate_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = _persona_agent({"memory": {"relationship": {"decay_rate": 1.5}}})  # maximum is 1
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )


# ── Persona additional properties ──────────────────────


class TestPersonaAdditionalProperties:
    """Validates additionalProperties: false on persona definition.

    Without this constraint, typos in persona-level fields (e.g.
    ``bckground`` instead of ``background``) silently pass validation.
    Added per PR #56 review: every other schema definition already had
    ``additionalProperties: false`` — persona was the only gap.
    """

    def test_persona_unknown_field_rejected(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = _persona_agent()
        data["agents"][0]["persona"]["extra_field"] = "should be rejected"
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_persona_typo_in_background_rejected(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """Realistic typo: ``bckground`` instead of ``background``."""
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "ember-owl",
                    "type": "persona",
                    "name": "Sarah",
                    "role": "Lead",
                    "model": "claude-sonnet-4-20250514",
                    "persona": {
                        "title": "Lead",
                        "bckground": "Engineer.",  # typo
                        "behavior": {},
                    },
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )


# ── Range constraints ───────────────────────────────────


class TestRangeConstraints:
    """Validates schema minimum/maximum constraints on numeric fields."""

    def test_trust_level_above_max_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """relationship.trust_level has maximum: 1 in schema."""
        data = _persona_agent({
            "relationships": [
                {
                    "agent_id": "iron-fox",
                    "type": "peer",
                    "trust_level": 1.5,  # max is 1.0
                }
            ]
        })
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_temperature_above_max_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """temperature has maximum: 2 in schema."""
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "code-writer",
                    "type": "task",
                    "name": "Code Writer",
                    "role": "Writes code",
                    "model": "claude-sonnet-4-20250514",
                    "instructions": "You are a code writer.",
                    "temperature": 3.0,  # max is 2
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_autonomy_typo_rejected(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """Typo in autonomy field name should be caught by additionalProperties: false."""
        data = _persona_agent({
            "autonomy": {
                "level": "semi-autonomous",
                "tic_interval_seconds": 30,  # typo: should be tick_interval_seconds
            }
        })
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_memory_typo_rejected(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """Typo in memory field name should be caught by additionalProperties: false."""
        data = _persona_agent({
            "memory": {
                "episodic": {
                    "retaintion_days": 90,  # typo: should be retention_days
                },
            }
        })
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )
