"""Tests for workflow/channel validation, edge cases, structured errors, and CLI interface."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest
import yaml

from agents.validate import ValidationError, validate_config_dir


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


def _write_workflow_yaml(workflow_dir: Path, name: str, data: dict) -> Path:
    p = workflow_dir / name
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


# ── Workflow validation ─────────────────────────────────


class TestWorkflowValidation:
    def test_valid_workflow_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        _write_workflow_yaml(workflow_dir, "test.yaml", _VALID_WORKFLOW)
        assert _validate_passes(
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
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_malformed_workflow_schema_reports_error(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """Malformed workflow schema JSON should report error, not crash.

        Mirrors the try/except in config schema loading (lines 101-104).
        Added per PR #56 review: workflow schema loading was the only
        json.loads() call without error handling.
        """
        _write_workflow_yaml(workflow_dir, "test.yaml", _VALID_WORKFLOW)
        (schemas_dir / "workflow.schema.json").write_text(
            "{invalid json", encoding="utf-8"
        )
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )


# ── Edge cases ──────────────────────────────────────────


class TestEdgeCases:
    def test_nonexistent_config_dir_fails(self, tmp_path: Path) -> None:
        assert not _validate_passes(str(tmp_path / "no-such-dir"))

    def test_nonexistent_schemas_dir_fails(
        self, config_dir: Path, tmp_path: Path, workflow_dir: Path
    ) -> None:
        _write_agents_yaml(config_dir, _VALID_TASK_AGENT)
        assert not _validate_passes(
            str(config_dir), str(tmp_path / "no-schemas"), str(workflow_dir)
        )

    def test_empty_config_dir_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        assert _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_yaml_parse_error_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        (config_dir / "agents.yaml").write_text(
            "invalid: yaml: [unterminated", encoding="utf-8"
        )
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_empty_yaml_file_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """Empty YAML file (None content) should not crash."""
        (config_dir / "agents.yaml").write_text("", encoding="utf-8")
        assert _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_missing_schema_file_reports_error(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """Missing schema file should report a clean error, not crash.

        Mirrors the graceful handling already used for workflow schemas
        (``if wf_schema_path.exists()``).  Covers the case where a schema
        referenced in ``_SCHEMA_MAP`` is accidentally deleted while the
        schemas directory still exists.
        """
        _write_agents_yaml(config_dir, _VALID_TASK_AGENT)
        (schemas_dir / "agent.schema.json").unlink()
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )

    def test_malformed_schema_file_reports_error(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """Malformed JSON schema should report error, not raise."""
        _write_agents_yaml(config_dir, _VALID_TASK_AGENT)
        (schemas_dir / "agent.schema.json").write_text(
            "{invalid json", encoding="utf-8"
        )
        assert not _validate_passes(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )


# ── Channels validation ─────────────────────────────────


class TestChannelsValidation:
    """F-6a-5: channels.yaml is in _SCHEMA_MAP and validated."""

    def test_valid_channels_yaml_passes(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = {
            "schema_version": "0.2",
            "channels": [
                {
                    "id": "general",
                    "type": "group",
                    "name": "General",
                    "members": "all",
                }
            ],
        }
        p = config_dir / "channels.yaml"
        p.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
        assert _validate_passes(str(config_dir), str(schemas_dir), str(workflow_dir))

    def test_invalid_channel_type_fails(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        data = {
            "schema_version": "0.2",
            "channels": [
                {
                    "id": "general",
                    "type": "invalid-type",
                    "name": "General",
                }
            ],
        }
        p = config_dir / "channels.yaml"
        p.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
        assert not _validate_passes(str(config_dir), str(schemas_dir), str(workflow_dir))


# ── Structured errors ───────────────────────────────────


class TestStructuredErrors:
    """F-6a-6: validate_config_dir returns structured error objects."""

    def test_errors_contain_message_and_file(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        """Returned ValidationError objects must carry file and message info."""
        data = {
            "schema_version": "0.2",
            "agents": [
                {
                    "id": "INVALID",
                    "type": "task",
                    "name": "Bad",
                    "role": "Noop",
                    "model": "gpt-4",
                    "instructions": "x",
                }
            ],
        }
        _write_agents_yaml(config_dir, data)
        ok, errors, _ = validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )
        assert not ok
        assert len(errors) >= 1
        assert any("INVALID" in e.message or "pattern" in e.message.lower() for e in errors)
        assert all("agents.yaml" in e.file for e in errors)

    def test_success_returns_empty_errors(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path
    ) -> None:
        _write_agents_yaml(config_dir, _VALID_TASK_AGENT)
        ok, errors, _ = validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir)
        )
        assert ok
        assert errors == []


# ── Real config ─────────────────────────────────────────


class TestRealConfig:
    """Validate the actual project config to catch regressions."""

    def test_real_agents_yaml_passes(self) -> None:
        """The real config/agents.yaml must pass validation.

        Uses absolute paths derived from this file's location so the test
        passes regardless of the working directory from which pytest is invoked.
        (PR review F-60-R10: relative paths fail when pytest run outside repo root.)
        """
        repo_root = Path(__file__).parent.parent.parent.parent
        assert _validate_passes(
            str(repo_root / "config"),
            str(repo_root / "schemas"),
            str(repo_root / "workflows"),
        )


# ── ValidationError ─────────────────────────────────────


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


# ── Files checked return ────────────────────────────────


class TestFilesCheckedReturn:
    """F-60-3: validate_config_dir returns files_checked count."""

    def test_empty_dir_returns_zero_files_checked(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path,
    ) -> None:
        """An empty config dir should return files_checked=0."""
        ok, errors, files_checked = validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir),
        )
        assert ok
        assert errors == []
        assert files_checked == 0

    def test_valid_config_returns_nonzero_files_checked(
        self, config_dir: Path, schemas_dir: Path, workflow_dir: Path,
    ) -> None:
        """A dir with valid config should return files_checked > 0."""
        _write_agents_yaml(config_dir, _VALID_TASK_AGENT)
        ok, errors, files_checked = validate_config_dir(
            str(config_dir), str(schemas_dir), str(workflow_dir),
        )
        assert ok
        assert files_checked >= 1

    def test_zero_files_checked_prints_warning(
        self, config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """PR review should-fix #4: verify the __main__ block prints
        'Warning: no config files found' when validate_config_dir returns
        files_checked=0 (empty directory).

        Exercises the actual __main__ code path via runpy so a message-text
        change in validate.py fails this test rather than silently passing.
        (F-6a-6: capsys assertion for 'no config files found' warning.)
        """
        monkeypatch.setattr(sys, "argv", ["validate.py", str(config_dir)])
        # run_module executes the __main__ block; exit(0) is raised since
        # files_checked=0 returns ok=True (no errors, no files).
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module("agents.validate", run_name="__main__")

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Warning: no config files found" in captured.out
        assert str(config_dir) in captured.out
