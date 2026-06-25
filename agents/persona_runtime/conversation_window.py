"""RFC 0034 Phase 1 PR 2 — persona conversation-window substrate.

The persona runtime calls the LLM with a ``messages`` array rebuilt from
scratch every turn that holds **only the current user message** — there
is no short-term conversational memory in the model's context, so
mid-conversation the persona treats every turn as the first turn
(ISSUE-0052).

This module owns the fix: :func:`build_conversation_messages`
reconstructs the ``messages`` array from the channel store on every
persona turn. It fetches the last N messages of ``event.channel_id``,
maps peer messages to ``role="user"`` and the persona's own messages to
``role="assistant"``, sanitizes each replayed peer turn through the same
``<|user_message|>`` delimiter escape the current event already gets,
and appends the current event last.

PR 2 ships the substrate **only** — no call site is wired. RFC 0034
Phase 1 PR 3 installs the call in ``action_loop.py`` immediately before
the ``messages = [...]`` LLM seed.

Design anchors (see ``docs/rfcs/0034-persona-conversational-working-memory.md``):

* **§B — window definition.** The window is the last ``max_turns``
  replayed messages of ``event.channel_id``, per-channel (no session
  filter — see OQ #1 below). The current event is excluded from the
  fetched window (dedup by ``id``) and appended last by this module.
  The fetch over-fetches by one (``max_turns + 1``): the inbound event
  may already be persisted, so dropping its own row would otherwise
  shrink the replayed window to ``max_turns - 1``. Rows *newer* than the
  current event — a concurrent writer can persist one before this fetch
  runs — are dropped too, so the transcript never places a future
  message ahead of the current turn (see
  :func:`_drop_rows_newer_than_current`).
* **§C — role mapping.** ``sender_id == agent_id`` ⇒ ``assistant``; any
  other sender ⇒ ``user``, prefixed inline ``[<peer_id>]: `` (Phase 2).
* **§D — sanitization.** Replayed peer turns are formatted through
  ``_PromptAssemblyMixin._format_event``'s ``CHANNEL_MESSAGE`` branch so
  the ``"<|" -> "\\<|"`` / ``"|>" -> "\\|>"`` escape is inherited by
  construction. The escape is never duplicated here.
* **§E — token budget.** ``max_tokens`` bounds the replayed transcript
  separately from the RFC 0017 system-prompt memory budget. Per-turn
  admission applies token-overflow FIFO first, then count-overflow FIFO
  (OQ #2 resolution 2a — tighter bound wins).
* **§F — caching.** An in-process cache keyed by ``(channel_id, limit)``
  skips the network fetch when the same event is seen twice at the same
  fetch limit (retries, sub-agent return paths). ``limit`` is in the key
  so a group channel's small-``max_turns`` persona cannot serve an
  undersized window to a large-``max_turns`` peer reacting to the same
  message (RFC 0034 Phase 2 correctness). Steady-state turn-over-turn
  hit rate is low *by design* — the cache key advances with every
  inbound message; Phase 3 telemetry is the arbiter for any re-spec
  (RFC §F "Known gap" framing (a)). The cache is bounded by an LRU
  (:class:`_WindowCache`, RFC 0034 Phase 3) so it cannot grow without
  bound over a long-lived process, and the hit/miss/eviction/fetch-latency
  /fallback instruments land in
  :mod:`agents.observability._metrics_conversation_window`. On any fetch
  failure the window degrades to current-event-only — the persona is no
  worse off than it is today.

OQ #1 resolution 1a (per-channel, no session filter): the window
filters on ``event.channel_id`` only; rows are admitted regardless of
``chat_session_id`` / ``persatrix_session_id``. Restarting the chat CLI
under a fresh session id preserves in-channel transcript continuity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from ..memory.working import estimate_tokens
from ..persona_types import AgentEvent, EventType

# ``_WINDOW_CACHE`` is re-exported here (its home is _conversation_window_cache,
# RFC §F) so the test-suite cache-reset fixtures can clear it via this module.
from ._conversation_window_cache import _WINDOW_CACHE, _fetch_window  # noqa: F401
from .prompt_assembly import _PromptAssemblyMixin

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..channel_history_fetcher import ChannelHistoryFetcher

__all__ = [
    "ConversationWindowConfig",
    "build_conversation_messages",
    "resolve_conversation_window_config",
]

# Committed Phase 1 defaults (RFC 0034 OQ #2). A retune is a one-line
# change here once Phase 3 telemetry lands — no schema implication.
DEFAULT_MAX_TURNS: int = 20
DEFAULT_MAX_TOKENS: int = 2048


@dataclass(frozen=True, slots=True)
class ConversationWindowConfig:
    """Per-agent tuning for the conversation window.

    Defaults mirror the top-level ``conversation_window`` block in
    ``config/optimization.yaml``; a per-agent ``conversation_window``
    block in ``config/agents.yaml`` overrides them. ``enabled: false`` is
    the operator escape hatch — :func:`build_conversation_messages` then
    seeds the array with the current event only (pre-RFC-0034
    behaviour). Config resolution into this dataclass is wired by PR 3.
    """

    max_turns: int = DEFAULT_MAX_TURNS
    max_tokens: int = DEFAULT_MAX_TOKENS
    enabled: bool = True


def resolve_conversation_window_config(
    agent_config: dict[str, Any],
) -> ConversationWindowConfig:
    """Resolve a :class:`ConversationWindowConfig` from a persona's config.

    Reads the optional per-agent ``conversation_window`` block in
    ``config/agents.yaml``. Any key absent from the block — or the whole
    block absent — inherits the dataclass default, which mirrors the
    ``config/optimization.yaml`` defaults block (the two are pinned equal
    by ``test_conversation_window.py::test_defaults_match_optimization_yaml``).

    A malformed block must not crash agent construction: production
    configs are gated through ``make validate`` against
    ``schemas/agent.schema.json``, but test fixtures and dict-built
    configs bypass that. A non-mapping block, a wrong-typed value, or an
    out-of-range integer count degrades to the per-key default. ``bool``
    is rejected for the integer counts explicitly — it is an ``int``
    subclass in Python, so ``max_turns: true`` would otherwise resolve to
    ``1``. The counts must additionally be ``>= 1``: the schema pins
    ``minimum: 1`` for both, and the resolver mirrors that lower bound for
    the configs that bypass the schema gate — a ``0`` or negative count
    would otherwise pass the type check yet silently yield an empty
    replayed window (``_apply_admission`` drops every turn).
    """
    defaults = ConversationWindowConfig()
    block = agent_config.get("conversation_window")
    if not isinstance(block, dict):
        return defaults

    raw_turns = block.get("max_turns")
    raw_tokens = block.get("max_tokens")
    raw_enabled = block.get("enabled")
    return ConversationWindowConfig(
        max_turns=(
            raw_turns
            if isinstance(raw_turns, int)
            and not isinstance(raw_turns, bool)
            and raw_turns >= 1
            else defaults.max_turns
        ),
        max_tokens=(
            raw_tokens
            if isinstance(raw_tokens, int)
            and not isinstance(raw_tokens, bool)
            and raw_tokens >= 1
            else defaults.max_tokens
        ),
        enabled=raw_enabled if isinstance(raw_enabled, bool) else defaults.enabled,
    )


# ``_PromptAssemblyMixin._format_event`` is a bound method on the persona
# agent, but its ``CHANNEL_MESSAGE`` branch reads only ``event`` fields
# (verified against current code 2026-05-15 — RFC §D), so the unbound
# method formats a replayed peer turn without an agent instance. The cast
# pins the self-independent contract for type checkers; if a future
# refactor makes that branch touch ``self`` this seam must change with
# it. The branch must also stay independent of the ``event`` fields the
# synthetic event built in ``_format_peer_turn`` leaves unset —
# ``channel_id``, ``thread_id`` and ``message_id`` are all ``None``
# there; if the CHANNEL_MESSAGE branch starts reading one,
# ``_format_peer_turn`` must populate it. Reusing ``_format_event`` —
# rather than re-implementing the delimiter escape here — is the explicit
# RFC §D requirement.
_format_peer_message: Callable[[object, AgentEvent], str] = cast(
    "Callable[[object, AgentEvent], str]",
    _PromptAssemblyMixin._format_event,
)


async def build_conversation_messages(
    *,
    event: AgentEvent,
    agent_id: str,
    history_fetcher: ChannelHistoryFetcher,
    current_user_message: str,
    config: ConversationWindowConfig,
) -> list[dict[str, Any]]:
    """Return the LLM ``messages`` array seeded with the channel transcript.

    The last element is always the current event — ``current_user_message``
    is the caller's already-formatted current turn (the output of the
    persona's own ``_format_event``), appended verbatim and never dropped.
    Every earlier element is a sanitized replayed turn in chronological
    order. The first element is always a ``user`` turn: any leading
    ``assistant`` turns from the replayed transcript are dropped (see
    :func:`_drop_leading_assistant_turns`) so the array satisfies the
    Anthropic Messages API ``messages[0].role == "user"`` requirement.

    On a disabled config, a session-less / channel-less event, or any
    fetch failure, the result degrades to ``[current_event_only]`` — the
    persona is no worse off than it is without the conversation window.
    """
    current_turn: dict[str, Any] = {
        "role": "user",
        "content": current_user_message,
    }
    if not config.enabled:
        return [current_turn]

    channel_id = event.channel_id
    if not channel_id:
        # No channel scope (e.g. a TICK event) — nothing to reconstruct.
        return [current_turn]

    raw = await _fetch_window(
        history_fetcher=history_fetcher,
        channel_id=channel_id,
        message_id=event.message_id,
        agent_id=agent_id,
        # Over-fetch by one: the inbound event may already be persisted, so
        # its own row is dropped from the window (dedup by id); +1 keeps a
        # full max_turns either way, and _apply_admission trims any excess.
        limit=config.max_turns + 1,
    )
    if raw is None:
        return [current_turn]

    replayed = _assemble_replayed_turns(
        raw=raw,
        agent_id=agent_id,
        current_message_id=event.message_id,
        config=config,
    )
    return [*replayed, current_turn]


def _assemble_replayed_turns(
    *,
    raw: list[dict[str, Any]],
    agent_id: str,
    current_message_id: str | None,
    config: ConversationWindowConfig,
) -> list[dict[str, Any]]:
    """Map raw history rows to ``messages`` turns and apply admission.

    The history endpoint returns newest-first (RFC 0011 §C). Rows newer
    than the current event are dropped first (see
    :func:`_drop_rows_newer_than_current`), then the survivors are
    reversed so the transcript reads oldest→newest. The row carrying the
    current event's ``message_id`` is dropped — the caller appends the
    current event as the final turn (RFC §B). After admission, any
    leading ``assistant`` turns are dropped (see
    :func:`_drop_leading_assistant_turns`) so the replayed transcript
    can never open with a non-``user`` turn.
    """
    turns: list[dict[str, Any]] = []
    for row in reversed(_drop_rows_newer_than_current(raw, current_message_id)):
        if not isinstance(row, dict):
            continue
        if (
            current_message_id is not None
            and row.get("id") == current_message_id
        ):
            # Dedup: the current event's own row (over-fetched at
            # ``max_turns + 1``). The caller re-appends it as the final
            # turn, so it must not also replay here.
            continue
        content = row.get("content")
        if not isinstance(content, str) or not content:
            continue
        # Row admission here is deliberately lighter than the
        # ``validate_channel_message_dict`` pass ``channel_catchup.py``
        # runs on the same fetcher's rows: a row catch-up would reject
        # can still enter this transcript. Accepted asymmetry — every
        # replayed peer turn is delimiter-escaped through ``_format_event``
        # below, so an ill-formed row contributes only inert text; the
        # ``dict`` / non-empty-``str`` checks are the load-bearing guard.
        sender_id = row.get("sender_id")
        if sender_id == agent_id:
            # The persona's own prior output is replayed raw as an
            # assistant turn — never delimiter-wrapped. PR 2 newly
            # introduces this trust surface: before the conversation
            # window nothing fed a stored message back as an *assistant*
            # turn. The role split trusts ``sender_id`` (server-enforced,
            # per RFC §C — the same trust the channel-scoped history
            # endpoint already assumes); a peer row spoofed under the
            # persona's id would replay as trusted prior output. Bounded
            # exposure: the content is not delimiter-wrapped, so it still
            # cannot break out of its API role.
            turns.append({"role": "assistant", "content": content})
        else:
            turns.append(
                {"role": "user", "content": _format_peer_turn(sender_id, content)},
            )
    return _drop_leading_assistant_turns(_apply_admission(turns, config))


def _drop_rows_newer_than_current(
    raw: list[dict[str, Any]],
    current_message_id: str | None,
) -> list[dict[str, Any]]:
    """Drop history rows newer than the current event (RFC §B ordering).

    ``raw`` is newest-first (RFC 0011 §C). The orchestrator persists
    channel messages independently of the persona's per-agent event
    lock, so a message that arrives *after* the current event can land
    in the channel store *before* this window's history fetch runs. Such
    a row is strictly newer than the event the persona is answering;
    replaying it as a turn would show the model a future message ahead
    of the current one.

    The current event's own row is the ordering anchor: every row before
    it in the newest-first list is strictly newer and is dropped. The
    anchor row itself is kept here — de-duplicating it is the per-row
    ``id`` match in :func:`_assemble_replayed_turns`, a separate concern.

    When the current event is not yet persisted (no row matches
    ``current_message_id``, or it is ``None``) there is no anchor and
    ``raw`` is returned unchanged. Channel persistence is FIFO, so an
    unpersisted current event implies no strictly-newer row is persisted
    either; the residual — out-of-order persistence landing a newer row
    while the current event is still absent — is an accepted known gap.

    On a concurrent-writer race this can shrink the replayed transcript
    below ``max_turns`` (the ``+ 1`` over-fetch offsets only the dedup,
    not the dropped newer rows). A slightly shorter window beats a
    mis-ordered one, and the next turn self-corrects.
    """
    if current_message_id is None:
        return raw
    for index, row in enumerate(raw):
        if isinstance(row, dict) and row.get("id") == current_message_id:
            return raw[index:]
    return raw


def _format_peer_turn(sender_id: Any, content: str) -> str:
    """Format one replayed peer message as a user-turn string.

    Builds a synthetic ``CHANNEL_MESSAGE`` event and runs it through
    ``_format_event`` so the replayed turn inherits the exact
    ``<|user_message|>`` delimiter escape, never duplicated here (RFC §D).

    RFC 0034 Phase 2 §C/§G: the peer's identity also rides **inline** as a
    ``[<peer_id>]: `` prefix ahead of the body — the wrapper ``user_id``
    attribute alone is weak disambiguation once several peers share one
    window. It is prepended **before** ``_format_event``, so the §D escape
    covers the combined string by construction (``[``/``]`` are not
    delimiter sequences — no hole); the persona's own ``assistant`` turns
    never take this path and stay unprefixed. ``peer_label`` mirrors the
    wrapper rendering in *both* steps — the ``sender_id or "unknown"``
    fallback (no bare ``[]: ``) and the ``"`` strip on ``safe_sender``
    (PR #120 F-2) — so label and attribute are one id and cannot diverge.
    Replayed turns only; the current event is left unprefixed (RFC §G).
    """
    peer_label = (
        sender_id if isinstance(sender_id, str) and sender_id else "unknown"
    ).replace('"', "")
    synthetic = AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": f"[{peer_label}]: {content}"},
        sender_id=sender_id if isinstance(sender_id, str) else None,
        # Always "user", never the row's real participant type — this is
        # deliberate, not a stub. It forces ``_format_event`` down its §D
        # delimiter-wrap + escape branch for *every* replayed peer turn;
        # a Phase 2 agent-peer row would otherwise format unwrapped. A
        # future reader should not "fix" this to the real type.
        metadata={"sender_participant_type": "user"},
    )
    return _format_peer_message(None, synthetic)


def _apply_admission(
    turns: list[dict[str, Any]],
    config: ConversationWindowConfig,
) -> list[dict[str, Any]]:
    """Trim the replayed transcript to the configured bounds.

    OQ #2 resolution 2a: token-overflow FIFO first (drop oldest until the
    transcript fits ``max_tokens``), then count-overflow FIFO (drop
    oldest until at most ``max_turns`` remain). The caller's current
    event is appended afterwards and is never subject to this loop.
    """
    admitted = list(turns)
    tokens = [estimate_tokens(t["content"], accurate=True) for t in admitted]
    while admitted and sum(tokens) > config.max_tokens:
        admitted.pop(0)
        tokens.pop(0)
    while len(admitted) > config.max_turns:
        admitted.pop(0)
    return admitted


def _drop_leading_assistant_turns(
    turns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop any leading ``assistant`` turns from the replayed transcript.

    :func:`build_conversation_messages` returns ``[*replayed,
    current_turn]`` and ``current_turn`` is always a ``user`` turn — but
    ``replayed`` carries no guarantee that *its* first element is one.
    The role split in :func:`_assemble_replayed_turns` maps the persona's
    own messages to ``role="assistant"``, and two routine paths leave
    such a turn at the front of the transcript:

    * **Persona-first channel.** The persona sent the channel's opening
      message — a greeting or a proactive turn — so the oldest replayed
      row is its own ``assistant`` turn.
    * **Token-FIFO admission.** :func:`_apply_admission` FIFO-drops a
      content-size-dependent number of oldest turns; evicting an odd
      count from a ``user``-leading alternating transcript leaves an
      ``assistant``-leading one.

    The Anthropic Messages API requires ``messages[0]`` to use the
    ``user`` role — a leading ``assistant`` turn is a hard 400
    (``messages: first message must use the "user" role``). The persona
    runtime passes this seed to the provider unmodified
    (``AnthropicProvider.create_message``), so the guard must live here.
    A leading ``assistant`` turn has no preceding ``user`` turn for the
    model to answer, so dropping it loses no model-relevant context; if
    every replayed turn is an ``assistant`` turn the transcript empties
    and :func:`build_conversation_messages` degrades to
    current-event-only.

    Runs as the final admission step: it only ever *shrinks* the
    transcript, so the :func:`_apply_admission` token / count bounds
    still hold afterwards.
    """
    for index, turn in enumerate(turns):
        if turn["role"] == "user":
            return turns[index:]
    return []
