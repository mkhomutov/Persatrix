"""The HTTP session an agent process uses for its REST calls to the orchestrator.

The orchestrator's RFC 0009 rate limiter gives each agent ID its own budget,
read from the ``X-Agent-ID`` header (``internal/security/middleware.go``,
``AgentIDHeader``). A call without it falls into the one budget that the CLI
and every other caller sending no ID share, so the session sends the hosted
agent's ID on every call: registration, channel reads and publishes, convene
callbacks and verbatim recall (ISSUE-0152 step 3). The wallet's gRPC calls
send the same ID as metadata (ISSUE-0111).

Every call on this session goes to the orchestrator. The ``http_request`` tool
opens a session of its own, so the ID never reaches an outside host.
"""

from __future__ import annotations

import aiohttp

from .wallet_client import AGENT_ID_METADATA_KEY

# One name for both transports: HTTP header names ignore case, so the REST
# session sends the wallet's gRPC metadata key as it is, and the
# orchestrator reads it as X-Agent-ID.
AGENT_ID_HEADER = AGENT_ID_METADATA_KEY


def new_orchestrator_session(agent_id: str | None) -> aiohttp.ClientSession:
    """Open the shared session, sending ``agent_id`` on every call.

    A process hosts one agent today, so the server passes that agent's ID, as
    it does for the wallet. With no agent hosted it passes an empty ID and
    the session sends no header.
    """
    headers = {AGENT_ID_HEADER: agent_id} if agent_id else None
    return aiohttp.ClientSession(headers=headers)
