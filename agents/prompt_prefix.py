"""The cached prompt prefix: text every persona turn carries first.

An operator can give an agent process text that each of its persona turns
carries ahead of the rest of its prompt, marked for the provider's prompt
cache (the ``cache_prefix`` of :meth:`agents.llm_client.LLMClient.create_message`).
``PERSATRIX_PROMPT_PREFIX`` names a UTF-8 file that holds it. EXP-001's arm
D′ puts the full transcripts of a series' earlier meetings there: its
advisers have no memory, and the pre-registration gives them the transcripts
wherever arm D's prompts receive recalled memory.

Recalled memory reaches one prompt: the persona's turn, every call of the
turn's tool loop (:mod:`agents.persona_runtime.action_loop`), so each of
those calls carries the prefix. The salience bid, the reflexion critic and
revise passes, the close summary and working-memory compression build
prompts of their own that memory never reaches, and none carries it. Every
call that carries it offers the same tools, which the provider reads first,
and then the same text, so the provider keeps one cache entry for all of
them: the first call writes it and the calls after it read it.

The file is read on first use, word for word with no newline translation,
and fixed for the life of the process. The agent server reads it at
startup, so a setting that names a missing or empty file stops the agent
there. Without the setting there is no prefix, and no call carries a cache
marker.

The provider keeps the entry for five minutes after the last call that
wrote or read it began, and a group discussion can go quiet for longer.
``PERSATRIX_PROMPT_PREFIX_KEEPALIVE`` names, in seconds, how long the room
may go without a call that carries the prefix before this process keeps the
entry alive (:mod:`agents.prefix_keepalive`). It needs the
prefix, and the call log the room shares, which says when the prefix was
last used; a setting without them, or one that does not fit inside the
entry's five minutes, stops the agent at startup too.
"""

from __future__ import annotations

import functools
import math
import os
from pathlib import Path

from .call_log import CALL_LOG_ENV, call_log_path

PROMPT_PREFIX_ENV = "PERSATRIX_PROMPT_PREFIX"
PROMPT_PREFIX_KEEPALIVE_ENV = "PERSATRIX_PROMPT_PREFIX_KEEPALIVE"
# A cache entry lives this long after the last call that wrote or read it
# began: the provider's five-minute cache, whose write price EXP-001 fixes.
CACHE_LIFETIME_SECONDS = 300.0


@functools.cache
def prompt_prefix() -> str:
    """The prefix the setting's file holds; empty without the setting."""
    setting = os.environ.get(PROMPT_PREFIX_ENV, "").strip()
    if not setting:
        return ""
    try:
        text = Path(setting).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{PROMPT_PREFIX_ENV}: cannot read {setting}: {exc}") from exc
    if not text.strip():
        raise ValueError(f"{PROMPT_PREFIX_ENV}: {setting} holds no text")
    return text


@functools.cache
def prefix_keepalive_seconds() -> float | None:
    """How long the room may go without a call that carries the prefix
    before this process keeps its cache entry alive; None without the setting."""
    setting = os.environ.get(PROMPT_PREFIX_KEEPALIVE_ENV, "").strip()
    if not setting:
        return None
    try:
        seconds = float(setting)
    except ValueError as exc:
        raise ValueError(
            f"{PROMPT_PREFIX_KEEPALIVE_ENV}: {setting!r} is not a number of seconds",
        ) from exc
    if not (math.isfinite(seconds) and 0 < seconds < CACHE_LIFETIME_SECONDS):
        raise ValueError(
            f"{PROMPT_PREFIX_KEEPALIVE_ENV}: {setting} seconds does not fit inside the "
            f"{CACHE_LIFETIME_SECONDS:g} seconds a cache entry lives",
        )
    if not prompt_prefix():
        raise ValueError(
            f"{PROMPT_PREFIX_KEEPALIVE_ENV} is set, but {PROMPT_PREFIX_ENV} names no "
            "prefix to keep",
        )
    if call_log_path() is None:
        raise ValueError(
            f"{PROMPT_PREFIX_KEEPALIVE_ENV} is set, but {CALL_LOG_ENV} names no call log "
            "to read the room from",
        )
    return seconds


def reset_prompt_prefix() -> None:
    """Forget the prefix and the keep-alive, so the next call reads the
    settings again (tests)."""
    prompt_prefix.cache_clear()
    prefix_keepalive_seconds.cache_clear()


__all__ = [
    "CACHE_LIFETIME_SECONDS",
    "PROMPT_PREFIX_ENV",
    "PROMPT_PREFIX_KEEPALIVE_ENV",
    "prefix_keepalive_seconds",
    "prompt_prefix",
    "reset_prompt_prefix",
]
