"""RFC 0051 Phase 5a (v0.3.10) — the reflexion critic→revise loop.

Phases 1–3 made a persona decide *whether* to post (the structured silence
verdict) and, under ``mode: plan``, *what* the post should accomplish (the
private :class:`~agents.persona_runtime.deliberation_plan.CompositionPlan`,
threaded into the Tier-C compose). Phase 5 closes the quality loop: **after** the
compose, a cheap ``fast`` **critic** re-reads the composed draft against that
plan and, only if it flags weakness, a ``quality`` **revise** pass rewrites it —
bounded to ``reasoning.revise`` rounds (RFC 0051 §Phase 5,
[OQ 3](../../docs/rfcs/0051-reasoning-before-posting.md#open-questions)).

Two load-bearing properties, both mirrored on the gate's discipline but with the
**opposite** bias:

* **Fail-soft, never block.** The gate biases to *silence*; reflexion biases to
  *posting*. A critic parse failure, a denied/exhausted lease, an unresolvable
  ``fast`` alias, a missing/malformed prompt snippet, a ``max_tokens``-truncated
  rewrite, or an empty revise output all degrade to the **last good draft** — the
  gate already decided the persona *should* post, so a reflexion hiccup must never
  swallow that turn. This is the same fail-closed-to-the-safe-direction rule
  :func:`~agents.persona_runtime.deliberation_plan.parse_plan` applies, pointed
  the other way.
* **Bounded cost.** The critic is the cheap leased ``fast`` call (a yes/no
  judgement, NOT one of the composes the RFC §F cost model counts); only a
  *weak* verdict pays the ``quality`` rewrite. So an ``N``-round post costs up to
  ``N+1`` quality composes (the original + one per flagged round), hard-capped by
  :data:`MAX_REVISE_ROUNDS` and the shared interaction lease (a low budget
  starves the later rounds first, degrading to the already-composed draft).

Own module — like :mod:`agents.persona_runtime.deliberation_plan` — so the loop
stays unit-testable in isolation and :mod:`agents.persona_runtime.action_loop`
keeps a one-line call and stays under the 500-line review cap. The discarded
drafts + critic notes are walled exactly like the plan (RFC 0051 §E); the no-leak
test (``tests/integration/test_deliberation_no_leak.py``) pins it.

**Default off.** The loop runs only under ``reasoning.revise ≥ 1``, which is
itself gated to ``mode: plan`` and defaults to ``0`` (single-pass) — an explicit,
per-channel operator opt-in on top of the plan rung.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from ..generated import wallet_pb2 as walletpb
from ..llm_types import StopReason
from ..model_aliases import resolve as resolve_model
from ..observability._metrics_salience import record_reflexion
from ..persona_types import ActionType, AgentAction
from ..prompt_loader import load_snippet
from ..wallet_client import BudgetExceededError
from .deliberation_plan import CompositionPlan

if TYPE_CHECKING:
    from ..llm_client import LLMClient
    from .salience_gate import SalienceOutcome

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_REVISE_ROUNDS",
    "ReflexionResult",
    "maybe_revise_channel_message",
    "run_reflexion",
]

# The hard ceiling on revise rounds (RFC 0051 §Phase 5 — ``revise: 0 | 1 | 2``).
# The config ``validate`` rejects a request above this (Go-side, capability gate);
# the loop clamps as defense-in-depth so a wire/test value can never run away.
MAX_REVISE_ROUNDS: Final[int] = 2

# The leased ``fast`` alias the critic runs on — the cheap judgement, billed like
# the salience bid, not counted among the §F quality composes. An unresolvable
# alias fails *soft* (no-op, keep the draft), mirroring the bid's fail-closed.
_CRITIC_MODEL_ALIAS: Final[str] = "fast"

# A judgement + a focused rewrite, not creative latitude.
_REFLEXION_TEMPERATURE: Final[float] = 0.0

# The critic's verdict is a single line; 64 tokens is ample (truncation past the
# verdict fails soft to "not weak"). The revise rewrites a chat message, so it
# reuses the persona's own compose ``max_tokens`` ceiling (passed by the caller).
_CRITIC_MAX_OUTPUT_TOKENS: Final[int] = 64

# Critic verdict grammar. ``weak: yes`` is the *only* trigger for a rewrite; a
# missing/unparseable verdict reads as **not weak** (fail-soft → keep the draft).
# Forgiving of surrounding prose like the bid's grammar; the ``\b`` keeps a token
# that merely starts with ``yes`` (``yesterday``) from tripping a spurious revise.
_WEAK_RE: Final[re.Pattern[str]] = re.compile(
    r"weak\s*[:=]\s*(?P<v>yes|no)\b", re.IGNORECASE,
)
_CRITIQUE_RE: Final[re.Pattern[str]] = re.compile(
    r"critique\s*[:=]\s*(?P<v>\S.*)", re.IGNORECASE,
)
# The critique note rides into the *trusted* revise prompt, so it carries the same
# per-field char bound the plan fields do (deliberation_plan ``_MAX_FIELD_CHARS``).
_CRITIQUE_MAX_CHARS: Final[int] = 240

_CRITIC_SYSTEM_SNIPPET: Final[str] = "reflexion-critic-system"
_CRITIC_USER_SNIPPET: Final[str] = "reflexion-critic-user"
_REVISE_SYSTEM_SNIPPET: Final[str] = "reflexion-revise-system"
_REVISE_USER_SNIPPET: Final[str] = "reflexion-revise-user"


@dataclass(frozen=True, slots=True)
class ReflexionResult:
    """The outcome of :func:`run_reflexion`.

    Attributes:
        text: The final draft — the revised message on a successful rewrite, or
            the original draft on a no-op / any fail-soft degradation. Always a
            post-able message (reflexion never blocks the turn).
        rounds: How many revise rounds actually rewrote the draft (``0`` on a
            no-op or a strong draft). The forward-compatible signal the PR 9
            revise-round telemetry reads.
        changed: ``True`` iff ``text`` differs from the draft passed in — the
            draft-changed / no-op-revise signal (PR 9 telemetry).
    """

    text: str
    rounds: int
    changed: bool


def _plan_brief(plan: CompositionPlan) -> str:
    """A compact, **data-framed** rendering of the plan for the critic/revise
    prompt — intent + the substance to land + what not to restate.

    Distinct from :func:`~agents.persona_runtime.deliberation_plan.render_plan_section`
    (which wraps the plan in the §E "never reveal" compose preamble): here the
    plan is the *rubric* the draft is judged against, not a section the persona
    composes under. Concatenated into the user prompt, never ``.format``-ed, so a
    brace in a plan field cannot break rendering (the bid's data-framing rule)."""
    lines = [f"Intent: {plan.intent}"]
    if plan.key_points:
        lines.append("Key points to land: " + "; ".join(plan.key_points))
    if plan.avoid_restating:
        lines.append("Do not restate: " + "; ".join(plan.avoid_restating))
    return "\n".join(lines)


def _parse_weak(text: str | None) -> bool:
    """``True`` only on an explicit ``weak: yes``. Everything else — a ``weak:
    no``, a missing verdict, empty text — is **not weak** (fail-soft: keep the
    draft, never block the post)."""
    if not text:
        return False
    match = _WEAK_RE.search(text)
    return match is not None and match.group("v").lower() == "yes"


def _parse_critique(text: str | None) -> str:
    """The critic's one-clause justification, char-bounded; ``""`` when absent."""
    if not text:
        return ""
    match = _CRITIQUE_RE.search(text)
    if match is None:
        return ""
    return match.group("v").strip()[:_CRITIQUE_MAX_CHARS]


async def run_reflexion(
    *,
    llm_client: LLMClient,
    draft: str,
    plan: CompositionPlan | None,
    revise: int,
    persona_name: str,
    persona_role: str,
    compose_model: str,
    compose_model_alias: str | None = None,
    cause: walletpb.Cause.ValueType = walletpb.CAUSE_CHANNEL_MESSAGE,
    agent_id: str = "",
    interaction_id: str = "",
    max_tokens: int = 4096,
) -> ReflexionResult:
    """Run the critic→revise loop over a composed ``draft`` (RFC 0051 §Phase 5).

    Returns a :class:`ReflexionResult`; the loop never raises and never blocks the
    post. It is a **no-op** (zero LLM calls) when ``revise <= 0``, when ``plan`` is
    ``None`` (nothing to critique against), or when ``draft`` is blank. Otherwise,
    up to ``min(revise, MAX_REVISE_ROUNDS)`` rounds each:

    1. run the **critic** (leased ``fast``) over the current draft + the plan;
    2. on ``weak: no`` / any fail-soft signal — stop, keep the draft;
    3. on ``weak: yes`` — run the **revise** (the persona's ``quality`` compose
       model) to rewrite, then loop. An errored/empty rewrite keeps the last good
       draft and stops.

    ``compose_model`` / ``compose_model_alias`` are the persona's quality turn
    model (the revise rewrites at compose quality). ``cause`` / ``agent_id`` /
    ``interaction_id`` bill both passes against the SAME interaction the compose
    used, so a low ``interaction_budget_tokens`` starves the later rounds first
    (the §F cost bound), and ``max_tokens`` caps the rewrite at the persona's own
    compose ceiling.
    """
    rounds_cap = min(revise, MAX_REVISE_ROUNDS)
    if rounds_cap <= 0 or plan is None or not draft.strip():
        return ReflexionResult(text=draft, rounds=0, changed=False)

    # Resolve the cheap critic alias once. An unresolvable ``fast`` (misconfig)
    # fails *soft* — no-op, keep the draft — rather than crashing the compose hot
    # path, the same SystemExit-swallow discipline the bid uses (RFC 0023 §F).
    try:
        critic = resolve_model(_CRITIC_MODEL_ALIAS)
    except SystemExit as exc:
        logger.warning(
            "Reflexion: critic model alias %r unresolvable for agent %s: %s; "
            "skipping revise", _CRITIC_MODEL_ALIAS, agent_id, exc,
        )
        return ReflexionResult(text=draft, rounds=0, changed=False)

    plan_brief = _plan_brief(plan)
    current = draft
    rounds = 0
    for _ in range(rounds_cap):
        critique = await _run_critic(
            llm_client, critic_model=critic.model, critic_alias=critic.alias,
            draft=current, plan_brief=plan_brief, persona_name=persona_name,
            persona_role=persona_role, cause=cause, agent_id=agent_id,
            interaction_id=interaction_id,
        )
        if critique is None:  # not weak, or a fail-soft signal → stop
            break
        revised = await _run_revise(
            llm_client, model=compose_model, model_alias=compose_model_alias,
            draft=current, critique=critique, plan_brief=plan_brief,
            persona_name=persona_name, persona_role=persona_role, cause=cause,
            agent_id=agent_id, interaction_id=interaction_id, max_tokens=max_tokens,
        )
        if revised is None:  # rewrite failed/empty → keep the last good draft
            break
        if revised == current:
            # The revise returned byte-identical text — the model converged on the
            # current draft despite the critic's flag. Stop WITHOUT counting a round:
            # ``rounds`` must count only real rewrites (PR 9 telemetry pairs it with
            # ``changed``), and re-running the critic would re-flag the same text and
            # burn the lease for no progress.
            break
        current = revised
        rounds += 1

    return ReflexionResult(text=current, rounds=rounds, changed=current != draft)


async def _run_critic(
    llm_client: LLMClient,
    *,
    critic_model: str,
    critic_alias: str | None,
    draft: str,
    plan_brief: str,
    persona_name: str,
    persona_role: str,
    cause: walletpb.Cause.ValueType,
    agent_id: str,
    interaction_id: str,
) -> str | None:
    """The cheap critic pass. Returns the critique note (``""`` if none) when the
    draft is **weak** — the signal to revise — or ``None`` to stop (a strong
    draft, OR any fail-soft degradation: parse failure, denied/exhausted lease,
    provider error). Distinguishing weak-with-no-note (``""``) from stop
    (``None``) is what lets the caller keep the loop fail-soft.

    The prompt assembly (``load_snippet`` + ``.format``) is INSIDE the guard so a
    missing/misnamed snippet (``PromptLoadError``) or a stray brace in a snippet
    (``str.format`` raising) degrades to a no-op like every other signal, rather
    than propagating and losing a post the gate already admitted."""
    try:
        system = load_snippet(_CRITIC_SYSTEM_SNIPPET).format(
            persona_name=persona_name, persona_role=persona_role,
        )
        user = (
            f"Your private plan for the message:\n{plan_brief}\n\n"
            f"Your draft message:\n{draft}\n\n"
            f"{load_snippet(_CRITIC_USER_SNIPPET)}"
        )
        response = await llm_client.create_message(
            model=critic_model, model_alias=critic_alias,
            messages=[{"role": "user", "content": user}], system=system, tools=[],
            max_tokens=_CRITIC_MAX_OUTPUT_TOKENS, temperature=_REFLEXION_TEMPERATURE,
            cause=cause, agent_id=agent_id, interaction_id=interaction_id,
        )
    except BudgetExceededError:
        logger.debug("Reflexion: critic lease denied for agent %s; keeping draft", agent_id)
        return None
    except Exception as exc:  # noqa: BLE001 - prompt-load/format OR provider error → keep the draft
        logger.warning("Reflexion: critic error for agent %s: %s; keeping draft", agent_id, exc)
        return None
    if not _parse_weak(response.text):
        return None
    return _parse_critique(response.text)


async def _run_revise(
    llm_client: LLMClient,
    *,
    model: str,
    model_alias: str | None,
    draft: str,
    critique: str,
    plan_brief: str,
    persona_name: str,
    persona_role: str,
    cause: walletpb.Cause.ValueType,
    agent_id: str,
    interaction_id: str,
    max_tokens: int,
) -> str | None:
    """The quality rewrite pass. Returns the revised message, or ``None`` to keep
    the last good draft (a denied/exhausted lease, a provider error, an empty
    rewrite, a missing/malformed prompt snippet, OR a ``max_tokens``-truncated
    rewrite — all fail-soft).

    The prompt assembly is inside the guard for the same reason as the critic:
    a snippet-load or ``.format`` failure keeps the draft rather than raising."""
    try:
        system = load_snippet(_REVISE_SYSTEM_SNIPPET).format(
            persona_name=persona_name, persona_role=persona_role,
        )
        critique_block = f"A reviewer flagged: {critique}\n\n" if critique else ""
        user = (
            f"Your private plan for the message:\n{plan_brief}\n\n"
            f"{critique_block}"
            f"Your current draft:\n{draft}\n\n"
            f"{load_snippet(_REVISE_USER_SNIPPET)}"
        )
        response = await llm_client.create_message(
            model=model, model_alias=model_alias,
            messages=[{"role": "user", "content": user}], system=system, tools=[],
            max_tokens=max_tokens, temperature=_REFLEXION_TEMPERATURE,
            cause=cause, agent_id=agent_id, interaction_id=interaction_id,
        )
    except BudgetExceededError:
        logger.debug("Reflexion: revise lease denied for agent %s; keeping draft", agent_id)
        return None
    except Exception as exc:  # noqa: BLE001 - prompt-load/format OR provider error → keep the draft
        logger.warning("Reflexion: revise error for agent %s: %s; keeping draft", agent_id, exc)
        return None
    # A truncated rewrite is a half-sentence — strictly worse than the complete
    # draft. Degrade to the last good draft, the same hard-stop the compose path
    # applies to MAX_TOKENS (action_loop step 3a) rather than posting the fragment.
    if response.stop_reason is StopReason.MAX_TOKENS:
        logger.warning(
            "Reflexion: revise truncated (max_tokens) for agent %s; keeping draft", agent_id,
        )
        return None
    revised = (response.text or "").strip()
    return revised or None


async def maybe_revise_channel_message(
    agent: Any,
    actions: list[AgentAction],
    salience: SalienceOutcome | None,
    *,
    cause: walletpb.Cause.ValueType,
    agent_id: str,
    interaction_id: str,
    max_tokens: int,
) -> list[AgentAction]:
    """The thin :mod:`~agents.persona_runtime.action_loop` glue: revise the
    composed channel reply under the private plan when ``reasoning.revise ≥ 1``.

    A no-op (the ``actions`` list returned unchanged, identity-preserved) on every
    rung but ``mode: plan`` with ``revise ≥ 1`` — there is no plan to critique
    against otherwise — and when the turn produced no ``SEND_CHANNEL_MESSAGE`` with
    postable content. On a successful rewrite only that one action's ``content`` is
    replaced; the rest of the turn's actions (memory writes, votes) are untouched.

    Lives here (not in ``action_loop``) so the loop's hot path keeps a single call
    and stays under the 500-line cap, the same separation that pulled the plan
    section into :mod:`~agents.persona_runtime.deliberation_plan`. ``run_reflexion``
    itself stays free of agent/action types (unit-testable in isolation); this is
    the only ``AgentAction``-aware seam."""
    if salience is None or salience.plan is None or salience.revise < 1:
        return actions
    # The first postable channel message — a turn composes one reply against one
    # plan (``synthesize_channel_reply`` yields a single SEND_CHANNEL_MESSAGE), so
    # there is exactly one draft to critique. Any sibling messages ship as composed.
    idx = next(
        (
            i for i, a in enumerate(actions)
            if a.action_type is ActionType.SEND_CHANNEL_MESSAGE
            and str(a.payload.get("content", "")).strip()
        ),
        None,
    )
    if idx is None:
        return actions

    # Fail-soft at the glue seam too. ``run_reflexion`` never raises, but the agent
    # attribute/config reads that feed it (``agent.name`` / ``agent.config["model"]``)
    # sit OUTSIDE its guard — a malformed agent would otherwise propagate and lose a
    # post the gate already admitted. Degrade to the composed draft instead, the same
    # bias-to-posting the loop itself applies to every other reflexion hiccup.
    try:
        result = await run_reflexion(
            llm_client=agent._llm_client,
            draft=str(actions[idx].payload["content"]),
            plan=salience.plan,
            revise=salience.revise,
            persona_name=agent.name,
            persona_role=agent.role,
            compose_model=agent.config["model"],
            compose_model_alias=agent.config.get("model_alias"),
            cause=cause,
            agent_id=agent_id,
            interaction_id=interaction_id,
            max_tokens=max_tokens,
        )
    except Exception as exc:  # noqa: BLE001 - any agent-shape error → keep the composed draft
        logger.warning(
            "Reflexion: glue error for agent %s: %s; keeping composed draft", agent_id, exc,
        )
        return actions
    # The loop ran to completion — chart its outcome (PR 9 telemetry). Emitted on
    # both the rewrite and the no-op (strong draft / fail-soft) so the draft-changed
    # fraction is computable; best-effort, never blocks the post (the guard above
    # already returned on any pre-loop short-circuit, so this counts only real runs).
    record_reflexion(rounds=result.rounds, changed=result.changed)
    if not result.changed:
        return actions
    # Replace only the message content; build a fresh action + list so a frozen
    # or shared payload is never mutated in place (the no-leak test relies on the
    # discarded draft never escaping — only the final text reaches the executor).
    revised = AgentAction(
        action_type=ActionType.SEND_CHANNEL_MESSAGE,
        payload={**actions[idx].payload, "content": result.text},
    )
    return [*actions[:idx], revised, *actions[idx + 1:]]
