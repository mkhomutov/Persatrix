"""System prompt + event formatting for ``_LLMPersonaAgent``.

Extracted from ``persona_runtime/__init__.py`` to keep that module under
the 500-line code file-size limit (``scripts/checks/file_size.py``).

Contains the ``_PromptAssemblyMixin`` with:

- ``_build_system_prompt()`` — assembles identity, behavior, goals,
  dynamic state, and memory-tool usage instructions into the system
  prompt string.  The persona sections (identity, background, behavior,
  quirks, goals, current-state) are loaded from
  ``prompts/runtime/persona/sections/`` via
  :func:`agents.prompt_loader.load_persona_section` and rendered with
  ``str.format_map``.  See RFC 0022 for the templating contract.
- ``_format_event()`` — renders an ``AgentEvent`` into the user-turn
  string presented to the LLM, including user-message delimiters and
  prompt-injection sanitization (PR #120 F-2).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..base import TaskInput
from ..persona_behavior import render_behavior
from ..persona_types import AgentEvent, EventType
from ..prompt_loader import load_persona_section, load_snippet

if TYPE_CHECKING:
    from ..persona_types import PersonaState
    from ..tools.registry import ToolDefinition


# ─── Persona section table ──────────────────────────────────
#
# Each section is loaded from ``prompts/runtime/persona/sections/<name>.md``
# (RFC 0022).  Adding a new section is one entry here plus one markdown
# file — no template-syntax change required.  Order in the tuple is the
# order sections appear in the rendered prompt.
#
# ``predicate`` returns ``True`` when the section should be rendered for
# a given persona config + state.  ``context`` returns the placeholder
# dict for ``str.format_map``; templates assume their predicate has
# fired so values are guaranteed non-empty.


@dataclass(frozen=True)
class _Section:
    """One persona-prompt section: a template name + when to render it + how.

    ``predicate`` and ``context`` both accept ``state`` even though most
    current sections only consult ``cfg``.  The signature is uniform on
    purpose: future state-dependent sections (e.g. an RFC 0021 now-anchor
    section whose visibility flips with ``state``) plug in without
    widening the protocol.  Reviewers seeing an unused ``state`` in a
    predicate lambda should read it as forward-compatibility, not dead
    code.
    """

    name: str
    predicate: Callable[[dict[str, Any], PersonaState], bool]
    context: Callable[
        [dict[str, Any], PersonaState, str, str], dict[str, str]
    ]


def _identity_context(
    persona_cfg: dict[str, Any],
    state: PersonaState,
    name: str,
    role: str,
) -> dict[str, str]:
    title = persona_cfg.get("title")
    # title_line carries its own trailing newline so the template can
    # keep the placeholder on its own logical line without producing a
    # stray blank line when the title is absent (RFC 0022 §C).
    title_line = f"Title: {title}\n" if title else ""
    return {"name": name, "title_line": title_line, "role": role}


def _background_context(
    persona_cfg: dict[str, Any],
    state: PersonaState,
    name: str,
    role: str,
) -> dict[str, str]:
    return {"background": persona_cfg["background"].strip()}


def _behavior_context(
    persona_cfg: dict[str, Any],
    state: PersonaState,
    name: str,
    role: str,
) -> dict[str, str]:
    return {"behavior": render_behavior(persona_cfg.get("behavior", {}))}


def _quirks_context(
    persona_cfg: dict[str, Any],
    state: PersonaState,
    name: str,
    role: str,
) -> dict[str, str]:
    bullets = "\n".join(f"- {q}" for q in persona_cfg["quirks"])
    return {"quirks": bullets}


def _goals_context(
    persona_cfg: dict[str, Any],
    state: PersonaState,
    name: str,
    role: str,
) -> dict[str, str]:
    goals = persona_cfg["goals"]
    lines: list[str] = []
    if goals.get("primary"):
        lines.append(f"- Primary: {goals['primary']}")
    for g in goals.get("secondary", []):
        lines.append(f"- Secondary: {g}")
    if goals.get("hidden"):
        lines.append(f"- Hidden motivation: {goals['hidden']}")
    return {"goals": "\n".join(lines)}


def _state_context(
    persona_cfg: dict[str, Any],
    state: PersonaState,
    name: str,
    role: str,
) -> dict[str, str]:
    return {"state": state.to_prompt_section()}


def _goals_present(persona_cfg: dict[str, Any]) -> bool:
    """Goals section renders when at least one populated key is present.

    This predicate is intentionally **stricter** than the pre-refactor
    ``if goals:`` truthiness check.  The byte-identical contract
    (RFC 0022 §F) holds for every well-formed shipped persona config;
    for degenerate shapes the new composer is more conservative:

    - ``goals: {"primary": ""}`` — old composer rendered an orphan
      ``Goals:`` header with no bullets; new composer omits the section.
    - ``goals: ["a", "b"]`` (non-dict) — old composer crashed with
      ``AttributeError`` on ``goals.get("primary")``; new composer
      omits the section.

    Both deltas are improvements over the previous behavior and are
    pinned by tests in ``test_persona_section_composer.py``.
    """
    goals = persona_cfg.get("goals", {})
    if not isinstance(goals, dict):
        return False
    return bool(
        goals.get("primary") or goals.get("secondary") or goals.get("hidden")
    )


_SECTIONS: tuple[_Section, ...] = (
    _Section(
        name="identity",
        predicate=lambda cfg, state: True,
        context=_identity_context,
    ),
    _Section(
        name="background",
        predicate=lambda cfg, state: bool(cfg.get("background")),
        context=_background_context,
    ),
    _Section(
        name="behavior",
        predicate=lambda cfg, state: bool(render_behavior(cfg.get("behavior", {}))),
        context=_behavior_context,
    ),
    _Section(
        name="quirks",
        predicate=lambda cfg, state: bool(cfg.get("quirks")),
        context=_quirks_context,
    ),
    _Section(
        name="goals",
        predicate=lambda cfg, state: _goals_present(cfg),
        context=_goals_context,
    ),
    _Section(
        name="current-state",
        predicate=lambda cfg, state: bool(state.to_prompt_section()),
        context=_state_context,
    ),
)


class _PromptAssemblyMixin:
    """System-prompt and event-formatting helpers for persona agents."""

    # Attributes provided by ``_LLMPersonaAgent``; declared for type checkers.
    name: str
    role: str
    persona: dict[str, Any]
    _state: PersonaState
    _memory_tools: list[ToolDefinition]

    def _build_system_prompt(self) -> str:
        """Assemble the full system prompt from persona config, behavior, and state.

        Persona sections are loaded from ``prompts/runtime/persona/sections/``
        and rendered through ``str.format_map`` (RFC 0022).  Safety
        snippets (user-message delimiters, memory-tool usage) are loaded
        from ``prompts/runtime/safety/`` via :func:`load_snippet` and
        appended after the persona sections.  Sections are joined with a
        blank line — equivalent to the previous f-string composer's
        ``"\\n".join(parts)``-with-leading-``\\n`` idiom.
        """
        persona_cfg = self.persona
        rendered: list[str] = []

        for section in _SECTIONS:
            if section.predicate(persona_cfg, self._state):
                template = load_persona_section(section.name)
                ctx = section.context(persona_cfg, self._state, self.name, self.role)
                rendered.append(template.format_map(ctx))

        # Safety snippets live under ``prompts/runtime/safety/`` and load
        # through ``load_snippet`` rather than the persona section loader.
        # User-message delimiter contract is unconditional so the LLM
        # always knows the convention, even before any user messages
        # arrive in this session (OQ 14b).
        rendered.append(load_snippet("user-message-delimiters"))

        # External-data envelope contract (RFC 0009 PR 3): unconditional
        # so the LLM understands `<external_data>` wrapping the moment
        # an external-source tool returns a result. Loading conditionally
        # would create a window where the first http_request / file_read
        # call before the snippet was ever rendered would arrive without
        # the prompt instructions.
        rendered.append(load_snippet("external-data-handling"))

        # Memory-tool usage nudge — without this the LLM often responds
        # conversationally ("Got it, I'll remember that") instead of
        # actually calling the store_note / recall_notes tools.
        if self._memory_tools:
            rendered.append(load_snippet("memory-tool-usage"))

        return "\n\n".join(rendered)

    def _format_event(self, event: AgentEvent) -> str:
        """Format an event as a user message for the LLM."""
        match event.event_type:
            case EventType.TASK_ASSIGNED:
                task = event.payload.get("task")
                if isinstance(task, TaskInput):
                    return f"You have been assigned a task:\n\n{task.payload}"
                return f"You have been assigned a task:\n\n{event.payload}"
            case EventType.CHANNEL_MESSAGE:
                # SECURITY: sender_id and content originate from the
                # dispatcher today (trusted).  When external bridges
                # (Slack, Discord, email) are added in v0.2+, these
                # fields will carry untrusted user input — sanitize
                # and length-cap before injecting into the LLM prompt
                # to mitigate prompt injection risks.
                #
                # User-typed channel messages are wrapped in XML-style
                # ``<|user_message|>`` delimiters with the PR #120 F-2
                # delimiter-injection sanitisation below, so a body
                # containing literal ``<|`` / ``|>`` cannot close the
                # block early and impersonate system instructions.
                # Without this case the event would fall through to
                # ``case _:`` and reach the LLM as a raw json.dumps
                # blob, leaking brace/quote tokens (PR #248 deep review
                # Medium; PR #249 deep-review Low cleaned up the
                # historical two-branch ``MESSAGE_RECEIVED`` /
                # ``CHANNEL_MESSAGE`` symmetry comment after the
                # RFC 0011 PR 4a-ii-α hard rename collapsed both
                # enum members into ``CHANNEL_MESSAGE``).
                sender = event.sender_id or "unknown"
                content = event.payload.get("content", "")
                # Wrap user participant messages in XML-style delimiters
                # to help the LLM distinguish human input from system
                # instructions (OQ 4, OQ 14 — prompt injection mitigation).
                sender_type = event.metadata.get("sender_participant_type", "agent")
                if sender_type == "user":
                    # Sanitize content: strip delimiter sequences that could
                    # allow a user to close the <|user_message|> block early
                    # and inject text that appears to come from the system.
                    # Also sanitize sender to prevent attribute injection
                    # via embedded double-quotes.
                    # (PR #120 review F-2: delimiter escape injection.)
                    safe_content = content.replace("<|", "\\<|").replace("|>", "\\|>")
                    safe_sender = sender.replace('"', "")
                    return (
                        f'<|user_message user_id="{safe_sender}"|>\n'
                        f"{safe_content}\n"
                        f"<|/user_message|>"
                    )
                return f"Message from {sender}:\n\n{content}"
            case EventType.MENTION:
                sender = event.sender_id or "unknown"
                content = event.payload.get("content", "")
                return f"You were mentioned by {sender}:\n\n{content}"
            case EventType.SUB_AGENT_COMPLETED:
                result = event.payload.get("result", "")
                return f"A sub-agent completed its task:\n\n{result}"
            case EventType.TICK:
                return "Autonomous tick: review your goals and decide on next actions."
            case _:
                try:
                    payload_str = json.dumps(event.payload)
                except TypeError:
                    payload_str = str(event.payload)
                return f"Event ({event.event_type.value}): {payload_str}"
