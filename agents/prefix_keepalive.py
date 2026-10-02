"""Keeping a cached prompt prefix alive through a quiet spell (EXP-001 arm D′).

The advisers of an arm D′ meeting share one cache entry: every turn offers
the same tools and then carries the same prefix, the transcripts of the
series' earlier meetings (:mod:`agents.prompt_prefix`). The first turn
writes the entry and the turns after it read it. The provider keeps an entry
five minutes after the last call that wrote or read it began, and a group
discussion can go quiet for longer by design: one that closes by its
600-second idle window has made no call for ten minutes, so the memo turn
after it would write the whole prefix again.

When ``PERSATRIX_PROMPT_PREFIX_KEEPALIVE`` names how long the room may go
without a call that carries the prefix, the process looks every
:data:`POLL_SECONDS`. The room is every process that writes to the same call
log, so the log says when the prefix was last used (:func:`agents.call_log.last_prefix_use`).
Once that is the setting's seconds ago, the process sends one call that
offers the turn's tools, carries the prefix and asks for no output: the
provider reads the entry, which keeps it for another five minutes, writes
nothing and answers nothing. The call carries none of the rest of a turn's
prompt, which comes after the prefix and is no part of the entry.

Nothing is sent before a call has carried the prefix, so the first turn
still writes the entry, and nothing once an entry's lifetime has passed,
since the entry is gone and the next turn writes it in any case. A call that
fails, or has not answered after :data:`TIMEOUT_SECONDS`, is tried again at
the next look while the entry may still live. Every call is logged, with
the purpose ``keepalive``. EXP-001 gives the setting to one adviser of a
meeting, its chair, so the room sends one keep-alive, not four.

The provider's cache and the call log both keep real time, so this module
reads the real clock, never the agent's, which a meeting sets to its story
date; that is why it sits beside the call log rather than in the persona
runtime, whose times are all agent time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .call_log import call_log_path, last_prefix_use
from .llm_types import LLMCallPurpose
from .persona_runtime import _LLMPersonaAgent
from .prompt_prefix import CACHE_LIFETIME_SECONDS, prefix_keepalive_seconds, prompt_prefix

logger = logging.getLogger(__name__)

__all__ = [
    "PLACEHOLDER",
    "POLL_SECONDS",
    "TIMEOUT_SECONDS",
    "keep_prefix_alive",
    "start_prefix_keepalive",
]

# How often the process looks at the log: a look this long after the quiet
# has run still leaves the call most of a minute before the entry goes.
POLL_SECONDS = 10.0
# A keep-alive that has not answered by then is cut off and tried again.
TIMEOUT_SECONDS = 30.0
# What the call asks. The provider reads it after the prefix, and the call
# asks for no output, so nothing ever answers it.
PLACEHOLDER = "Nothing to answer: this request only keeps the shared transcripts cached."


async def keep_prefix_alive(
    agent: Any,
    *,
    quiet: float,
    prefix: str,
    log: str,
    poll: float = POLL_SECONDS,
    timeout: float = TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Keep *prefix*'s cache entry alive for the room whose calls *log* holds,
    sending *agent*'s turn request once the room has gone *quiet* seconds
    without using it; runs until cancelled."""
    while True:
        await sleep(poll)
        last = last_prefix_use(log, prefix)
        if last is None or not quiet <= clock() - last < CACHE_LIFETIME_SECONDS:
            continue
        try:
            await asyncio.wait_for(
                agent._llm_client.create_message(
                    **agent._turn_request(),
                    purpose=LLMCallPurpose.KEEPALIVE,
                    cache_prefix=prefix,
                    messages=[{"role": "user", "content": PLACEHOLDER}],
                    system="",
                    max_tokens=0,
                ),
                timeout,
            )
        except Exception:
            logger.warning(
                "prompt prefix keep-alive for %s failed; trying again at the next look",
                agent.agent_id, exc_info=True,
            )


def start_prefix_keepalive(agents: Mapping[str, Any]) -> asyncio.Task[None] | None:
    """Keep the process's prefix alive when its setting asks, for its first
    persona agent; None when the setting is off or no agent is a persona."""
    quiet = prefix_keepalive_seconds()
    log = call_log_path()
    if quiet is None or log is None:
        return None
    persona = next((a for a in agents.values() if isinstance(a, _LLMPersonaAgent)), None)
    if persona is None:
        return None
    logger.info(
        "keeping the prompt prefix's cache entry alive for %s after %g s of quiet",
        persona.agent_id, quiet,
    )
    return asyncio.create_task(
        keep_prefix_alive(persona, quiet=quiet, prefix=prompt_prefix(), log=log),
        name=f"prompt-prefix-keepalive:{persona.agent_id}",
    )
