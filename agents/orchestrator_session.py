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

The session also shows the agent's token (RFC 0009 Phases 3–4 plan, PR 7),
which proves the ID. A token changes each time the agent registers, so
:func:`token_middleware` adds the current one to each call rather than the
session fixing one at the start, and it is what acts when a call is refused
for its token. Registration is the one call without it: see
:func:`enrollment_headers`.
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

import aiohttp

from .server_reregister import AgentTokenKeeper
from .wallet_client import AGENT_ID_METADATA_KEY, AGENT_TOKEN_METADATA_KEY

# One name for both transports: HTTP header names ignore case, so the REST
# session sends the wallet's gRPC metadata key as it is, and the
# orchestrator reads it as X-Agent-ID.
AGENT_ID_HEADER = AGENT_ID_METADATA_KEY
AGENT_TOKEN_HEADER = AGENT_TOKEN_METADATA_KEY

# The fleet enrollment secret, under the name the orchestrator reads it by, so
# a deployment sets one setting for both.
ENROLLMENT_SECRET_ENV = "SECURITY_ENROLLMENT_SECRET"
ENROLLMENT_SECRET_HEADER = "X-Enrollment-Secret"

# The ``code`` of a 401 that refuses a call for its token, in the orchestrator's
# usual error body. Any other 401 is about something else, and is left alone.
TOKEN_INVALID = "AGENT_TOKEN_INVALID"
TOKEN_EXPIRED = "AGENT_TOKEN_EXPIRED"
TOKEN_REVOKED = "AGENT_TOKEN_REVOKED"
_TOKEN_REFUSALS = frozenset({TOKEN_INVALID, TOKEN_EXPIRED, TOKEN_REVOKED})

_DEFAULT_PORTS = {"http": 80, "https": 443}


def enrollment_headers() -> dict[str, str]:
    """What a registration shows in place of a token, which it is about to get.

    Empty with no secret set. The value is trimmed as the orchestrator trims
    its copy: a secret read from a file often ends in a line break, which no
    header can hold. Registration follows no redirect, so the secret cannot
    reach a host other than the orchestrator's.
    """
    secret = os.environ.get(ENROLLMENT_SECRET_ENV, "").strip()
    return {ENROLLMENT_SECRET_HEADER: secret} if secret else {}


async def _refused_token_code(response: aiohttp.ClientResponse) -> str | None:
    """The refusal a 401 names, or ``None`` for any other answer.

    Reading the body keeps it for the caller, which may still read it.
    """
    if response.status != 401:
        return None
    try:
        body = json.loads(await response.read())
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if code in _TOKEN_REFUSALS else None


def token_middleware(
    orchestrator_url: str, tokens: AgentTokenKeeper,
) -> aiohttp.ClientMiddlewareType:
    """Show the agent's token on each call, and act when a call is refused for it.

    A refused call is sent once more when ``tokens`` has a newer token to show,
    and its second answer stands, whatever it is, so nothing here can loop. The
    orchestrator refuses a call before acting on it, so sending it again cannot
    do its work twice.

    aiohttp runs this for every hop of a redirect, so the origin check is what
    keeps the token off a host a redirect names (the plan's G-5).
    """
    target = urlsplit(orchestrator_url)
    orchestrator = (
        target.scheme, target.hostname, target.port or _DEFAULT_PORTS.get(target.scheme),
    )

    async def middleware(
        request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType,
    ) -> aiohttp.ClientResponse:
        used = tokens.token
        if not used or (request.url.scheme, request.url.host, request.url.port) != orchestrator:
            return await handler(request)
        request.headers[AGENT_TOKEN_HEADER] = used
        response = await handler(request)
        code = await _refused_token_code(response)
        if code is None:
            return response
        newer = await tokens.after_refusal(used, revoked=code == TOKEN_REVOKED)
        if not newer:
            return response
        request.headers[AGENT_TOKEN_HEADER] = newer
        return await handler(request)

    return middleware


def new_orchestrator_session(
    agent_id: str | None,
    orchestrator_url: str = "",
    tokens: AgentTokenKeeper | None = None,
) -> aiohttp.ClientSession:
    """Open the shared session, sending ``agent_id`` on every call.

    A process hosts one agent today, so the server passes that agent's ID, as
    it does for the wallet. With no agent hosted it passes an empty ID and
    the session sends no header. With ``tokens``, every call to
    ``orchestrator_url`` also shows the agent's token.
    """
    headers = {AGENT_ID_HEADER: agent_id} if agent_id else None
    middlewares = () if tokens is None else (token_middleware(orchestrator_url, tokens),)
    return aiohttp.ClientSession(headers=headers, middlewares=middlewares)
