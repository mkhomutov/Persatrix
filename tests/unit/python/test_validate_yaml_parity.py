"""Tests that ``agents/validate.py`` reads every config file as Go does.

The orchestrator reads the same files with Go's YAML parser and regex
engine, which refuse a key given twice in one mapping, read only YAML 1.1,
and match `$` only at the end of the text. ``make validate`` holds each
file to the same reading, so it cannot pass a file the orchestrator
refuses.
"""

from __future__ import annotations

from pathlib import Path

from agents.validate import validate_config_dir

_SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"

_AGENTS = """\
schema_version: "0.2"
agents:
  - id: code-writer
    type: task
    name: Code Writer
    role: Writes code
    model: test-model
    instructions: Write code.
"""


def _errors(tmp_path: Path, agents_yaml: str) -> list[str]:
    config = tmp_path / "config"
    config.mkdir(parents=True)
    (config / "agents.yaml").write_text(agents_yaml, encoding="utf-8")
    ok, errors, _ = validate_config_dir(
        str(config), str(_SCHEMAS), workflow_dir=str(tmp_path / "no-workflows")
    )
    assert ok == (not errors)
    return [error.message for error in errors]


def test_a_well_formed_file_passes(tmp_path: Path) -> None:
    assert _errors(tmp_path, _AGENTS) == []


def test_a_key_given_twice_is_refused_in_any_file(tmp_path: Path) -> None:
    """PyYAML would keep the second `instructions`; Go refuses the file."""
    (message,) = _errors(tmp_path, _AGENTS + "    instructions: Write tests.\n")
    assert message.startswith("duplicate-key: 'instructions' is given twice"), message


def test_a_merged_key_may_be_overridden(tmp_path: Path) -> None:
    """A key a mapping gives itself overrides one `<<` merged into it, in
    both parsers; that is not a key given twice."""
    merged = _AGENTS.replace("  - id: code-writer\n", "  - &writer\n    id: code-writer\n")
    merged += "  - <<: *writer\n    id: test-writer\n    name: Test Writer\n"
    assert _errors(tmp_path, merged) == []


def test_a_yaml_version_other_than_1_1_is_refused(tmp_path: Path) -> None:
    """Go's YAML parser refuses every `%YAML` version but 1.1."""
    (message,) = _errors(tmp_path, "%YAML 1.2\n---\n" + _AGENTS)
    assert message.startswith("yaml-version: "), message
    assert _errors(tmp_path / "1.1", "%YAML 1.1\n---\n" + _AGENTS) == []


def test_a_pattern_refuses_a_final_line_break(tmp_path: Path) -> None:
    """Python's `$` matches before a final line break, Go's does not, so an
    id written as a block scalar, which ends in one, is refused."""
    block = _AGENTS.replace("  - id: code-writer\n", "  - id: |\n      code-writer\n")
    (message,) = _errors(tmp_path, block)
    assert message.startswith("'code-writer\\n' does not match "), message
    assert "line break" in message, message
