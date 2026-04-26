"""Tests for agent schema validation — types, IDs, safety net, multi-agent configs."""

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
            "id": "ember-owl",
            "type": "persona",
            "name": "Ember Owl",
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


# ── Valid agent types ───────────────────────────────────


class TestValidTaskAgent:
    def test_valid_task_agent_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        _write_agents_yaml(config_dir, _VALID_TASK_AGENT)
        assert _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_valid_persona_agent_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        _write_agents_yaml(config_dir, _VALID_PERSONA_AGENT)
        assert _validate_passes(
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
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_missing_persona_for_persona_agent_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "ember-owl",
                    "type": "persona",
                    "name": "Ember Owl",
                    "role": "VP",
                    "model": "claude-sonnet-4-20250514",
                    # Missing 'persona'
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    # instructions_file schema constraints (positive and negative) are
    # covered in test_validate_agent_schema_instructions_file.py (split
    # for the 500-line policy).

    def test_v01_agent_without_type_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """v0.1 agent (no type/instructions/persona) must pass validation.

        The schema's allOf conditionals gate on ``required: ["type"]`` so
        that v0.1 agents — which omit ``type`` entirely — skip the
        persona/task conditionals and validate with only base required
        fields.  A regression here would break ``make validate`` for all
        legacy v0.1 configurations.
        """
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "legacy-agent",
                    "name": "Legacy",
                    "role": "Does stuff",
                    "model": "claude-sonnet-4-20250514",
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )


# ── Agent ID validation ─────────────────────────────────


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
        assert not _validate_passes(
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
                    "id": "ember-owl",
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
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )


# ── Agent ID regex ──────────────────────────────────────


class TestAgentIdRegex:
    """Agent ID regex requires minimum 2 characters per spec (^[a-z0-9][a-z0-9-]*[a-z0-9]$)."""

    def test_single_char_id_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "a",
                    "type": "task",
                    "name": "Agent A",
                    "role": "Test",
                    "model": "gpt-4",
                    "instructions": "x",
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(str(config_dir), str(schemas_dir), str(workflow_dir))

    def test_two_char_id_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "ab",
                    "type": "task",
                    "name": "Agent AB",
                    "role": "Test",
                    "model": "gpt-4",
                    "instructions": "x",
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert _validate_passes(str(config_dir), str(schemas_dir), str(workflow_dir))

    def test_trailing_hyphen_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "agent-",
                    "type": "task",
                    "name": "Bad",
                    "role": "Test",
                    "model": "gpt-4",
                    "instructions": "x",
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(str(config_dir), str(schemas_dir), str(workflow_dir))


# ── Multi-agent configs ─────────────────────────────────


class TestMultiAgentConfig:
    """Validates allOf conditionals work across array items with mixed types."""

    def test_mixed_task_and_persona_agents_pass(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """Multiple agents with different types in one file should pass.

        Real-world ``config/agents.yaml`` has 3 task + 1 persona agents.
        This validates the allOf conditionals don't interfere with each
        other across array items.
        """
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
                },
                {
                    "id": "ember-owl",
                    "type": "persona",
                    "name": "Ember Owl",
                    "role": "VP of Engineering",
                    "model": "claude-sonnet-4-20250514",
                    "persona": {
                        "title": "VP of Engineering",
                        "background": "15 years experience.",
                        "behavior": {"directness": "direct"},
                    },
                },
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_one_invalid_agent_fails_whole_file(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """A valid task agent + an invalid persona agent (missing persona)
        should fail validation for the file."""
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
                },
                {
                    "id": "ember-owl",
                    "type": "persona",
                    "name": "Ember Owl",
                    "role": "VP",
                    "model": "claude-sonnet-4-20250514",
                    # Missing 'persona'
                },
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )
