"""Pattern parity test (RFC 0009 PR 3).

The Go side (`internal/security/sanitize_patterns.go`) is the canonical
authority for the InputSanitizer pattern table. The Python side reads
its patterns from `agents/security_patterns.py`, which is generated from
the Go source by `cmd/genpatterns`.

This test asserts:
  1. The generated Python file carries the expected pattern families.
  2. Every pattern in the Python mirror compiles cleanly — guards
     against accidental Go-only escape syntax leaking through.
  3. Re-running `make generate-sanitizer-patterns` produces no diff
     against the checked-in file (freshness check).

The third leg is the actual parity gate: it fails the build if a
maintainer edits the Go pattern table without regenerating the Python
mirror, which would let the two sides drift silently.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from agents.security_patterns import COMPILED_PATTERNS, DEFAULT_PATTERNS

REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATED_FILE = REPO_ROOT / "agents" / "security_patterns.py"


class TestGeneratedShape:
    def test_default_patterns_nonempty(self) -> None:
        assert len(DEFAULT_PATTERNS) >= 3, (
            "Expected at least one pattern per family (instruction_override / "
            "role_injection / exfiltration); got %d" % len(DEFAULT_PATTERNS)
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
    """Re-running the generator must produce the checked-in file
    byte-for-byte. If this fails, run `make generate-sanitizer-patterns`
    and commit the result."""

    @pytest.mark.skipif(
        not (REPO_ROOT / "go.mod").exists(),
        reason="Go module unavailable in this environment; freshness checked in CI.",
    )
    def test_generator_output_matches_checked_in_file(self, tmp_path: Path) -> None:
        out_path = tmp_path / "security_patterns.py"
        try:
            result = subprocess.run(
                ["go", "run", "./cmd/genpatterns", "-out", str(out_path)],
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

        actual = GENERATED_FILE.read_bytes()
        regenerated = out_path.read_bytes()
        if actual != regenerated:
            pytest.fail(
                "agents/security_patterns.py is stale relative to "
                "internal/security/sanitize_patterns.go. Run:\n"
                "    make generate-sanitizer-patterns\n"
                "and commit the resulting diff."
            )
