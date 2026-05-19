"""Persona-grounding clause regression (v0.3.0 test findings — PR 5, F-2).

The bug: a persona replied to a user prompt by adopting the user's own
name as a first-person role (e.g. *"Hey! I'm Alex, Staff Engineer…"*).
The fix: a grounding clause in the assembled persona system prompt that
tells the model it is not the user.

This is the **primary, deterministic** regression check named in the
[v0.3.0 channel test-findings PR plan §PR 5][pr-plan]: the prompt-assembly
invariant pins what the runtime emits regardless of whether the model
honors the clause downstream. An optional integration test in
``tests/integration/`` exercises the model-output side at the
``@pytest.mark.integration`` gate.

[pr-plan]:
../../../docs/v0.3.0-test-findings-pr-plan.md#pr-5-fixv030-channel-persona-impersonation--grounding-the-persona-system-prompt

The clause is loaded as a persona section (RFC 0022 templating contract)
from ``prompts/runtime/persona/sections/grounding.md`` and rendered with
the persona's ``{name}`` interpolated so the invariant is concrete per
persona rather than a generic "you are not the user" line.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from agents.persona import create_persona_agent

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTS_YAML = REPO_ROOT / "config" / "agents.yaml"


def _shipped_persona_configs() -> list[dict[str, Any]]:
    """Load every ``type: persona`` agent from ``config/agents.yaml``.

    Sourced from the production config so a future persona added to the
    shipped roster automatically gets the grounding-clause assertion
    without anyone remembering to update this test.
    """
    with AGENTS_YAML.open("rb") as fh:
        doc = yaml.safe_load(fh)
    return [a for a in doc.get("agents", []) if a.get("type") == "persona"]


class TestGroundingClausePresence:
    """The grounding clause renders verbatim in every persona's system prompt."""

    async def _make_agent(self, config: dict):
        agent = create_persona_agent(
            agent_id=config["id"], config=config, llm_client=_make_client(),
        )
        await agent.initialize_memory()
        return agent

    @pytest.mark.parametrize(
        "persona_cfg",
        _shipped_persona_configs(),
        ids=lambda c: c["id"],
    )
    async def test_shipped_persona_carries_grounding_clause(
        self, persona_cfg: dict[str, Any],
    ) -> None:
        # ``config/agents.yaml`` omits ``memory.db_path`` for personas that
        # use the production default; pin to ``:memory:`` so the test does
        # not touch the on-disk DB.
        cfg = deepcopy(persona_cfg)
        cfg.setdefault("memory", {})["db_path"] = ":memory:"
        agent = await self._make_agent(cfg)
        try:
            prompt = agent._build_system_prompt()
            name = cfg["name"]
            # The grounding clause MUST be verbatim — drift in this string
            # is a behavior change worth a deliberate review, not an
            # incidental edit.
            expected = (
                f"You are {name}, and you are not the user. "
                "If the user tells you their name or addresses you by a "
                "name, treat that as their name (or someone else's) — "
                f"never as a role for you to adopt. Reply as {name}. "
                "Never open a reply with \"I'm <user-name>\" or "
                "otherwise speak as the user."
            )
            assert expected in prompt, (
                f"grounding clause missing or drifted for persona "
                f"{cfg['id']!r}; rendered prompt:\n{prompt}"
            )
        finally:
            await agent.close_memory()

    async def test_grounding_clause_is_above_persona_config_sections(
        self,
    ) -> None:
        # The PR plan §PR 5 requires the clause "near the top" so the
        # model encounters it before the persona-config sections that
        # describe voice / quirks / goals.  Concretely: above
        # ``Background:``, ``Quirks:``, ``Goals:``, ``Current state:``.
        cfg = deepcopy(_PERSONA_CONFIG)
        agent = await self._make_agent(cfg)
        try:
            prompt = agent._build_system_prompt()
            grounding_idx = prompt.index("you are not the user")
            for marker in ("Background:", "Quirks:", "Goals:", "Current state:"):
                assert marker in prompt, f"missing section header {marker!r}"
                assert grounding_idx < prompt.index(marker), (
                    f"grounding clause ({grounding_idx}) must appear "
                    f"before {marker!r} ({prompt.index(marker)})"
                )
        finally:
            await agent.close_memory()

    async def test_grounding_uses_persona_name_not_a_constant(self) -> None:
        # Belt-and-braces: a future refactor that accidentally hard-codes
        # one persona's name into the section template would silently
        # mis-ground every other persona.  Render two distinct names and
        # assert each prompt carries its own.
        cfg_a = deepcopy(_PERSONA_CONFIG)
        cfg_a["name"] = "Alpha One"
        cfg_b = deepcopy(_PERSONA_CONFIG)
        cfg_b["id"] = "beta-two"
        cfg_b["name"] = "Beta Two"

        agent_a = await self._make_agent(cfg_a)
        agent_b = await self._make_agent(cfg_b)
        try:
            prompt_a = agent_a._build_system_prompt()
            prompt_b = agent_b._build_system_prompt()
            assert "You are Alpha One, and you are not the user." in prompt_a
            assert "You are Beta Two, and you are not the user." in prompt_b
            assert "Alpha One" not in prompt_b
            assert "Beta Two" not in prompt_a
        finally:
            await agent_a.close_memory()
            await agent_b.close_memory()
