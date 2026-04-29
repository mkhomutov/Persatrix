"""Unit tests for the log-safety helpers in ``agents.sub_agents._log_safety``.

Closes the round-3 L4 / round-4 L4 deferred coverage gap on
``_bounded`` and its constants.  Prior to this file the helper had
three behavioural surfaces (control-character strip, length cap,
non-string coercion) all exercised only indirectly through one
integration test (``test_failed_status_payload_is_truncated_…`` plus
its round-4 sibling ``…_control_chars_are_stripped``).  A regression
to any single surface — for example dropping the ``str()`` coercion
or flipping the ``<= cap`` boundary to ``<`` — would not surface in
the integration tests because they only assert the post-conditions
relevant to one specific raise site.

Tests here are deliberately surface-focused (input → returned string),
not site-focused.  PR 6b lifted the helper from
``agents.sub_agents.spawner`` to ``agents.sub_agents._log_safety`` —
the imports below were updated accordingly (round-3 Info closed).
"""

from __future__ import annotations

import pytest

from agents.sub_agents._log_safety import (
    _CTRL_REPLACEMENT,
    _DELEGATION_FAILURE_MESSAGE_CAP,
    _bounded,
)


# ─── Length cap (the volume defence) ──────────────────────────────


def test_bounded_returns_short_input_unchanged() -> None:
    """No truncation marker on inputs at or under the cap."""
    assert _bounded("abc") == "abc"


def test_bounded_empty_string_is_passthrough() -> None:
    """Edge: empty input must not crash and must not gain a marker."""
    assert _bounded("") == ""


def test_bounded_cap_boundary_is_inclusive() -> None:
    """Pin the documented contract: ``len(s) <= cap`` returns *s* as-is.

    A regression flipping the comparison to ``<`` would silently start
    appending the truncation marker to exact-cap-length strings, which
    operators would (correctly) read as a payload that was longer than
    it actually was.
    """
    cap = _DELEGATION_FAILURE_MESSAGE_CAP
    payload = "a" * cap
    out = _bounded(payload)
    assert out == payload
    assert "… (truncated)" not in out


def test_bounded_cap_plus_one_triggers_marker() -> None:
    """The first character past the cap must trigger the canonical
    ``"… (truncated)"`` marker and the returned prefix must be exactly
    ``cap`` characters long (not ``cap - 1`` or ``cap + 1``)."""
    cap = _DELEGATION_FAILURE_MESSAGE_CAP
    out = _bounded("a" * (cap + 1))
    assert out.endswith("… (truncated)")
    # Strip the marker; the remaining payload must equal the cap.
    assert len(out) - len("… (truncated)") == cap


def test_bounded_custom_cap_is_respected() -> None:
    """Helper must honour a caller-supplied cap (used for parameterised
    raise sites that prefer a tighter or looser bound than the default).
    """
    out = _bounded("abcdefghij", cap=4)
    assert out == "abcd… (truncated)"


# ─── Control-character strip (the injection defence) ──────────────


@pytest.mark.parametrize(
    "ctrl",
    [
        "\x00",  # NUL — log-pipeline truncation
        "\t",    # TAB — TSV column injection
        "\n",    # LF  — forged log line
        "\r",    # CR  — log-line overwrite (CRLF)
        "\x1b",  # ESC — ANSI sequence / terminal hijack
        "\x7f",  # DEL — non-printing
    ],
)
def test_bounded_strips_individual_control_chars(ctrl: str) -> None:
    """Every C0 control char (0x00-0x1F) plus DEL (0x7F) must be
    replaced with the U+2424 sentinel.  Parametrised so a regression
    points at the offending codepoint."""
    out = _bounded(f"a{ctrl}b")
    assert ctrl not in out
    assert _CTRL_REPLACEMENT in out
    # Surrounding text must survive (the strip is targeted, not a
    # blanket redaction).
    assert "a" in out and "b" in out


def test_bounded_strips_mixed_control_chars() -> None:
    """All control chars in a mixed-payload string get replaced
    individually — the strip is per-codepoint, not per-run."""
    out = _bounded("a\nb\x1bc\x00d")
    assert out == f"a{_CTRL_REPLACEMENT}b{_CTRL_REPLACEMENT}c{_CTRL_REPLACEMENT}d"


def test_bounded_preserves_non_control_unicode() -> None:
    """Non-control Unicode (emoji, accented letters, the U+2424 glyph
    itself) must pass through untouched.  This guards against an
    over-broad strip that accidentally includes printable codepoints."""
    payload = "café 🎉 done"
    assert _bounded(payload) == payload


# ─── Non-string coercion ───────────────────────────────────────────


def test_bounded_coerces_exception_via_str() -> None:
    """``_bounded(exc)`` is used at the ``_extract_result`` invalid-result
    raise site.  Must coerce via ``str()`` (giving the exception
    message) rather than ``repr()`` (which would leak the class name
    and quoting into the failure message)."""
    exc = RuntimeError("boom\nforged")
    out = _bounded(exc)
    # str(RuntimeError("boom\nforged")) == "boom\nforged"; strip then
    # neutralises the embedded newline.
    assert "boom" in out
    assert "\n" not in out
    assert _CTRL_REPLACEMENT in out


def test_bounded_coerces_non_string_value() -> None:
    """Defensive: any object with a sane ``__str__`` works.  Regression
    guard against a future change that narrows the input type to
    ``str`` only and silently mishandles non-string call sites."""
    assert _bounded(12345) == "12345"
