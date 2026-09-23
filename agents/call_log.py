"""The call log: one JSON line for every model call an agent makes.

An experiment has to account for every model call, whichever agent made it,
and each agent is its own process. So when ``PERSATRIX_CALL_LOG`` names a
file, :class:`agents.llm_client.LLMClient` appends one line to it per call,
failed calls included. Several agents may share the file: each line goes out
in a single append, so lines from different processes do not mix.

A line holds:

* ``tags`` — the fixed labels from ``PERSATRIX_CALL_TAGS``, a JSON object of
  strings the operator sets per process (EXP-001 sets the arm, series,
  meeting and attempt); ``{}`` when unset;
* ``agent_id`` — the process's agent;
* ``purpose`` — what the call was for, from :class:`agents.llm_types.CallPurpose`;
* ``started_at`` — real time, UTC, when the call began (never agent time);
* the provider, model and alias, and the token counts, cache reads and
  writes apart;
* ``error`` — the exception's class name when the call failed, else null.

Both settings are read on first use and fixed for the life of the process.
A write that fails is logged and the call goes on: losing a line must not
lose the reply it paid for.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

from .llm_types import CallPurpose, Usage
from .observability.metrics import current_agent_id

logger = logging.getLogger(__name__)

CALL_LOG_ENV = "PERSATRIX_CALL_LOG"
CALL_TAGS_ENV = "PERSATRIX_CALL_TAGS"

_UNREAD = object()
_path: object = _UNREAD  # str | None once read
_tags: dict[str, str] | None = None


def call_log_path() -> str | None:
    """The file to append to, or None when the log is off."""
    global _path
    if _path is _UNREAD:
        _path = os.environ.get(CALL_LOG_ENV, "").strip() or None
    return _path  # type: ignore[return-value]


def call_tags() -> dict[str, str]:
    """The fixed tags every line carries."""
    global _tags
    if _tags is None:
        _tags = _read_tags()
    return _tags


def _read_tags() -> dict[str, str]:
    raw = os.environ.get(CALL_TAGS_ENV, "").strip()
    if not raw:
        return {}
    try:
        tags = json.loads(raw)
    except ValueError:
        tags = None
    if not isinstance(tags, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in tags.items()
    ):
        raise ValueError(f"{CALL_TAGS_ENV} must be a JSON object of strings; got {raw!r}")
    return tags


def reset_call_log() -> None:
    """Forget both settings, so the next call reads them again (tests)."""
    global _path, _tags
    _path = _UNREAD
    _tags = None


def record_call(
    *,
    started_at: float,
    purpose: CallPurpose | None,
    provider: str,
    model: str,
    model_alias: str | None,
    usage: Usage | None,
    error: BaseException | None,
) -> None:
    """Append one line for a finished call, when the log is on."""
    path = call_log_path()
    if path is None:
        return
    usage = usage or Usage(0, 0)
    try:
        tags = call_tags()
    except ValueError:
        # The server checks the tags at startup; reaching here means they
        # were never checked, and the reply still matters more than the line.
        logger.exception("call log: bad %s; line not written", CALL_TAGS_ENV)
        return
    line = {
        "tags": tags,
        "agent_id": current_agent_id(),
        "purpose": purpose.value if purpose is not None else None,
        "provider": provider,
        "model": model,
        "model_alias": model_alias,
        "started_at": datetime.fromtimestamp(started_at, UTC).isoformat(),
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "error": type(error).__name__ if error is not None else None,
    }
    data = (json.dumps(line) + "\n").encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except OSError:
        logger.exception("call log: could not append to %s", path)


__all__ = [
    "CALL_LOG_ENV",
    "CALL_TAGS_ENV",
    "call_log_path",
    "call_tags",
    "record_call",
    "reset_call_log",
]
