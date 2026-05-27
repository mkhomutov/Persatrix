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
from pathlib import Path
from typing import Any

_AGENT_SCHEMA = Path(__file__).resolve().parents[3] / "schemas" / "agent.schema.json"


def _provider_property() -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(_AGENT_SCHEMA.read_text(encoding="utf-8"))
    prop = schema["definitions"]["agent"]["properties"]["provider"]
    assert isinstance(prop, dict)
    return prop


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
    # (agents/llm_factory.py): anthropic / openai / ollama / mock. A drift here
    # would let a config name a provider the factory cannot build (or vice versa).
    assert set(_provider_property()["enum"]) == {"anthropic", "openai", "ollama", "mock"}
