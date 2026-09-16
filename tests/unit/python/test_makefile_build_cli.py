"""Pin `make build-cli` creating its output directory (v0.3.16 release-prep P-1).

The recipe copies the release binary into ``$(GO_BIN)`` with ``cp … || true``
and then prints ``✓ CLI built → bin/persatrix``. On a fresh worktree ``bin/``
does not exist, so the copy fails, the ``|| true`` swallows it, and the
success line is printed with no binary on disk — the v0.3.16 live arc hit
exactly that. The fix is a ``mkdir -p`` ahead of the copy; this test pins it
against the recipe body, not the Makefile as a whole, so a later target
carrying the same line cannot keep it green.
"""

from __future__ import annotations

from _test_infra import makefile_recipe_body


def _recipe_lines() -> list[str]:
    body = makefile_recipe_body("build-cli")
    return [line.strip().lstrip("@").strip() for line in body.splitlines() if line.strip()]


def test_build_cli_creates_the_bin_directory_before_copying() -> None:
    lines = _recipe_lines()
    mkdir = [i for i, line in enumerate(lines) if line.startswith("mkdir -p $(GO_BIN)")]
    copy = [i for i, line in enumerate(lines) if line.startswith("cp ")]
    assert mkdir, "build-cli must `mkdir -p $(GO_BIN)`: `cp … || true` fails silently without it"
    assert copy, "build-cli no longer copies the binary into $(GO_BIN)"
    assert mkdir[0] < copy[0], "the mkdir must run before the copy it exists for"
