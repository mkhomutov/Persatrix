"""Cross-language drift pin for the RFC 0052 synthesis-reserve unit.

Mirrors the established cross-language drift-test pattern
(``test_cross_language_max_cascade_depth_drift.py``, PR #319;
``test_cross_language_salience_max_channel_members_drift.py``, PR #573).

RFC 0052 PR 4a's roster-scaled synthesis reserve
(``internal/wallet/synthesis_reserve.go``) sizes ONE close-path LLM call
via ``DefaultSynthesisCallReserveTokens = 3500``. The Go doc-comment
states this value "tracks the RFC 0020 close summary's bounds" —
``SUMMARIZATION_TARGET_TOKENS`` (2000, input context) plus
``SUMMARIZATION_MAX_OUTPUT_TOKENS`` (1024, output) in
``agents/persona_runtime/summarize_close.py``, "with headroom for the
prompt/envelope overhead" (≈ 3024 → 3500). Until this file landed,
nothing pinned that relationship: a future change to either Python
constant would silently under-size the Go reserve, and the bounded
close (PR 4b) would then deny a close-path lease it was sized to admit.

The relationship pinned is an INEQUALITY, not equality (unlike the
cascade-depth / salience-cap pins) — the Go constant is deliberately
*derived with headroom*, not a mirrored literal. A future edit that
lets the two drift below the documented headroom (or below the Python
sum outright) should fail loudly here rather than surface as an
under-funded close discovered on a real soak.

The test imports the Python constants directly and parses
``internal/wallet/synthesis_reserve.go`` as text for the Go literal —
the same text-parse approach as the established precedents, so this
test needs no Go toolchain.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agents.persona_runtime.summarize_close import (
    SUMMARIZATION_MAX_OUTPUT_TOKENS,
    SUMMARIZATION_TARGET_TOKENS,
)

_SYNTHESIS_RESERVE_GO = Path("internal/wallet/synthesis_reserve.go")

# Captures `const DefaultSynthesisCallReserveTokens int64 = <int>` on a single
# line. Intentionally anchored to the current single-line, typed-const shape;
# a refactor that moves it into a `const ( ... )` block forces a deliberate
# update here, which is the point of a drift pin.
_GO_CONST_PATTERN = re.compile(
    r"^\s*const\s+DefaultSynthesisCallReserveTokens\s+int64\s*=\s*(\d+)\s*(?://.*)?$",
    re.MULTILINE,
)


def _go_default_synthesis_call_reserve_tokens() -> int:
    """Parse ``DefaultSynthesisCallReserveTokens`` out of the Go source.

    Raises ``pytest.fail`` (rather than returning ``None``) on a parse
    miss so a refactor that hides or renames the constant lands as an
    actionable test failure instead of a silent ``None``-vs-``int``
    ``AssertionError``.
    """
    src = _SYNTHESIS_RESERVE_GO.read_text(encoding="utf-8")
    match = _GO_CONST_PATTERN.search(src)
    if match is None:
        pytest.fail(
            f"could not find `const DefaultSynthesisCallReserveTokens int64 = "
            f"<int>` in {_SYNTHESIS_RESERVE_GO}. If the constant was moved "
            f"into a `const ( ... )` block, retyped, or renamed, update the "
            f"parse rule in this test to match the new shape — the "
            f"cross-language drift pin is part of the contract.",
        )
    return int(match.group(1))


def test_go_reserve_unit_covers_python_summary_bounds():
    """The Go per-call reserve MUST cover the Python summary's own bounds.

    A drift here is silent in the common case: PR 4a ships the reserve
    accounting DARK (nothing consults it yet), so a Python-side bump to
    either constant with no matching Go update passes every existing
    suite. It becomes load-bearing exactly when PR 4b wires the bounded
    close to this reserve — at which point an under-sized Go constant
    denies the close-path summary lease it was meant to fund, and the
    persona's summary falls through to the RFC 0020 janitor's
    "[interaction summary unavailable]" placeholder.
    """
    go_value = _go_default_synthesis_call_reserve_tokens()
    python_floor = SUMMARIZATION_TARGET_TOKENS + SUMMARIZATION_MAX_OUTPUT_TOKENS
    assert go_value >= python_floor, (
        f"synthesis-reserve unit no longer covers the RFC 0020 summary bounds: "
        f"Go (DefaultSynthesisCallReserveTokens in {_SYNTHESIS_RESERVE_GO}) = "
        f"{go_value}, Python (SUMMARIZATION_TARGET_TOKENS + "
        f"SUMMARIZATION_MAX_OUTPUT_TOKENS in "
        f"agents/persona_runtime/summarize_close.py) = {python_floor}. "
        f"Either the Python bounds grew past the Go headroom, or the Go "
        f"constant shrank — update DefaultSynthesisCallReserveTokens (and its "
        f"doc-comment, which restates the arithmetic) to restore headroom."
    )


def test_go_reserve_unit_matches_documented_value():
    """The Go constant MUST be the documented ``3500``.

    Independent of the inequality test above: pins the absolute value
    the Go doc-comment advertises ("≈ 3024 ... with headroom" → 3500),
    so a silent tune of the constant without updating its own
    doc-comment's arithmetic is caught here rather than only in a
    calibration soak.
    """
    assert _go_default_synthesis_call_reserve_tokens() == 3500
