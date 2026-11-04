"""The Python secret redactor matches Go's (RFC 0028 PR plan, PR 3a).

The decision log masks secrets before it writes a line. The patterns come
from Go's ``SecretRedactor``: ``cmd/genpatterns`` copies them into
``agents/observability/redact_patterns.py`` and runs Go's redactor over a set
of samples, writing what Go returned into ``_redact_parity_samples.py``. These
tests check that Python returns the same for each sample, so the two sides
are compared by what they mask, not only by their pattern strings. That the
generated files are fresh is checked by ``test_pattern_parity.py`` and by
``make generate-sanitizer-patterns-check``, whose failing on a hand edit
``cmd/genpatterns/redact_test.go`` pins.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.observability.redact import Redactor, SecretRedactor
from agents.observability.redact_patterns import REDACT_PATTERNS

from ._redact_parity_samples import SAMPLES


@pytest.mark.parametrize(("pattern", "text", "go_output"), SAMPLES,
                         ids=[f"{p or 'clean'}-{i}" for i, (p, _, _) in enumerate(SAMPLES)])
def test_each_sample_masks_as_go_does(pattern: str, text: str, go_output: str) -> None:
    assert SecretRedactor().redact_text(text) == go_output


def test_the_samples_cover_every_pattern() -> None:
    assert {p for p, _, _ in SAMPLES} >= {name for name, _ in REDACT_PATTERNS}


def test_it_implements_the_redactor_protocol() -> None:
    assert isinstance(SecretRedactor(), Redactor)


def _secret_sample() -> tuple[str, str]:
    """One sample Go masks, and Go's output for it."""
    _, text, go_output = next(s for s in SAMPLES if s[0] == "openai-api-key")
    return text, go_output


def test_redact_walks_nested_values_and_keeps_keys() -> None:
    text, masked = _secret_sample()
    record: dict[str, Any] = {
        "top": text,
        "nested": {"inner": [text, ("x", text)], "n": 3, "flag": True, "none": None},
    }
    assert SecretRedactor().redact(record) == {
        "top": masked,
        "nested": {"inner": [masked, ("x", masked)], "n": 3, "flag": True, "none": None},
    }


def test_redact_leaves_its_input_unchanged() -> None:
    text, _ = _secret_sample()
    record = {"a": text, "b": {"c": [text]}}
    SecretRedactor().redact(record)
    assert record == {"a": text, "b": {"c": [text]}}


def test_redact_is_idempotent() -> None:
    redactor = SecretRedactor()
    record = {f"k{i}": text for i, (_, text, _) in enumerate(SAMPLES)}
    once = redactor.redact(record)
    assert redactor.redact(once) == once


def test_a_value_past_the_depth_cap_is_replaced_not_kept() -> None:
    text, _ = _secret_sample()
    deep: dict[str, Any] = {"leaf": text}
    for _ in range(40):
        deep = {"down": deep}
    flat = repr(SecretRedactor().redact(deep))
    assert text not in flat
    assert "[REDACTED:max-depth-exceeded]" in flat


def test_a_cycle_ends_at_the_depth_cap() -> None:
    record: dict[str, Any] = {"name": "x"}
    record["self"] = record
    assert "[REDACTED:max-depth-exceeded]" in repr(SecretRedactor().redact(record))


def test_a_cycle_through_two_keys_ends_at_the_depth_cap() -> None:
    # Each object is walked once per depth, so two ways back into the same
    # dict do not double the work at every level.
    text, masked = _secret_sample()
    record: dict[str, Any] = {"s": text}
    record["a"] = record
    record["b"] = record
    marker = "[REDACTED:max-depth-exceeded]"
    node: Any = SecretRedactor().redact(record)
    steps = 0
    while isinstance(node, dict):
        # The dict at depth 32 holds values past the cap, its "s" included.
        assert node["s"] == (masked if steps < 32 else marker)
        node, steps = node["b"], steps + 1
    assert (node, steps) == (marker, 33)


def test_a_value_shared_down_many_paths_is_masked_on_each() -> None:
    text, masked = _secret_sample()
    shared: Any = {"leaf": text}
    for _ in range(28):
        shared = [shared, shared]
    out = SecretRedactor().redact({"root": shared})
    left = right = out["root"]
    for _ in range(28):
        left, right = left[0], right[1]
    assert left == right == {"leaf": masked}
