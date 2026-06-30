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
from ..observability.metrics import current_agent_id, try_get_instruments
from ..persona_behavior import render_behavior
from ..persona_types import AgentEvent, EventType
from ..prompt_loader import load_persona_section, load_snippet
from ..prompt_safety import escape_prompt_delimiters
from ..temporal.rendering import format_now_anchor
from .convener import format_convener_opening

if TYPE_CHECKING:
    from ..clock import Clock
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


def _grounding_context(
    persona_cfg: dict[str, Any],
    state: PersonaState,
    name: str,
    role: str,
) -> dict[str, str]:
    # PR plan §PR 5 (v0.3.0 channel test findings F-2): the grounding
    # clause is rendered with the persona's own name woven in so the
    # invariant is concrete per persona rather than a generic "you are
    # not the user" line; the model is less likely to drift on a
    # personalized invariant than a templated one.
    return {"name": name}


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


def format_chair_escalation(
    formatted_event: str, *, resynthesize: bool = False
) -> str:
    """Wrap a formatted CHANNEL_MESSAGE in the chair-escalation framing.

    The chair-stall-escalation amendment's forced turn (§C item 2): the
    orchestrator re-delivered a stalled stimulus to this persona — the
    channel's designated chair — and the framing tells it what the turn is
    and what its two permissible outcomes are (synthesize + vote, or call on
    the member best placed). Rendered per-event, ahead of the
    already-sanitized formatted message; the snippet is the prose half
    (``prompts/runtime/safety/chair-escalation.md``, the
    ``end-interaction-vote`` snippet's sibling) and is lru-cached by
    :func:`load_snippet`, so the per-event call costs one dict lookup.

    ``resynthesize`` (ISSUE-0099) selects the synthesize-only variant
    (``chair-escalation-resynthesize.md``): the SECOND forced turn, sent after
    the chair's first hand-off provably reached no floor-capable member. The
    two-outcome default is wrong there — handing off again is the move that
    just failed — so the variant drops outcome (b) and forces the end-vote.
    The wire flag is ``chair_escalation_resynthesize``, a refinement of
    ``chair_escalation``; the gate lift is unchanged (it still keys on
    ``chair_escalation``), so this only swaps the framing.
    """
    snippet = "chair-escalation-resynthesize" if resynthesize else "chair-escalation"
    return f"{load_snippet(snippet)}\n\n{formatted_event}"


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
    # PR plan §PR 5 (v0.3.0 channel test findings F-2): grounding clause
    # against user-name impersonation.  Always-on, placed immediately
    # after identity so the "you are not the user" invariant lands
    # before the persona-config sections (background, behavior, quirks,
    # goals) that describe voice and inadvertently provide vectors for
    # role-adoption drift.
    _Section(
        name="grounding",
        predicate=lambda cfg, state: True,
        context=_grounding_context,
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
    # RFC 0021 PR 2: temporal seam.  Set by ``_LLMPersonaAgent.__init__``.
    _clock: Clock
    _timezone: str

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

        # RFC 0021 §C: now-anchor block, unconditionally appended after
        # the persona-config sections and before the safety snippets.
        # The persona always benefits from knowing the current time —
        # the RFC explicitly rejects a behavioral toggle ("there is no
        # scenario in which a persona is better off not knowing the
        # current time").  Rendering uses the agent's ``Clock`` seam so
        # tests can pin the line with a ``FrozenClock``.
        anchor_template = load_persona_section("now-anchor")
        anchor_text = format_now_anchor(self._clock.now(), self._timezone)
        rendered.append(anchor_template.format_map({"now_anchor": anchor_text}))
        _inst = try_get_instruments()
        if _inst is not None:
            _inst.temporal_now_anchor_emitted.add(
                1, attributes={"agent.id": current_agent_id()},
            )

        # Conversation-window self-awareness (v0.3.7 test-findings F-2).
        # RFC 0034 reconstructs the in-progress conversation as a rolling
        # transcript in the ``messages`` array, but the persona was never
        # told that view exists — so it denied being able to read past
        # messages and hedged on how many it could see, falling back on
        # the generic "I don't retain conversations" disclaimer. This
        # unconditional, perceptual nudge sits alongside the now-anchor
        # (both ground the persona in its current situation: the time it
        # is, and the recent conversation it can see) and tells it to
        # describe the window honestly rather than deny memory or invent a
        # message count. The window itself stays in the ``messages`` array
        # (RFC 0034 §B), never the system prompt — this snippet only
        # describes it.
        rendered.append(load_snippet("conversation-window-awareness"))

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

        # Reply discretion + conversational pacing are unconditional
        # behavioural nudges. The response gate (response_gate.py) decides
        # whether the persona may speak; these snippets tell the persona
        # how to spend that permission. ``reply-discretion`` pins the
        # "silence is a valid turn outcome on group channels" affordance
        # and the "DMs always reply" invariant; ``conversational-pacing``
        # tells the persona to match the length/register of the inbound
        # message so a one-line greeting does not draw a paragraph.
        rendered.append(load_snippet("reply-discretion"))
        rendered.append(load_snippet("conversational-pacing"))

        # Peer-voice nudge (v0.3.7, RFC 0030 relevance amendment §"What is
        # prompt, what is architecture"). The Tier A gate decides *whether*
        # a persona may speak on a group channel; this snippet shapes *how*
        # — frame the persona as a colleague among peers, not an assistant
        # serving a user (address people by name, build on the round's
        # transcript, disagree/defer like a colleague). Unconditional like
        # ``reply-discretion``: the assembler has no per-turn channel
        # context, so the prose carries the DM carve-out inline rather than
        # a code gate.
        rendered.append(load_snippet("peer-conversation-voice"))

        # End-of-discussion vote vocabulary (RFC 0030 Layer 4, the producer
        # plan PR 2). The orchestrator's quorum machinery is live; this
        # snippet is the prompt half of the social contract — it teaches the
        # one structured action a persona can emit (the exact JSON form the
        # action parser recognises) and when voting is appropriate.
        # Unconditional like its siblings: the assembler has no per-turn
        # channel context, so the prose carries the group-channel framing and
        # the DM carve-out inline.
        rendered.append(load_snippet("end-interaction-vote"))

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
                # SECURITY: ingest-time pattern sanitisation now runs
                # upstream in
                # :meth:`_ActionLoopMixin._sanitize_inbound_event`
                # (RFC 0011 PR 5), so ``content`` reaching this format
                # site has already been cleared of the canonical
                # injection-pattern set. Length-capping is still
                # deferred — ``_sanitize_inbound_event`` matches
                # patterns but does not bound input length, so a future
                # external bridge that allows very long messages would
                # still bloat the prompt. Wiring a length cap is the
                # right thing to do alongside the v0.5.0 external-bridge
                # work; until then the prompt-budget logic in
                # :class:`MemoryBudget` is the only effective ceiling.
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
                # RFC 0052 §B: a convene forced turn opens an autonomous
                # channel. The operator topic/agenda/goal ride in `content`
                # as a DISTINCT trust class (operator config, not
                # persona-authored), so this branch renders them wrapped in
                # the RFC 0009 `<external_data>` envelope with the convener
                # framing — NOT the peer-message `<|user_message|>` /
                # "Message from" shapes below. Strict `is True`, mirroring the
                # response gate's read and `chair_escalation`; the framing
                # lives in `convener.py` (the sibling of
                # `format_chair_escalation`), so this stays a one-line branch
                # and self-independent (the conversation-window seam casts
                # this method with `self=None`).
                if event.payload.get("convene") is True:
                    return format_convener_opening(content)
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
                    # Shared with the RFC 0036 recall tool's §F per-row
                    # escape via ``agents.prompt_safety`` — one source of
                    # truth, never two divergent copies.
                    safe_content = escape_prompt_delimiters(content)
                    safe_sender = sender.replace('"', "")
                    formatted = (
                        f'<|user_message user_id="{safe_sender}"|>\n'
                        f"{safe_content}\n"
                        f"<|/user_message|>"
                    )
                else:
                    formatted = f"Message from {sender}:\n\n{content}"
                # Chair-stall-escalation amendment (§C item 2): a marked
                # forced turn carries the escalation framing ahead of the
                # stalled stimulus — per-event, the sibling of the
                # `end-interaction-vote` system-prompt snippet. Strict
                # `is True`, mirroring the response gate's read; the framing
                # wraps the already-sanitized formatted message, so it adds
                # no new injection surface.
                if event.payload.get("chair_escalation") is True:
                    # ISSUE-0099: the resynthesize refinement swaps in the
                    # synthesize-only framing for the second forced turn; the
                    # admission lift above is unchanged (it keys on
                    # `chair_escalation`). Strict `is True`, same as the gate.
                    resynth = (
                        event.payload.get("chair_escalation_resynthesize") is True
                    )
                    return format_chair_escalation(formatted, resynthesize=resynth)
                return formatted
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
