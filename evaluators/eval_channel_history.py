"""RFC 0044 Phase 1 — an in-process channel-history fetcher for the eval driver.

The persona runtime's RFC 0034 conversation window (working memory) reconstructs
the ``messages`` array from a :class:`agents.channel_history_fetcher.ChannelHistoryFetcher`
— in production an HTTP call to ``GET /channels/{id}/messages``. The RFC 0044
eval driver has no orchestrator to fetch from, so without a fetcher every turn
degrades to current-event-only (``agents/persona_runtime/conversation_seed.py``):
the persona never sees its own prior turn, and a working-memory recipe would be a
vacuous regression bar.

:class:`InProcessChannelHistory` is that missing fetcher, backed by an in-memory
list the driver appends each turn to. It conforms structurally to the
``ChannelHistoryFetcher`` Protocol (``async fetch(channel_id, *, limit,
as_participant=None)``) so
:meth:`agents.persona_runtime.conversation_seed._SeedMixin.set_history_fetcher`
accepts it. Wiring it lets a recipe that declares ``setup.channel`` drive the real
RFC 0034 window: turn *n*'s prompt carries the reconstructed in-channel transcript
(prior peer turns + the persona's own replies), so a regression in the
working-memory → prompt-assembly path shifts the request and misses the golden.

Kept **pure** (no ``agents`` import) — like the assertion core, it holds no
runtime dependency, so it is unit-testable in isolation. The driver (which already
imports the runtime) owns the wiring.

Determinism (RFC 0044 §D): the store returns exactly what was appended, newest
first, so a golden recorded once replays byte-stably given the driver appends the
same turns with the same deterministic message ids each run.
"""

from __future__ import annotations

from typing import Any

__all__ = ["InProcessChannelHistory"]


class InProcessChannelHistory:
    """A ``ChannelHistoryFetcher`` backed by an in-memory, append-only log.

    The driver calls :meth:`append` once per delivered turn (the inbound user
    message, then the persona's reply) and the persona runtime calls
    :meth:`fetch` during prompt assembly. Rows are the minimal shape the
    conversation window reads (``id`` / ``sender_id`` / ``content``,
    ``agents/persona_runtime/conversation_window.py``); other endpoint fields are
    omitted because the window ignores them.
    """

    def __init__(self) -> None:
        # channel_id -> chronological (oldest-first) list of history rows.
        self._by_channel: dict[str, list[dict[str, Any]]] = {}

    def append(
        self, *, channel_id: str, message_id: str, sender_id: str, content: str
    ) -> None:
        """Record one delivered message in ``channel_id`` (chronological order).

        The driver appends the inbound user turn *before* dispatching it to the
        persona (so it is the ordering anchor the window dedups against, matching
        how the orchestrator persists an inbound message before the persona acts)
        and the persona's reply *after* — so the log reads user, assistant, user,
        …, exactly the transcript the window replays.
        """
        self._by_channel.setdefault(channel_id, []).append(
            {"id": message_id, "sender_id": sender_id, "content": content}
        )

    async def fetch(
        self,
        channel_id: str,
        *,
        limit: int,
        as_participant: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the last ``limit`` messages of ``channel_id``, newest-first.

        Mirrors the ``ChannelHistoryFetcher`` contract: newest-first ordering
        (RFC 0011 §C — the window reverses it back to chronological) and ``[]`` for
        an unknown/empty channel. It never returns the Protocol's ``None`` (an
        in-memory log has no best-effort failure mode) — the narrower return type
        is a covariant subtype, so structural conformance to the seam holds.
        ``as_participant`` (RFC 0036 §G membership scoping) is accepted for that
        conformance but ignored: an eval runs a single persona present for the
        whole channel, so there is no removal gap to scope out.
        """
        rows = self._by_channel.get(channel_id, [])
        return list(reversed(rows))[:limit]
