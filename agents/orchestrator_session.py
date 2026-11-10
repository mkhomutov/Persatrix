"""The HTTP session an agent process uses for its REST calls to the orchestrator.

The orchestrator's RFC 0009 rate limiter gives each agent ID its own budget,
read from the ``X-Agent-ID`` header (``internal/security/middleware.go``,
``AgentIDHeader``). A call without it falls into the one budget that the CLI
and every other caller sending no ID share, so the session sends the hosted
agent's ID on every call: registration, channel reads and publishes, convene
callbacks and verbatim recall (ISSUE-0152 step 3). The wallet's gRPC calls
send the same ID as metadata (ISSUE-0111).

Every call on this session goes to the orchestrator. The ``http_request`` tool
opens a session of its own, so the ID never reaches a host a model chose. A
redirect the orchestrator answers with is followed, and it keeps the ID, which
proves nothing on its own, but not the token.

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

import aiohttp
from yarl import URL

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


# The secret once :func:`hold_enrollment_secret` has taken it out of the
# environment; empty until then.
_held_enrollment_secret = ""


def hold_enrollment_secret() -> None:
    """Move the enrollment secret out of the environment, into this process's memory.

    Called once at startup, before anything starts a process, so no process
    the agent starts, by any means, inherits the secret (RFC 0009 G-14).
    :func:`enrollment_headers` still shows it. On Linux the process's starting
    environment stays readable in ``/proc/<pid>/environ``; this cannot change it.
    """
    global _held_enrollment_secret
    _held_enrollment_secret = os.environ.pop(ENROLLMENT_SECRET_ENV, "") or _held_enrollment_secret


def enrollment_headers() -> dict[str, str]:
    """What a registration shows in place of a token, which it is about to get.

    Empty with no secret set. The secret is read from the environment, or
    from memory once :func:`hold_enrollment_secret` has moved it there. The
    value is trimmed as the orchestrator trims its copy: a secret read from a
    file often ends in a line break, which no header can hold. Registration
    follows no redirect, so the secret cannot reach a host other than the
    orchestrator's.
    """
    secret = (os.environ.get(ENROLLMENT_SECRET_ENV) or _held_enrollment_secret).strip()
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
    return code if isinstance(code, str) and code in _TOKEN_REFUSALS else None


async def _send_showing(
    token: str, request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType,
) -> aiohttp.ClientResponse:
    """Send ``request`` with ``token``, which comes off it once the answer is back.

    aiohttp's response errors read these same headers and their ``repr`` prints
    them, and ``agents/event_loop.py`` logs a failed wake with ``%r``.
    """
    request.headers[AGENT_TOKEN_HEADER] = token
    try:
        return await handler(request)
    finally:
        del request.headers[AGENT_TOKEN_HEADER]


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
    target = URL(orchestrator_url)
    orchestrator = (target.scheme, target.host, target.port)

    async def middleware(
        request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType,
    ) -> aiohttp.ClientResponse:
        used = tokens.token
        if not used or (request.url.scheme, request.url.host, request.url.port) != orchestrator:
            return await handler(request)
        response = await _send_showing(used, request, handler)
        code = await _refused_token_code(response)
        if code is None:
            return response
        newer = await tokens.after_refusal(used, revoked=code == TOKEN_REVOKED)
        if not newer:
            return response
        return await _send_showing(newer, request, handler)

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
