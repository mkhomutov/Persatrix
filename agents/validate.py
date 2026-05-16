"""
Configuration validator.

Validates YAML configs against JSON schemas in ``schemas/``.
Usage: python -m persatrix_agents.validate config/
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]
import yaml

# Map config filenames to their schema files.
# Only files with a known schema are validated; unknown YAML files are skipped.
# optimization.schema.json (RFC 0034 Phase 1 PR 2) is scoped to the
# conversation_window block only — the Go orchestrator owns the rest of
# the optimisation surface, so the schema leaves unknown top-level keys
# unconstrained.
# TODO: Add organizations.yaml, bridges.yaml, mcp-servers.yaml when JSON
#       schemas are created for them.
_SCHEMA_MAP: dict[str, str] = {
    "agents.yaml": "agent.schema.json",
    "channels.yaml": "channel.schema.json",
    "optimization.yaml": "optimization.schema.json",
}

# Workflow files live in a separate directory and share one schema.
_WORKFLOW_SCHEMA = "workflow.schema.json"


class ValidationError:
    """Single validation failure with context."""

    def __init__(self, file: str, message: str, path: str = "") -> None:
        self.file = file
        self.message = message
        self.path = path

    def __str__(self) -> str:
        loc = f"{self.file}"
        if self.path:
            loc += f" at $.{self.path}"
        return f"  ERROR: {loc}: {self.message}"


def _load_schema(schemas_dir: Path, schema_file: str) -> dict[str, Any]:
    """Load and parse a JSON schema file."""
    schema_path = schemas_dir / schema_file
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema not found: {schema_path}")
    result: dict[str, Any] = json.loads(schema_path.read_text(encoding="utf-8"))
    return result


def _validate_yaml_against_schema(
    yaml_path: Path,
    schema: dict,
) -> list[ValidationError]:
    """Validate a single YAML file against a JSON schema."""
    errors: list[ValidationError] = []
    try:
        content = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        errors.append(ValidationError(str(yaml_path), f"YAML parse error: {exc}"))
        return errors

    if content is None:
        # Empty file is valid (no data to validate)
        return errors

    validator = jsonschema.Draft7Validator(schema)
    for error in sorted(validator.iter_errors(content), key=lambda e: list(e.absolute_path)):
        json_path = ".".join(str(p) for p in error.absolute_path)
        errors.append(ValidationError(str(yaml_path), error.message, json_path))

    return errors


def validate_config_dir(
    config_dir: str,
    schemas_dir: str = "schemas/",
    workflow_dir: str | None = None,
) -> tuple[bool, list[ValidationError], int]:
    """Validate all YAML files in a config directory.

    Returns a tuple of (success, errors, files_checked). ``success`` is True
    when all files pass validation. ``errors`` contains structured error
    objects for programmatic consumption. ``files_checked`` is the number
    of files that were actually validated.
    (F-6a-1: structured validation return.)
    (F-60-3: include files_checked to detect empty-dir edge case.)
    """
    config_path = Path(config_dir)
    if not config_path.exists():
        err = ValidationError(config_dir, f"Config directory not found: {config_dir}")
        return False, [err], 0

    schemas_path = Path(schemas_dir)
    if not schemas_path.exists():
        err = ValidationError(schemas_dir, f"Schemas directory not found: {schemas_dir}")
        return False, [err], 0

    all_errors: list[ValidationError] = []
    files_checked = 0

    # Validate config files with known schemas
    for filename, schema_file in _SCHEMA_MAP.items():
        yaml_path = config_path / filename
        if not yaml_path.exists():
            continue
        try:
            schema = _load_schema(schemas_path, schema_file)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            all_errors.append(ValidationError(filename, f"Schema error: {exc}"))
            files_checked += 1
            continue
        errors = _validate_yaml_against_schema(yaml_path, schema)
        all_errors.extend(errors)
        files_checked += 1

    # Validate workflow files
    wf_path = Path(workflow_dir) if workflow_dir else config_path.parent / "workflows"
    if wf_path.exists():
        wf_schema_path = schemas_path / _WORKFLOW_SCHEMA
        if wf_schema_path.exists():
            try:
                wf_schema = json.loads(wf_schema_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                all_errors.append(ValidationError(str(wf_schema_path), f"Schema error: {exc}"))
                files_checked += 1
            else:
                for yaml_file in sorted(wf_path.glob("*.yaml")):
                    errors = _validate_yaml_against_schema(yaml_file, wf_schema)
                    all_errors.extend(errors)
                    files_checked += 1

    if files_checked == 0:
        return True, [], 0

    return (len(all_errors) == 0), all_errors, files_checked


if __name__ == "__main__":
    config_dir = sys.argv[1] if len(sys.argv) > 1 else "config/"
    success, errors, files_checked = validate_config_dir(config_dir)
    if errors:
        print(f"Validation failed \u2014 {len(errors)} error(s):\n")
        for error in errors:
            print(error)
    elif files_checked == 0:
        # (F-60-3: distinguish "no files found" from "all files valid".)
        print(f"Warning: no config files found in {config_dir}")
    elif success:
        print(f"Validation passed ({files_checked} file(s) checked)")
    sys.exit(0 if success else 1)
