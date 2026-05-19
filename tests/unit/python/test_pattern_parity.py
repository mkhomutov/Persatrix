"""Pattern parity test (RFC 0009 PR 3 + #254).

The Go side is the canonical authority for both the InputSanitizer
pattern table (`internal/security/sanitize_patterns.go`) and the
ContextSource + SanitizerAction closed-set enums
(`internal/security/context_source.go`, `internal/security/sanitize_action.go`).
The Python side reads its patterns from `agents/security_patterns.py`
and its enum constants from `agents/security_enums.py`; both are
generated from the Go source by `cmd/genpatterns`.

This test asserts:
  1. The generated Python pattern file carries the expected families.
  2. Every pattern in the Python mirror compiles cleanly — guards
     against accidental Go-only escape syntax leaking through.
  3. Re-running `make generate-sanitizer-patterns` produces no diff
     against the checked-in pattern OR enums file (freshness check).

The third leg is the actual parity gate: it fails the build if a
maintainer edits the Go pattern table or enum closed sets without
regenerating the Python mirror, which would let the two sides drift
silently.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from agents.security_patterns import COMPILED_PATTERNS, DEFAULT_PATTERNS

REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATED_PATTERNS_FILE = REPO_ROOT / "agents" / "security_patterns.py"
GENERATED_ENUMS_FILE = REPO_ROOT / "agents" / "security_enums.py"


class TestGeneratedShape:
    def test_default_patterns_nonempty(self) -> None:
        assert len(DEFAULT_PATTERNS) >= 3, (
            "Expected at least one pattern per family (instruction_override / "
            f"role_injection / exfiltration); got {len(DEFAULT_PATTERNS)}"
        )

    def test_every_family_present(self) -> None:
        names = {p.name for p in DEFAULT_PATTERNS}
        assert names >= {"instruction_override", "role_injection", "exfiltration"}

    def test_compiled_patterns_match_default_patterns(self) -> None:
        # COMPILED_PATTERNS is a tuple of (name, compiled regex) — every
        # row corresponds to one DEFAULT_PATTERNS entry, in the same order.
        assert len(COMPILED_PATTERNS) == len(DEFAULT_PATTERNS)
        for (name, compiled), p in zip(COMPILED_PATTERNS, DEFAULT_PATTERNS):
            assert name == p.name
            assert isinstance(compiled, re.Pattern)


class TestPythonMirrorCompiles:
    """Every Go regex string round-trips cleanly through Python's `re`.
    Catches accidental use of Go-only constructs (`\\A`, atomic groups)
    that would silently pass the Go-side test but fail the Python one."""

    def test_each_pattern_compiles(self) -> None:
        for p in DEFAULT_PATTERNS:
            try:
                re.compile(p.regex)
            except re.error as exc:
                pytest.fail(
                    f"Pattern {p.name!r} regex {p.regex!r} did not compile under "
                    f"Python `re`: {exc}"
                )


class TestParityFreshness:
    """Re-running the generator must produce the checked-in files
    byte-for-byte. If this fails, run `make generate-sanitizer-patterns`
    and commit the result."""

    @pytest.mark.skipif(
        not (REPO_ROOT / "go.mod").exists(),
        reason="Go module unavailable in this environment; freshness checked in CI.",
    )
    def test_generator_output_matches_checked_in_files(self, tmp_path: Path) -> None:
        patterns_out = tmp_path / "security_patterns.py"
        enums_out = tmp_path / "security_enums.py"
        try:
            result = subprocess.run(
                [
                    "go", "run", "./cmd/genpatterns",
                    "-out", str(patterns_out),
                    "-enums-out", str(enums_out),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                check=False,
                text=True,
            )
        except FileNotFoundError:
            pytest.skip("`go` toolchain not on PATH; freshness check deferred to CI.")
        if result.returncode != 0:
            pytest.fail(
                f"Generator failed (rc={result.returncode}). stderr:\n{result.stderr}"
            )

        # Two byte-level diffs, one per generated file. We assert each
        # separately so a failure message points at the specific stale file
        # rather than a combined "something drifted" report.
        if GENERATED_PATTERNS_FILE.read_bytes() != patterns_out.read_bytes():
            pytest.fail(
                "agents/security_patterns.py is stale relative to "
                "internal/security/sanitize_patterns.go. Run:\n"
                "    make generate-sanitizer-patterns\n"
                "and commit the resulting diff."
            )
        if GENERATED_ENUMS_FILE.read_bytes() != enums_out.read_bytes():
            pytest.fail(
                "agents/security_enums.py is stale relative to "
                "internal/security/context_source.go or sanitize_action.go. Run:\n"
                "    make generate-sanitizer-patterns\n"
                "and commit the resulting diff."
            )


class TestEnumsParity:
    """Spot-check the generated enum module loads with the Go-side closed
    set. The byte-level freshness gate above is the real parity check;
    these guard against the Python module being importable but empty,
    which the byte diff would catch but with a much less obvious error."""

    def test_known_context_sources_nonempty(self) -> None:
        from agents.security_enums import KNOWN_CONTEXT_SOURCES

        # Every closed-set member documented in RFC 0009 §C plus OQ #7's
        # `channel_message`. If the Go side adds a sixth, regenerate and
        # the byte diff above will already have failed.
        assert KNOWN_CONTEXT_SOURCES >= {
            "internal", "external", "agent_output", "user", "channel_message",
        }

    def test_known_sanitizer_actions_nonempty(self) -> None:
        from agents.security_enums import KNOWN_SANITIZER_ACTIONS

        assert KNOWN_SANITIZER_ACTIONS == {"passthrough", "quarantine"}
