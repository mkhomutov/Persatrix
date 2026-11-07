"""
Configuration validator.

Validates YAML configs against JSON schemas in ``schemas/``.
Usage: python -m persatrix_agents.validate config/
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
import io
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import jsonschema  # type: ignore[import-untyped]
import yaml

# Map config filenames to their schema files.
# Only files with a known schema are validated; unknown YAML files are skipped.
# optimization.schema.json (RFC 0034 Phase 1 PR 2) is scoped to the
# conversation_window block only — the Go orchestrator owns the rest of
# the optimisation surface, so the schema leaves unknown top-level keys
# unconstrained.
# TODO: Add bridges.yaml, mcp-servers.yaml when JSON schemas are created
#       for them.
_SCHEMA_MAP: dict[str, str] = {
    "agents.yaml": "agent.schema.json",
    "channels.yaml": "channel.schema.json",
    "optimization.yaml": "optimization.schema.json",
    "organizations.yaml": "organization.schema.json",
    "security.yaml": "security.schema.json",
    "ui.yaml": "ui.schema.json",
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


class _Refusal(yaml.YAMLError):
    """A file PyYAML reads but Go's YAML parser refuses. The message starts
    with the name of the rule it breaks."""


class _GoLoader(yaml.SafeLoader):
    """The safe loader, refusing what Go's YAML parser refuses and PyYAML
    reads: a key given twice in one mapping, of which PyYAML keeps the last,
    and a ``%YAML`` version other than 1.1."""

    def process_directives(self) -> tuple[tuple[int, int] | None, dict[str, str] | None]:
        version, tags = super().process_directives()
        if version is not None and version != (1, 1):
            raise _Refusal(
                f"yaml-version: the file declares YAML {version[0]}.{version[1]}; "
                "Go's YAML parser reads only 1.1"
            )
        return version, tags

    def get_single_node(self) -> yaml.Node | None:
        node = super().get_single_node()
        if node is not None:
            _refuse_repeated_keys(node, set())
        return node


def _refuse_repeated_keys(node: yaml.Node, seen: set[int]) -> None:
    """Refuse a key given twice in one mapping, compared by its text as Go
    compares it. This runs before PyYAML merges any ``<<``, which counts by
    its text too, so a second ``<<`` is refused and a key that overrides a
    merged one is not."""
    if id(node) in seen:  # an alias: its node is checked once
        return
    seen.add(id(node))
    if isinstance(node, yaml.MappingNode):
        keys: set[str] = set()
        for key, value in node.value:
            if isinstance(key, yaml.ScalarNode):
                if key.value in keys:
                    raise _Refusal(
                        f"duplicate-key: {key.value!r} is given twice in one mapping "
                        f"(line {key.start_mark.line + 1}); YAML would keep only the last"
                    )
                keys.add(key.value)
            _refuse_repeated_keys(key, seen)
            _refuse_repeated_keys(value, seen)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _refuse_repeated_keys(item, seen)


@functools.cache
def _end_of_text(pattern: str) -> str:
    """``pattern`` with each ``$`` anchor outside a character class written
    ``\\Z``, which matches only at the end of the text."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern[i] == "\\":
            out.append(pattern[i : i + 2])
            i += 2
        elif pattern[i] == "[":
            # A class runs to the first `]` after an optional `^` and a
            # leading `]`, both of which belong to it.
            j = i + 1 + pattern.startswith("^", i + 1)
            j += pattern.startswith("]", j)
            while j < len(pattern) and pattern[j] != "]":
                j += 2 if pattern[j] == "\\" else 1
            out.append(pattern[i : j + 1])
            i = j + 1
        else:
            out.append(r"\Z" if pattern[i] == "$" else pattern[i])
            i += 1
    return "".join(out)


def _pattern(
    validator: Any, pattern: str, instance: Any, schema: dict[str, Any]
) -> Iterator[jsonschema.ValidationError]:
    """JSON Schema's ``pattern`` with ``$`` at the end of the text only, as in
    the standard's regexes and Go's. Python's ``$`` also matches before a
    final line break, so ``"acme\\n"`` would pass ``^[a-z]+$``."""
    if not validator.is_type(instance, "string") or re.search(_end_of_text(pattern), instance):
        return
    note = "; the text may not end in a line break" if re.search(pattern, instance) else ""
    yield jsonschema.ValidationError(f"{instance!r} does not match {pattern!r}{note}")


_Validator = jsonschema.validators.extend(jsonschema.Draft7Validator, {"pattern": _pattern})


def _org_validate() -> ModuleType:
    """The sibling ``org_validate`` module.

    ``make validate``, CI and ``persatrix validate`` run this file as a
    script, with no package set, so the module is loaded from its path: the
    script's folder is not on the import path under ``python -I``, ``-P``
    or PYTHONSAFEPATH. An import statement could not serve both ways, since
    mypy would resolve the module under two names.
    """
    if __package__:
        return importlib.import_module(".org_validate", __package__)
    path = Path(__file__).with_name("org_validate.py")
    spec = importlib.util.spec_from_file_location("org_validate", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["org_validate"] = module
    spec.loader.exec_module(module)
    return module


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
) -> tuple[Any, list[ValidationError]]:
    """Validate a single YAML file against a JSON schema: the file's
    content, and what is wrong with it."""
    errors: list[ValidationError] = []
    try:
        content = yaml.load(yaml_path.read_text(encoding="utf-8"), Loader=_GoLoader)
    except _Refusal as exc:
        errors.append(ValidationError(str(yaml_path), str(exc)))
        return None, errors
    except yaml.YAMLError as exc:
        errors.append(ValidationError(str(yaml_path), f"YAML parse error: {exc}"))
        return None, errors

    if content is None:
        # Empty file is valid (no data to validate)
        return content, errors

    validator = _Validator(schema)
    for error in sorted(validator.iter_errors(content), key=lambda e: list(e.absolute_path)):
        json_path = ".".join(str(p) for p in error.absolute_path)
        # A subschema may name the rule it enforces (`x-refusal`), so the
        # message says why the value is refused, not which keyword caught it.
        refusal = error.schema.get("x-refusal") if isinstance(error.schema, dict) else None
        errors.append(ValidationError(str(yaml_path), refusal or error.message, json_path))

    return content, errors


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
        content, errors = _validate_yaml_against_schema(yaml_path, schema)
        if filename == "organizations.yaml" and content is not None and not errors:
            # RFC 0012: the graph rules a JSON Schema cannot express.
            graph = _org_validate().check_document(content, config_path / "agents.yaml")
            errors = [ValidationError(str(yaml_path), e.message, e.path) for e in graph]
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
                    _, errors = _validate_yaml_against_schema(yaml_file, wf_schema)
                    all_errors.extend(errors)
                    files_checked += 1

    if files_checked == 0:
        return True, [], 0

    return (len(all_errors) == 0), all_errors, files_checked


if __name__ == "__main__":
    # `persatrix validate` reads this output as UTF-8. On Windows a piped
    # stdout would use the ANSI code page, which cannot print every
    # character a message can hold, such as a cycle's arrows.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
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
