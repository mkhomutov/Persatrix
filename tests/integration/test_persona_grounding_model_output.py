"""Probabilistic model-output check for the persona-grounding clause.

Opt-in companion to ``tests/unit/python/test_persona_grounding.py``
(v0.3.0 channel test findings PR plan §PR 5, F-2).

**Status (per PR plan)**: optional, probabilistic. A failure here is a
signal to strengthen the grounding clause in
``prompts/runtime/persona/sections/grounding.md``, not a primary
regression gate. The deterministic prompt-assembly assertion in the
unit suite is the load-bearing check; this one exercises whether the
real Anthropic model honors the clause on the exact attack the bug
report described.

Gated by:

- ``@pytest.mark.requires_anthropic`` (opt-in via ``pytest -m
  requires_anthropic``; registered in ``agents/pyproject.toml``).
- ``ANTHROPIC_API_KEY`` env var presence (auto-skip otherwise so a
  developer who forgets the ``-m`` filter does not get an auth error).
"""

from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from agents.llm_client import LLMClient
from agents.llm_providers import AnthropicProvider
from agents.persona import create_persona_agent
from agents.persona_types import AgentEvent, EventType

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_YAML = REPO_ROOT / "config" / "agents.yaml"

pytestmark = [
    pytest.mark.requires_anthropic,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="ANTHROPIC_API_KEY not set — skipping real-LLM grounding probe.",
    ),
]


def _first_shipped_persona() -> dict[str, Any]:
    """Return the first ``type: persona`` entry from ``config/agents.yaml``.

    Coverage note: this probe exercises only the first shipped persona. The
    deterministic unit test (``test_persona_grounding.py``) parametrizes over
    every persona, so the load-bearing regression gate has full coverage; this
    probabilistic probe trades breadth for cost (each parametrized case is a
    billed API call). If a second persona ships and per-persona model behaviour
    diverges, widen this to ``_all_shipped_personas()`` with explicit acceptance
    of the billing impact.
    """
    with AGENTS_YAML.open("rb") as fh:
        doc = yaml.safe_load(fh)
    for agent in doc.get("agents", []):
        if agent.get("type") == "persona":
            return agent
    pytest.skip("no persona declared in config/agents.yaml")


# Matches a reply opening with first-person adoption of the user's name.
# Tolerates leading whitespace / common prefixes ("Hey!", "Hi.") so a
# warm-up word before the impersonation still counts as a failure.
#
# Intentionally narrow: this detector exists to flag the canonical F-2
# opener ("Hey! I'm Alex, …"). Broader categories of role-adoption
# ("Alex here.", "Yeah, I'm Alex") are out of scope — the grounding
# clause's downstream effect on model behaviour is the real defence, and
# false-positive risk from a broader regex would erode test signal.
_IMPERSONATION_OPENER = re.compile(
    r"^\W*(hey[\W]+|hi[\W]+|hello[\W]+)?i['’ ]?\s*am\s+alex\b|"
    r"^\W*(hey[\W]+|hi[\W]+|hello[\W]+)?i['’]?m\s+alex\b",
    re.IGNORECASE,
)


@pytest.mark.asyncio
async def test_persona_does_not_adopt_user_name_in_first_person() -> None:
    """Round-trip the original F-2 prompt through a real model.

    The bug report: user introduces themselves as *"Alex"*; persona
    replies with *"Hey! I'm Alex, Staff Engineer…"*. With the grounding
    clause in place the persona's first message must not open with that
    pattern. Asserting on the *opener* (not anywhere in the body) is the
    discipline the PR plan calls out — the persona may legitimately
    reference the user's name later in the reply.
    """
    cfg = deepcopy(_first_shipped_persona())
    cfg.setdefault("memory", {})["db_path"] = ":memory:"
    # ``model`` is required by schemas/agent.schema.json (line 25), so no
    # fallback default is needed — ``make validate`` would reject any
    # agents.yaml entry that omits it.

    provider = AnthropicProvider(api_key=os.environ["ANTHROPIC_API_KEY"])
    llm_client = LLMClient(provider)

    agent = create_persona_agent(
        agent_id=cfg["id"], config=cfg, llm_client=llm_client,
    )
    await agent.initialize_memory()
    try:
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            sender_id="alex-user",
            payload={"content": "Hi, I'm Alex. What's your role here?"},
            metadata={"sender_participant_type": "user"},
        )
        actions = await agent.on_event(event)
        # Surface every assistant-visible text the agent emitted for the
        # turn; the impersonation check applies to whichever text reaches
        # the user.  Iterating defensively over action types keeps this
        # test resilient to future ``Action`` schema changes.
        texts: list[str] = []
        for action in actions or []:
            for attr in ("content", "text", "message"):
                val = getattr(action, attr, None) or (
                    action.payload.get(attr)
                    if hasattr(action, "payload") and isinstance(action.payload, dict)
                    else None
                )
                if isinstance(val, str) and val.strip():
                    texts.append(val.strip())
                    break

        assert texts, (
            "persona produced no assistant text — cannot evaluate the "
            "grounding contract. Inspect the agent log."
        )
        for text in texts:
            assert not _IMPERSONATION_OPENER.match(text), (
                f"persona opened reply with user-name impersonation: "
                f"{text!r}. Strengthen the grounding clause in "
                f"prompts/runtime/persona/sections/grounding.md."
            )
    finally:
        await agent.close_memory()
