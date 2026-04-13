"""Tests for config validation (RFC 0005 PR 6a)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agents.validate import ValidationError, validate_config_dir

# ── Fixtures ────────────────────────────────────────────


@pytest.fixture()
def schemas_dir(tmp_path: Path) -> Path:
    """Copy real schemas into a temp directory."""
    real_schemas = Path("schemas")
    dest = tmp_path / "schemas"
    dest.mkdir()
    for schema_file in real_schemas.glob("*.json"):
        (dest / schema_file.name).write_text(schema_file.read_text(encoding="utf-8"), encoding="utf-8")
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


def _write_workflow_yaml(workflow_dir: Path, name: str, data: dict) -> Path:
    p = workflow_dir / name
    p.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    return p


# ── Valid configs ───────────────────────────────────────


_VALID_TASK_AGENT = {
    "schema_version": "0.2",
    "agents": [
        {
            "id": "code-writer",
            "type": "task",
            "name": "Code Writer",
            "role": "Writes code",
            "model": "claude-sonnet-4-20250514",
            "instructions": "You are a code writer.",
        }
    ],
}


_VALID_PERSONA_AGENT = {
    "schema_version": "0.2",
    "agents": [
        {
            "id": "sarah-chen",
            "type": "persona",
            "name": "Sarah Chen",
            "role": "VP of Engineering",
            "model": "claude-sonnet-4-20250514",
            "persona": {
                "title": "VP of Engineering",
                "background": "15 years experience.",
                "behavior": {
                    "directness": "direct",
                    "detail_focus": "big-picture",
                    "formality": "professional",
                    "risk_tolerance": "moderate",
                    "expressiveness": "reserved",
                },
            },
            "autonomy": {
                "level": "semi-autonomous",
                "tick_interval_seconds": 60,
                "max_actions_per_tick": 3,
                "idle_after_ticks": 10,
            },
            "memory": {
                "db_path": "data/memory.db",
            },
        }
    ],
}


_VALID_WORKFLOW = {
    "schema_version": "0.1",
    "workflow": {
        "id": "test-workflow",
        "name": "Test Workflow",
        "steps": [
            {
                "id": "step-1",
                "agent": "code-writer",
                "input": "Write hello world",
            }
        ],
    },
}


class TestValidTaskAgent:
    def test_valid_task_agent_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        _write_agents_yaml(config_dir, _VALID_TASK_AGENT)
        assert validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_valid_persona_agent_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        _write_agents_yaml(config_dir, _VALID_PERSONA_AGENT)
        assert validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_missing_instructions_for_task_agent_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "code-writer",
                    "type": "task",
                    "name": "Code Writer",
                    "role": "Writes code",
                    "model": "claude-sonnet-4-20250514",
                    # Missing 'instructions'
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_missing_persona_for_persona_agent_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "sarah-chen",
                    "type": "persona",
                    "name": "Sarah Chen",
                    "role": "VP",
                    "model": "claude-sonnet-4-20250514",
                    # Missing 'persona'
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )


class TestBehaviorDimensions:
    def test_invalid_directness_value_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "sarah-chen",
                    "type": "persona",
                    "name": "Sarah",
                    "role": "Lead",
                    "model": "claude-sonnet-4-20250514",
                    "persona": {
                        "title": "Lead",
                        "background": "Engineer.",
                        "behavior": {
                            "directness": "very-direct",  # invalid
                        },
                    },
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_omitted_behavior_dimensions_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """Omitted dimensions should default to middle values — valid."""
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "sarah-chen",
                    "type": "persona",
                    "name": "Sarah",
                    "role": "Lead",
                    "model": "claude-sonnet-4-20250514",
                    "persona": {
                        "title": "Lead",
                        "background": "Engineer.",
                        "behavior": {},  # All dimensions omitted → defaults
                    },
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_unknown_behavior_dimension_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "sarah-chen",
                    "type": "persona",
                    "name": "Sarah",
                    "role": "Lead",
                    "model": "claude-sonnet-4-20250514",
                    "persona": {
                        "title": "Lead",
                        "background": "Engineer.",
                        "behavior": {
                            "aggressiveness": "high",  # not a real dimension
                        },
                    },
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )


class TestAutonomyValidation:
    def test_invalid_autonomy_level_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "sarah-chen",
                    "type": "persona",
                    "name": "Sarah",
                    "role": "Lead",
                    "model": "claude-sonnet-4-20250514",
                    "persona": {
                        "title": "Lead",
                        "background": "Engineer.",
                        "behavior": {},
                    },
                    "autonomy": {
                        "level": "fully-autonomous",  # invalid
                    },
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_valid_autonomy_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "sarah-chen",
                    "type": "persona",
                    "name": "Sarah",
                    "role": "Lead",
                    "model": "claude-sonnet-4-20250514",
                    "persona": {
                        "title": "Lead",
                        "background": "Engineer.",
                        "behavior": {},
                    },
                    "autonomy": {
                        "level": "autonomous",
                        "tick_interval_seconds": 30,
                        "max_actions_per_tick": 5,
                        "idle_after_ticks": 20,
                    },
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )


class TestMemoryValidation:
    def test_invalid_retention_days_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "sarah-chen",
                    "type": "persona",
                    "name": "Sarah",
                    "role": "Lead",
                    "model": "claude-sonnet-4-20250514",
                    "persona": {
                        "title": "Lead",
                        "background": "Engineer.",
                        "behavior": {},
                    },
                    "memory": {
                        "episodic": {
                            "retention_days": 0,  # minimum is 1
                        },
                    },
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_invalid_decay_rate_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "sarah-chen",
                    "type": "persona",
                    "name": "Sarah",
                    "role": "Lead",
                    "model": "claude-sonnet-4-20250514",
                    "persona": {
                        "title": "Lead",
                        "background": "Engineer.",
                        "behavior": {},
                    },
                    "memory": {
                        "relationship": {
                            "decay_rate": 1.5,  # maximum is 1
                        },
                    },
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )


class TestAgentIdValidation:
    def test_invalid_agent_id_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "INVALID_ID",  # uppercase not allowed
                    "type": "task",
                    "name": "Bad Agent",
                    "role": "Does nothing",
                    "model": "claude-sonnet-4-20250514",
                    "instructions": "Noop.",
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )


class TestSafetyNetConditional:
    def test_persona_fields_without_type_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """Agent with persona/autonomy/memory but missing 'type' field."""
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "sarah-chen",
                    # Missing 'type' — safety net requires it
                    "name": "Sarah",
                    "role": "Lead",
                    "model": "claude-sonnet-4-20250514",
                    "persona": {
                        "title": "Lead",
                        "background": "Engineer.",
                        "behavior": {},
                    },
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )


class TestWorkflowValidation:
    def test_valid_workflow_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        _write_workflow_yaml(workflow_dir, "test.yaml", _VALID_WORKFLOW)
        assert validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_workflow_missing_steps_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = {
            "schema_version": "0.1",
            "workflow": {
                "id": "broken",
                "name": "Broken Workflow",
                # Missing 'steps'
            },
        }
        _write_workflow_yaml(workflow_dir, "broken.yaml", data)
        assert not validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )


class TestEdgeCases:
    def test_nonexistent_config_dir_fails(self, tmp_path: Path) -> None:
        assert not validate_config_dir(str(tmp_path / "no-such-dir"))

    def test_nonexistent_schemas_dir_fails(
        self, config_dir: Path, tmp_path: Path, workflow_dir: Path
    ) -> None:
        _write_agents_yaml(config_dir, _VALID_TASK_AGENT)
        assert not validate_config_dir(
            str(config_dir), str(tmp_path / "no-schemas"), str(workflow_dir)
        )

    def test_empty_config_dir_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        assert validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_yaml_parse_error_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        (config_dir / "agents.yaml").write_text(
            "invalid: yaml: [unterminated", encoding="utf-8"
        )
        assert not validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_empty_yaml_file_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """Empty YAML file (None content) should not crash."""
        (config_dir / "agents.yaml").write_text("", encoding="utf-8")
        assert validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )


class TestRealConfig:
    """Validate the actual project config to catch regressions."""

    def test_real_agents_yaml_passes(self) -> None:
        """The real config/agents.yaml must pass validation."""
        assert validate_config_dir("config/", "schemas/", "workflows/")


class TestValidationError:
    def test_str_with_path(self) -> None:
        err = ValidationError("agents.yaml", "bad value", "agents.0.id")
        assert "agents.yaml" in str(err)
        assert "agents.0.id" in str(err)
        assert "bad value" in str(err)

    def test_str_without_path(self) -> None:
        err = ValidationError("agents.yaml", "parse error")
        assert "agents.yaml" in str(err)
        assert "parse error" in str(err)


class TestMultipleAgents:
    def test_mixed_task_and_persona_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "code-writer",
                    "type": "task",
                    "name": "Writer",
                    "role": "Writes",
                    "model": "claude-sonnet-4-20250514",
                    "instructions": "Write code.",
                },
                {
                    "id": "sarah-chen",
                    "type": "persona",
                    "name": "Sarah",
                    "role": "Lead",
                    "model": "claude-sonnet-4-20250514",
                    "persona": {
                        "title": "Lead",
                        "background": "Engineer.",
                        "behavior": {"directness": "direct"},
                    },
                },
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_one_invalid_agent_fails_entire_file(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "code-writer",
                    "type": "task",
                    "name": "Writer",
                    "role": "Writes",
                    "model": "claude-sonnet-4-20250514",
                    "instructions": "Write code.",
                },
                {
                    "id": "bad-agent",
                    "type": "task",
                    "name": "Bad",
                    "role": "Bad",
                    "model": "claude-sonnet-4-20250514",
                    # Missing 'instructions'
                },
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )
