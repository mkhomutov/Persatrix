"""Schema-content guards for ``schemas/agent.schema.json``.

These pin invariants the JSON-schema *validator* cannot enforce on itself —
notably that the prose ``provider`` description describes the **current**
selection contract. v0.3.4 made provider selection purely config/alias-driven
and removed the ``PERSATRIX_OFFLINE`` / ``PERSATRIX_OLLAMA`` global force-knobs;
the schema must not keep advertising the removed knobs (a stale schema doc would
ship a contradiction in a release whose whole theme is knob-free, config-driven
provider selection). Surviving env vars (``PERSATRIX_OFFLINE_RESPONSES`` /
``PERSATRIX_OLLAMA_MODEL`` / ``PERSATRIX_OLLAMA_BASE_URL``) are provider
*configuration*, documented in ``.env.example`` — not selection knobs, and not
here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
_AGENT_SCHEMA = _REPO_ROOT / "schemas" / "agent.schema.json"
_LLM_FACTORY = _REPO_ROOT / "agents" / "llm_factory.py"


def _agent_properties() -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(_AGENT_SCHEMA.read_text(encoding="utf-8"))
    props = schema["definitions"]["agent"]["properties"]
    assert isinstance(props, dict)
    return props


def _provider_property() -> dict[str, Any]:
    prop = _agent_properties()["provider"]
    assert isinstance(prop, dict)
    return prop


def _dispatched_providers() -> set[str]:
    """The provider names ``create_provider`` actually dispatches, parsed from its
    ``provider == "<name>"`` branches (agents/llm_factory.py).

    Derived from the factory source rather than a hand-maintained literal: the
    previous literal set silently drifted when the ``gemini`` and ``watsonx``
    branches were added (the enum and the assertion went stale *together*, so the
    guard passed while the two newest providers were missing from the enum). A
    source-derived expectation fails the guard on the next new branch until the
    enum is updated to match.
    """
    src = _LLM_FACTORY.read_text(encoding="utf-8")
    return set(re.findall(r'provider == "([a-z0-9_]+)"', src))


def test_provider_description_does_not_advertise_removed_force_knobs() -> None:
    desc: str = _provider_property()["description"]
    lowered = desc.lower()
    # The v0.3.4 provider-parity refactor removed these global force-knobs.
    assert "PERSATRIX_OFFLINE=1" not in desc
    assert "PERSATRIX_OLLAMA=1" not in desc
    assert "force-flag" not in lowered
    assert "force flag" not in lowered


def test_provider_enum_matches_dispatched_providers() -> None:
    # The enum must list exactly the providers create_provider dispatches
    # (agents/llm_factory.py). A drift here would let a config name a provider the
    # factory cannot build (or reject one it can) — so the expectation is derived
    # from the factory's own branches, which today are anthropic / openai / gemini
    # / watsonx / ollama / mock.
    assert set(_provider_property()["enum"]) == _dispatched_providers()


def test_provider_config_allows_every_key_the_providers_read() -> None:
    # The agent-level provider_config keeps additionalProperties:false as a typo
    # guard, so it must enumerate every key create_provider / the providers read
    # from provider_config — else a schema-valid deployment that sets a real key
    # (gemini's project/location/thinking_budget, watsonx's url/project_id/
    # space_id) is rejected by `make validate` even though the factory honours it.
    config_prop = _agent_properties()["provider_config"]
    assert config_prop.get("additionalProperties") is False
    declared = set(config_prop["properties"])
    required_keys = {
        "base_url",  # openai / ollama
        "project",
        "location",
        "thinking_budget",  # gemini
        "url",
        "project_id",
        "space_id",  # watsonx
    }
    missing = required_keys - declared
    assert not missing, f"provider_config schema is missing keys the code reads: {missing}"
