"""Schema-level validation tests for instructions_file constraints.

Split out of ``test_validate_agent_schema.py`` to keep both files under
the 500-line policy (see scripts/checks/file_size.py).  Concerns isolated
here: validate-time enforcement of the instructions / instructions_file
contract — the prompts/-subtree pattern, the task-type oneOf rule, and
the persona-type rejection of both fields.

Runtime-side resolution of the same field is covered by
``test_prompt_loader.py`` and
``test_server_load_agent_instructions_file.py``.
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
    """Copy real schemas into a temp directory."""
    real_schemas = Path("schemas")
    dest = tmp_path / "schemas"
    dest.mkdir()
    for schema_file in real_schemas.glob("*.json"):
        (dest / schema_file.name).write_text(
            schema_file.read_text(encoding="utf-8"), encoding="utf-8",
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


class TestTaskInstructionsFile:
    """Schema rules for task agents that use instructions_file."""

    def test_task_agent_with_instructions_file_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """A task agent that uses ``instructions_file`` instead of
        ``instructions`` must pass schema validation — the schema accepts
        either field."""
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "planner",
                    "type": "task",
                    "name": "Planner",
                    "role": "Plans things",
                    "model": "claude-sonnet-4-20250514",
                    "instructions_file": "prompts/runtime/task-agents/planner.md",
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_task_agent_with_both_instructions_and_file_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """A task agent that sets both ``instructions`` and
        ``instructions_file`` must be rejected at validate time.

        The schema's task-type then-clause uses ``oneOf`` so that
        mutually-exclusive fields surface during ``make validate`` instead
        of waiting for first-startup.  The runtime loader still enforces
        the same rule as defense-in-depth (see test_prompt_loader.py
        ``test_inline_and_file_set_raises``).
        """
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "planner",
                    "type": "task",
                    "name": "Planner",
                    "role": "Plans things",
                    "model": "claude-sonnet-4-20250514",
                    "instructions": "inline",
                    "instructions_file": "prompts/runtime/task-agents/planner.md",
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_instructions_file_outside_prompts_subtree_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """``instructions_file`` paths that don't start with ``prompts/``
        must be rejected at validate time.

        The schema's ``pattern: "^prompts/"`` is defense-in-depth: the
        runtime loader's ``relative_to(prompts_root)`` check is the
        authoritative deny-by-default control, but catching it at schema
        time gives operators a clearer error before the agent server boots.
        """
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "planner",
                    "type": "task",
                    "name": "Planner",
                    "role": "Plans things",
                    "model": "claude-sonnet-4-20250514",
                    "instructions_file": "/etc/passwd",
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )


class TestPersonaRejectsTaskFields:
    """Persona agents must not declare task-only prompt fields."""

    def test_persona_agent_with_instructions_file_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """Persona agents must not declare ``instructions_file``.

        ``create_persona_agent`` does not consume the field, so without
        this constraint a misconfiguration would be silently dropped at
        runtime.  Schema-level rejection makes the operator footgun
        impossible.
        """
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "ember-owl",
                    "type": "persona",
                    "name": "Ember Owl",
                    "role": "VP",
                    "model": "claude-sonnet-4-20250514",
                    "persona": {
                        "title": "VP",
                        "background": "...",
                        "behavior": {},
                    },
                    "instructions_file": "prompts/runtime/task-agents/x.md",
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_persona_agent_with_inline_instructions_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """Persona agents must not declare inline ``instructions`` either.

        Same rationale as ``instructions_file``: silently dropped by
        ``create_persona_agent``.  Both task-only fields are forbidden on
        persona agents at the schema level.
        """
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "ember-owl",
                    "type": "persona",
                    "name": "Ember Owl",
                    "role": "VP",
                    "model": "claude-sonnet-4-20250514",
                    "persona": {
                        "title": "VP",
                        "background": "...",
                        "behavior": {},
                    },
                    "instructions": "this should not be here",
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )
