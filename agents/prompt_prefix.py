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
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

PROMPT_PREFIX_ENV = "PERSATRIX_PROMPT_PREFIX"


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


def reset_prompt_prefix() -> None:
    """Forget the prefix, so the next call reads the setting again (tests)."""
    prompt_prefix.cache_clear()


__all__ = ["PROMPT_PREFIX_ENV", "prompt_prefix", "reset_prompt_prefix"]
