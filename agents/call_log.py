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
* ``purpose`` — what the call was for, from :class:`agents.llm_types.LLMCallPurpose`;
* ``started_at`` — real time, UTC, when the call began (never agent time);
* the provider, model and alias, and the token counts, cache reads and
  writes apart;
* ``error`` — the exception's class name when the call failed, else null.

Both settings are read on first use and fixed for the life of the process.
A process that serves many meetings in turn, such as an experiment harness
making its own calls, wraps each block of calls in :func:`scoped` instead;
inside it, that file and those tags replace the settings. A write that fails
is logged and the call goes on: losing a line must not lose the reply it
paid for.
"""

from __future__ import annotations

import contextvars
import functools
import json
import logging
import os
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from typing import TypeGuard

from .llm_types import LLMCallPurpose, Usage
from .observability.metrics import current_agent_id

logger = logging.getLogger(__name__)

CALL_LOG_ENV = "PERSATRIX_CALL_LOG"
CALL_TAGS_ENV = "PERSATRIX_CALL_TAGS"

# The file and tags a :func:`scoped` block names, or None outside one.
_scope: contextvars.ContextVar[tuple[str, dict[str, str]] | None] = contextvars.ContextVar(
    "persatrix_call_log_scope", default=None,
)


@functools.cache
def call_log_path() -> str | None:
    """The file named by the setting, or None when the log is off."""
    return os.environ.get(CALL_LOG_ENV, "").strip() or None


@functools.cache
def call_tags() -> dict[str, str]:
    """The fixed tags from the setting."""
    raw = os.environ.get(CALL_TAGS_ENV, "").strip()
    if not raw:
        return {}
    try:
        tags = json.loads(raw)
    except ValueError:
        tags = None
    if not _is_string_map(tags):
        raise ValueError(f"{CALL_TAGS_ENV} must be a JSON object of strings; got {raw!r}")
    return tags


def _is_string_map(tags: object) -> TypeGuard[dict[str, str]]:
    return isinstance(tags, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in tags.items()
    )


def scoped(path: str | os.PathLike[str], tags: Mapping[str, str]) -> AbstractContextManager[None]:
    """Log the calls made inside the block to *path*, tagged *tags*."""
    if not _is_string_map(dict(tags)):
        raise ValueError(f"call log tags must be strings; got {dict(tags)!r}")
    return _scoped(os.fspath(path), dict(tags))


@contextmanager
def _scoped(path: str, tags: dict[str, str]) -> Iterator[None]:
    token = _scope.set((path, tags))
    try:
        yield
    finally:
        _scope.reset(token)


def reset_call_log() -> None:
    """Forget both settings, so the next call reads them again (tests)."""
    call_log_path.cache_clear()
    call_tags.cache_clear()


def record_call(
    *,
    started_at: float,
    purpose: LLMCallPurpose | None,
    provider: str,
    model: str,
    model_alias: str | None,
    usage: Usage | None,
    error: BaseException | None,
) -> None:
    """Append one line for a finished call, when the log is on."""
    scope = _scope.get()
    if scope is not None:
        path, tags = scope
    else:
        setting = call_log_path()
        if setting is None:
            return
        path = setting
        try:
            tags = call_tags()
        except ValueError:
            # The server checks the tags at startup; reaching here means they
            # were never checked, and the reply matters more than the line.
            logger.exception("call log: bad %s; line not written", CALL_TAGS_ENV)
            return
    usage = usage or Usage(0, 0)
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
    "scoped",
]
