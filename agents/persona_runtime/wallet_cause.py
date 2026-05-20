"""RFC 0023 — wallet-lease ``cause`` derivation for the persona action loop.

Free-function helper (no ``self`` access) split out of ``action_loop.py``
so that file stays under the 500-line review limit, mirroring the
``channel_ingest`` / ``channel_reply`` split convention.
"""

from __future__ import annotations

from ..generated import wallet_pb2 as walletpb
from ..persona_types import AgentEvent, EventType

__all__ = ["cause_for_event"]


def cause_for_event(event: AgentEvent) -> walletpb.Cause.ValueType:
    """Pick the RFC 0023 lease ``cause`` for an event handled by the loop.

    The persona action loop is the LLM-call site for both chat
    (``SendChatMessage``) and receiver-side channel messages
    (``ReceiveChannelMessage``); they share the ``CHANNEL_MESSAGE`` event
    type but route through different wallet causes. The discriminator is
    ``metadata["chat_session_id"]`` — the chat servicer always sets it
    (RFC 0016 OQ 9), the channel-message servicer never does.

    PR 4 wires the ``CAUSE_CHAT`` arm. Receiver-side channel and TICK
    arms stay ``CAUSE_UNSPECIFIED`` (``LLMClient.create_message`` then
    skips the wallet bracket and behaves as in v0.2.3); PR 5 maps TICK
    to ``CAUSE_AUTONOMOUS_TICK``, PR 6 maps receiver-side channel
    events to ``CAUSE_CHANNEL_MESSAGE``.
    """
    if (
        event.event_type is EventType.CHANNEL_MESSAGE
        and "chat_session_id" in event.metadata
    ):
        return walletpb.CAUSE_CHAT
    return walletpb.CAUSE_UNSPECIFIED
