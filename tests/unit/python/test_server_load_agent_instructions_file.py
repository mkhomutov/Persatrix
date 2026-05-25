"""Tests for instructions_file resolution inside load_agent().

Split out of ``test_server_load_agent.py`` to keep both files under the
500-line policy (see scripts/checks/file_size.py).  Concerns isolated
here: how load_agent() reads ``instructions_file`` references, swaps the
resolved text into the agent config, and surfaces failures.

Schema-level acceptance/rejection of the same field lives in
``test_validate_agent_schema.py`` and
``test_validate_agent_schema_instructions_file.py``.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from agents.server_persona import load_agent
from agents.task_agent import TaskAgent


class TestInstructionsFileLoading:
    """Verify instructions_file references are resolved at load time."""

    def _layout(self, tmp: str) -> Path:
        """Create the <repo>/config and <repo>/prompts/runtime/task-agents layout."""
        root = Path(tmp)
        (root / "config").mkdir()
        (root / "prompts" / "runtime" / "task-agents").mkdir(parents=True)
        return root

    @patch("agents.server_persona.create_provider")
    def test_instructions_file_resolves_to_file_contents(self, mock_create):
        mock_create.return_value = (MagicMock(), "test-model")
        with tempfile.TemporaryDirectory() as tmp:
            root = self._layout(tmp)
            (root / "prompts" / "runtime" / "task-agents" / "planner.md").write_text(
                "You are a planner.\n", encoding="utf-8",
            )
            config_path = root / "config" / "agents.yaml"
            config_path.write_text(
                yaml.dump({
                    "schema_version": "0.2",
                    "agents": [
                        {
                            "id": "planner",
                            "type": "task",
                            "name": "Planner",
                            "role": "Plans things",
                            "model": "test-model",
                            "instructions_file": "prompts/runtime/task-agents/planner.md",
                            "permissions": {},
                        },
                    ],
                }),
                encoding="utf-8",
            )
            agent = load_agent(
                "planner",
                str(config_path),
                str(root / "workspace"),
                repo_root=root,
            )
            assert isinstance(agent, TaskAgent)
            assert agent.config["instructions"] == "You are a planner.\n"
            # The file reference is replaced — not duplicated — on the loaded
            # agent config so downstream code sees a single source of truth.
            assert "instructions_file" not in agent.config

    @patch("agents.server_persona.create_provider")
    def test_instructions_file_missing_fails_clearly(self, mock_create):
        mock_create.return_value = (MagicMock(), "test-model")
        with tempfile.TemporaryDirectory() as tmp:
            root = self._layout(tmp)
            config_path = root / "config" / "agents.yaml"
            config_path.write_text(
                yaml.dump({
                    "schema_version": "0.2",
                    "agents": [
                        {
                            "id": "planner",
                            "type": "task",
                            "name": "Planner",
                            "role": "Plans things",
                            "model": "test-model",
                            "instructions_file": "prompts/runtime/task-agents/nope.md",
                            "permissions": {},
                        },
                    ],
                }),
                encoding="utf-8",
            )
            with pytest.raises(SystemExit, match="not found"):
                load_agent(
                    "planner",
                    str(config_path),
                    str(root / "workspace"),
                    repo_root=root,
                )

    @patch("agents.server_persona.create_provider")
    def test_inline_instructions_still_work(self, mock_create):
        """Backward compat: agents using the inline ``instructions`` field load unchanged."""
        mock_create.return_value = (MagicMock(), "test-model")
        with tempfile.TemporaryDirectory() as tmp:
            root = self._layout(tmp)
            config_path = root / "config" / "agents.yaml"
            config_path.write_text(
                yaml.dump({
                    "schema_version": "0.2",
                    "agents": [
                        {
                            "id": "planner",
                            "type": "task",
                            "name": "Planner",
                            "role": "Plans things",
                            "model": "test-model",
                            "instructions": "Inline plan.",
                            "permissions": {},
                        },
                    ],
                }),
                encoding="utf-8",
            )
            agent = load_agent("planner", str(config_path), str(root / "workspace"))
            assert agent.config["instructions"] == "Inline plan."

    @patch("agents.server_persona.create_provider")
    def test_both_inline_and_file_rejected(self, mock_create):
        mock_create.return_value = (MagicMock(), "test-model")
        with tempfile.TemporaryDirectory() as tmp:
            root = self._layout(tmp)
            (root / "prompts" / "runtime" / "task-agents" / "p.md").write_text(
                "from file", encoding="utf-8",
            )
            config_path = root / "config" / "agents.yaml"
            config_path.write_text(
                yaml.dump({
                    "schema_version": "0.2",
                    "agents": [
                        {
                            "id": "planner",
                            "type": "task",
                            "name": "Planner",
                            "role": "Plans things",
                            "model": "test-model",
                            "instructions": "inline",
                            "instructions_file": "prompts/runtime/task-agents/p.md",
                            "permissions": {},
                        },
                    ],
                }),
                encoding="utf-8",
            )
            with pytest.raises(SystemExit, match="mutually exclusive"):
                load_agent(
                    "planner",
                    str(config_path),
                    str(root / "workspace"),
                    repo_root=root,
                )
