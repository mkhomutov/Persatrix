"""Whether an adapter sends a model the temperature its caller asked for.

Each vendor has model families that take a temperature and newer ones that
reject it or ask for the default. The Anthropic, OpenAI and Gemini adapters
each name the families that take one; this module holds what they share: how
a family is read out of a model ID, and the one log line an operator gets
when a temperature is dropped.
"""

from __future__ import annotations

import logging

__all__ = ["takes_temperature"]


def takes_temperature(
    model: str,
    families: tuple[str, ...],
    *,
    asked: float,
    reason: str,
    warned: set[str],
    log: logging.Logger,
) -> bool:
    """Whether *model* belongs to one of the *families* that take a temperature.

    A family is read past a fine-tune or path prefix: ``ft:gpt-4o-mini-…`` is
    a ``gpt-4`` model and ``models/gemini-2.5-flash`` a ``gemini-2`` one. When
    the model takes none, the temperature the caller *asked* for is dropped,
    and that is logged once per model with *reason*: *warned* holds the models
    already logged, and *log* is the adapter's own logger.
    """
    if model.rsplit("/", 1)[-1].removeprefix("ft:").startswith(families):
        return True
    if model not in warned:
        warned.add(model)
        log.warning(
            "Sending %r no temperature (the caller asked for %s): %s", model, asked, reason,
        )
    return False
