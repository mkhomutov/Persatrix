"""RFC 0034 Phase 1 — conversation-window agent integration mixin.

The conversation-window substrate (:mod:`conversation_window`, RFC 0034
PR 2) reconstructs the LLM ``messages`` array from the channel store so
the model sees the in-progress conversation as a transcript instead of a
single isolated message (ISSUE-0052). This mixin is the seam that wires
that substrate into :class:`_LLMPersonaAgent`:

* it accepts the channel-history fetcher injected post-construction by
  :meth:`agents.server.AgentServer.start` (the agent is built in
  ``load_agent`` before the shared ``aiohttp`` session exists), and
* it builds the per-turn ``messages`` seed the action loop hands to the
  LLM.

Carved into its own mixin — the way ``_LLMPersonaAgent`` already
composes ``_ActionLoopMixin`` / ``_MemoryContextMixin`` / … — so that
neither ``action_loop.py`` nor the runtime package ``__init__`` grows
past the 500-line file-size cap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .conversation_window import (
    ConversationWindowConfig,
    build_conversation_messages,
    resolve_conversation_window_config,
)

if TYPE_CHECKING:
    from ..channel_history_fetcher import ChannelHistoryFetcher
    from ..persona_types import AgentEvent

__all__ = ["_ConversationWindowMixin"]


class _ConversationWindowMixin:
    """Conversation-window seam for ``_LLMPersonaAgent`` (RFC 0034 Phase 1)."""

    # Provided by BaseAgent / PersonaAgent — declared for type checkers.
    agent_id: str
    config: dict[str, Any]

    # The channel-history fetcher is injected post-construction by
    # ``AgentServer.start()`` once the shared aiohttp session is open.
    # ``None`` until then (and on task-only / partial-init test paths):
    # ``_build_seed_messages`` then seeds with the current event alone.
    _history_fetcher: ChannelHistoryFetcher | None = None
    # Resolved lazily — and cached — on the first turn that actually
    # builds a window (i.e. with a fetcher wired); the unwired
    # short-circuit in ``_build_seed_messages`` resolves nothing, since
    # the resolved config would only be discarded. The config dict is
    # immutable after construction, so one lazy resolve is equivalent to
    # resolving at construction (RFC 0034 PR plan §PR 3) while keeping the
    # runtime ``__init__`` under the file-size cap.
    _conversation_window_config: ConversationWindowConfig | None = None

    def set_history_fetcher(self, fetcher: ChannelHistoryFetcher) -> None:
        """Inject the RFC 0034 conversation-window channel-history fetcher.

        Called by :meth:`agents.server.AgentServer.start` once the shared
        :class:`aiohttp.ClientSession` is open — the agent is constructed
        before that session exists, so the fetcher is wired
        post-construction (the same shape the dispatcher uses to receive
        its channel publisher).  Until this runs the seed degrades to the
        current event alone (pre-RFC-0034 behaviour).
        """
        self._history_fetcher = fetcher

    async def _build_seed_messages(
        self, event: AgentEvent, current_user_message: str,
    ) -> list[dict[str, Any]]:
        """Return the LLM ``messages`` seed for one persona turn.

        With a history fetcher wired this is the reconstructed
        conversation window (RFC 0034 §A) — the in-progress channel
        transcript with ``current_user_message`` appended last.  Without
        one (task-only test paths, partial init) the seed degrades to the
        current event alone, identical to pre-RFC-0034 behaviour.
        """
        # The ``None``-fetcher check runs first so the short-circuit is
        # total: an unwired persona resolves no conversation-window config
        # (it would only be discarded — the config feeds nothing but the
        # ``build_conversation_messages`` call below).
        if self._history_fetcher is None:
            return [{"role": "user", "content": current_user_message}]
        if self._conversation_window_config is None:
            self._conversation_window_config = resolve_conversation_window_config(
                self.config,
            )
        return await build_conversation_messages(
            event=event,
            agent_id=self.agent_id,
            history_fetcher=self._history_fetcher,
            current_user_message=current_user_message,
            config=self._conversation_window_config,
        )
