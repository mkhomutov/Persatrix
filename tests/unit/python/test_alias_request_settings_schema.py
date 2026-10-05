"""``make validate`` checks the request settings an alias's provider_config
carries (ISSUE-0169).

The settings reach the vendor on every call, and a misspelt value (``effort:
lo``) would only surface as a 400 on the first request. The optimization
schema names each setting the adapters read and the values it takes, so the
config check catches it first. Other keys stay open: provider_config is a
passthrough, and connection settings (``base_url``, ``project_id``) differ by
provider.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]
import pytest

_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[3] / "schemas" / "optimization.schema.json").read_text(),
)


def _errors(provider_config: dict[str, Any]) -> list[str]:
    doc = {
        "schema_version": "0.3",
        "models": {"aliases": {"quality": {
            "provider": "anthropic", "model": "m",
            "input_per_1m_tokens": 1, "output_per_1m_tokens": 1,
            "provider_config": provider_config,
        }}},
    }
    validator = jsonschema.Draft7Validator(_SCHEMA)
    return [e.message for e in validator.iter_errors(doc)]


@pytest.mark.parametrize("config", [
    {"effort": "low"},
    {"effort": "max", "thinking": "adaptive", "prompt_cache": True},
    {"thinking": "between_tools"},
    {"reasoning_effort": "none"},
    {"thinking_level": "minimal"},
    {"thinking_budget": 0},
    {"base_url": "http://localhost:11434/v1", "project_id": ""},
])
def test_valid_settings_pass(config: dict[str, Any]) -> None:
    assert _errors(config) == []


@pytest.mark.parametrize("config", [
    {"effort": "lo"},
    {"thinking": "enabled"},
    {"prompt_cache": "yes"},
    {"reasoning_effort": "extreme"},
    {"thinking_level": "none"},
    {"thinking_budget": "0"},
])
def test_a_bad_value_fails(config: dict[str, Any]) -> None:
    assert _errors(config)
