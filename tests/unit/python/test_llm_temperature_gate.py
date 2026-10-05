"""One rule for whether an adapter sends a temperature.

Each vendor has model families that take a temperature and newer ones that
reject it or ask for the default. The Anthropic, OpenAI and Gemini adapters
each name the families that take one and ask ``takes_temperature``, which
holds the rest once: how a family is read out of a model ID, and the single
log line an operator gets when the temperature an agent asked for is
dropped.

A model ID can name its family after a prefix: an OpenAI fine-tune is
``ft:gpt-4o-mini-…``, and the Gemini API and Vertex AI take a resource path
such as ``models/gemini-2.5-flash``. The family is read past both.
"""

from __future__ import annotations

import logging

import pytest

from agents.llm_temperature import takes_temperature

_LOG = logging.getLogger("tests.temperature_gate")


def _takes(model: str, families: tuple[str, ...], warned: set[str] | None = None) -> bool:
    return takes_temperature(
        model, families, asked=0.3, reason="this family takes none",
        warned=set() if warned is None else warned, log=_LOG,
    )


@pytest.mark.parametrize(
    ("model", "families"),
    [
        ("gpt-4o", ("gpt-3.5", "gpt-4")),
        ("ft:gpt-4o-mini-2024-07-18:acme::abc123", ("gpt-4",)),
        ("models/gemini-2.5-flash", ("gemini-1", "gemini-2")),
        ("publishers/google/models/gemini-2.0-flash", ("gemini-2",)),
        ("claude-sonnet-4-6", ("claude-sonnet-4",)),
    ],
)
def test_a_model_of_a_listed_family_takes_one(model: str, families: tuple[str, ...]) -> None:
    assert _takes(model, families) is True


@pytest.mark.parametrize(
    "model",
    ["gpt-6-sol", "ft:gpt-6-luna:acme::abc123", "models/gemini-3.8-flash", "my-gpt-4-clone"],
)
def test_a_model_of_no_listed_family_takes_none(model: str) -> None:
    assert _takes(model, ("gpt-4", "gemini-2")) is False


def test_no_listed_family_means_no_model_takes_one() -> None:
    assert _takes("gpt-4o", ()) is False


def test_a_dropped_temperature_is_logged_once_per_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    warned: set[str] = set()
    with caplog.at_level(logging.WARNING, logger=_LOG.name):
        _takes("gpt-6-sol", ("gpt-4",), warned)
        _takes("gpt-6-sol", ("gpt-4",), warned)
        _takes("gpt-6-luna", ("gpt-4",), warned)
        _takes("gpt-4o", ("gpt-4",), warned)
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2
    assert "'gpt-6-sol'" in messages[0]
    assert "'gpt-6-luna'" in messages[1]
    assert warned == {"gpt-6-sol", "gpt-6-luna"}


def test_the_log_line_says_what_was_asked_for_and_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOG.name):
        _takes("gpt-6-sol", ("gpt-4",))
    message = caplog.records[0].getMessage()
    assert "0.3" in message
    assert message.endswith("this family takes none")
