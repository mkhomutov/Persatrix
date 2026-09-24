"""What the harness asks a deployment's orchestrator, and reads in its log.

The harness drives each channel meeting over the orchestrator's REST API, as
an operator would: it posts the operator's messages, reads the channel's
messages and, for the memo turn, disarms the channel and makes the other
advisers observers. Its requests carry an ID of their own, so the
orchestrator's audit log can tell them from the advisers'.

Nothing on the REST API says when a discussion has closed; the orchestrator
logs it. So the harness also reads the orchestrator's log: each close, with
its channel and trigger; each discussion cut at the cascade-depth cap; every
lease the wallet refused, telling a spending limit's refusal (check 6) from
any other; and the startup lines that say the rate limiter is off and the
wallet is served.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

import aiohttp

HARNESS_ID = "exp001-harness"
# The newest messages the orchestrator returns in one read, its own maximum.
_HISTORY_LIMIT = 1000
# One request's limit; every request is a quick read or write on loopback.
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
# The lines the harness reads; a test holds each to the Go source that logs it.
_END_VOTE_CLOSE = "channels: interaction closed by end-of-interaction votes"
_BOUNDED_CLOSE = "channels: interaction closed by RFC 0052 bounded close"
_CLOSES = frozenset({_END_VOTE_CLOSE, _BOUNDED_CLOSE})
_DEPTH_CAP = (
    "channels: autonomous discussion reached the cascade-depth cap; running the structural close"
)
# A spending limit's refusal, the one check 6 is about. Every other refusal
# starts with one of _REFUSED_LEASE.
_SPENDING_LIMIT_REFUSAL = "wallet: lease denied — budget exceeded"
_REFUSED_LEASE = ("wallet: lease denied", "wallet: request rejected")
_RATE_LIMIT_OFF = "security.rate_limit.disabled scope=startup"
# Both are logged only when the orchestrator serves the wallet; without it,
# every adviser's model call is refused before any lease is asked for.
_WALLET_SERVED = frozenset({"wallet lease enforcement initialized", "gRPC server listening"})
# Go writes up to nine digits of a second; keep six, which Python reads.
_FRACTION = re.compile(r"(\.\d{6})\d+")


class OrchestratorError(RuntimeError):
    """The orchestrator refused a request, or answered one in a shape the harness cannot read."""


def _instant(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(_FRACTION.sub(r"\1", text.replace("Z", "+00:00")))


@dataclass(frozen=True)
class Message:
    """One channel message, as the orchestrator stored it; its time is real time."""

    id: str
    sender: str
    content: str
    at: dt.datetime
    mentions: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def interaction(self) -> str | None:
        """The interaction the orchestrator filed the message under."""
        value = self.metadata.get("interaction_id")
        return value if isinstance(value, str) else None

    @property
    def closed_before(self) -> tuple[str, str] | None:
        """The interaction that closed just before this message opened a new
        one, and how it closed; None when the message opened none."""
        closed = self.metadata.get("previous_interaction_id")
        trigger = self.metadata.get("previous_interaction_close_trigger")
        if isinstance(closed, str) and isinstance(trigger, str):
            return closed, trigger
        return None

    @classmethod
    def read(cls, raw: Any) -> Message:
        try:
            return cls(
                id=str(raw["id"]),
                sender=str(raw["sender_id"]),
                content=str(raw["content"]),
                at=_instant(str(raw["timestamp"])),
                mentions=tuple(str(m) for m in raw.get("mentions") or ()),
                metadata=MappingProxyType(dict(raw.get("metadata") or {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise OrchestratorError(f"a message the harness cannot read: {raw!r}") from exc


class Orchestrator:
    """The orchestrator's REST API, as far as the harness uses it."""

    def __init__(
        self, base_url: str, session: aiohttp.ClientSession, *,
        timeout: aiohttp.ClientTimeout = _REQUEST_TIMEOUT,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._session = session
        self._headers = {"X-Agent-ID": HARNESS_ID}
        self._timeout = timeout

    async def healthy(self) -> bool:
        try:
            async with self._session.get(
                f"{self._base}/healthz", headers=self._headers, timeout=self._timeout,
            ) as r:
                return r.status == 200
        except (aiohttp.ClientError, TimeoutError):
            return False

    async def agents(self) -> set[str]:
        """The IDs of the agents registered right now."""
        listed = await self._call("GET", "/api/v1/agents")
        return {str(agent["id"]) for agent in listed}

    async def post(
        self, channel: str, sender: str, content: str, *, mentions: Sequence[str] = (),
    ) -> Message:
        """Post *content* as *sender*, word for word; return the message as stored."""
        body = {"sender_id": sender, "content": content, "mentions": list(mentions)}
        return Message.read(await self._call("POST", f"/api/v1/channels/{channel}/messages", body))

    async def messages(self, channel: str) -> list[Message]:
        """The channel's messages, oldest first."""
        path = f"/api/v1/channels/{channel}/messages?limit={_HISTORY_LIMIT}"
        newest_first = [Message.read(raw) for raw in (await self._call("GET", path))["messages"]]
        if len(newest_first) >= _HISTORY_LIMIT:
            raise OrchestratorError(f"{channel} holds more messages than one read returns")
        return newest_first[::-1]

    async def activity(self, channel: str) -> set[str]:
        """The members the orchestrator has sent a turn to and awaits a reply from."""
        answer = await self._call("GET", f"/api/v1/channels/{channel}/activity")
        return {str(member) for member in answer["thinking"]}

    async def disarm(self, channel: str) -> None:
        """Turn the channel's autonomous mode off; no new discussion opens after this."""
        config = await self._call("GET", f"/api/v1/channels/{channel}/config")
        await self._call(
            "PATCH", f"/api/v1/channels/{channel}/config", {"autonomous": {"enabled": False}},
            headers={"If-Match": str(config["revision"])},
        )

    async def set_respond(self, channel: str, member: str, respond: str) -> None:
        await self._call(
            "PATCH", f"/api/v1/channels/{channel}/members/{member}", {"respond": respond},
        )

    async def _call(
        self, method: str, path: str, body: Any = None, *, headers: Mapping[str, str] | None = None,
    ) -> Any:
        try:
            async with self._session.request(
                method, f"{self._base}{path}", json=body,
                headers={**self._headers, **(headers or {})}, timeout=self._timeout,
            ) as response:
                status, text = response.status, await response.text()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise OrchestratorError(f"{method} {path}: {exc!r}") from exc
        if status >= 400:
            raise OrchestratorError(f"{method} {path}: {status} {text[:300]}")
        try:
            return json.loads(text) if text else None
        except ValueError as exc:
            raise OrchestratorError(f"{method} {path}: not JSON: {text[:300]}") from exc


@dataclass(frozen=True)
class Close:
    """A discussion the orchestrator closed: where, which, how and when (real time)."""

    channel: str
    interaction: str
    trigger: str
    at: dt.datetime


@dataclass(frozen=True)
class OrchestratorLog:
    closes: tuple[Close, ...]
    depth_capped: frozenset[str]  # interactions a discussion ran to the depth cap in
    spending_limit_refusals: tuple[str, ...]  # "agent: message", in log order (check 6)
    refused_leases: tuple[str, ...]  # every other refusal, the same way
    rate_limit_off: bool
    wallet_served: bool

    def closes_in(self, channel: str) -> tuple[Close, ...]:
        return tuple(c for c in self.closes if c.channel == channel)


def read_orchestrator_log(path: Path) -> OrchestratorLog:
    """What the orchestrator has logged so far; a line that is not JSON is skipped."""
    closes: list[Close] = []
    capped: set[str] = set()
    spending: list[str] = []
    refused: list[str] = []
    rate_limit_off = False
    served: set[str] = set()
    lines = path.read_text(errors="replace").splitlines() if path.exists() else []
    for text in lines:
        try:
            line = json.loads(text)
        except ValueError:
            continue
        if not isinstance(line, dict):
            continue
        message = str(line.get("message", ""))
        if message in _CLOSES:
            closes.append(Close(
                channel=str(line["channel_id"]), interaction=str(line["interaction_id"]),
                trigger=str(line["trigger"]), at=_instant(str(line["timestamp"])),
            ))
        elif message == _DEPTH_CAP:
            capped.add(str(line["interaction_id"]))
        elif message == _SPENDING_LIMIT_REFUSAL:
            spending.append(f"{line.get('agent_id', '?')}: {message}")
        elif message.startswith(_REFUSED_LEASE):
            refused.append(f"{line.get('agent_id', '?')}: {message}")
        elif message == _RATE_LIMIT_OFF:
            rate_limit_off = True
        elif message in _WALLET_SERVED:
            served.add(message)
    return OrchestratorLog(
        closes=tuple(closes), depth_capped=frozenset(capped),
        spending_limit_refusals=tuple(spending), refused_leases=tuple(refused),
        rate_limit_off=rate_limit_off, wallet_served=served == _WALLET_SERVED,
    )
