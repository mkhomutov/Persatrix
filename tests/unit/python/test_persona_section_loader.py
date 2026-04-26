"""Tests for ``agents.prompt_loader.load_persona_section`` (RFC 0022).

Split out of ``test_prompt_loader.py`` to keep that module under the
500-line code file-size policy. The composer that consumes these
templates is exercised in ``test_persona_section_composer.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agents.prompt_loader import (
    PromptLoadError,
    _read_persona_section,
    load_persona_section,
)


@pytest.fixture()
def sections_repo_root(tmp_path: Path) -> Path:
    """Repo-root layout for ``load_persona_section`` tests.

    Creates ``<tmp_path>/prompts/runtime/persona/sections/`` ready for
    template writes.  Each test gets its own ``tmp_path``, so the
    lru_cache on ``_read_persona_section`` (keyed by resolved
    ``sections_root``) cannot leak between tests.
    """
    (tmp_path / "prompts" / "runtime" / "persona" / "sections").mkdir(parents=True)
    return tmp_path


def _write_section(repo_root: Path, name: str, body: str) -> None:
    (
        repo_root / "prompts" / "runtime" / "persona" / "sections" / f"{name}.md"
    ).write_text(body, encoding="utf-8")


class TestLoadPersonaSectionSuccess:
    def test_returns_file_contents(self, sections_repo_root: Path) -> None:
        _write_section(sections_repo_root, "identity", "You are {name}.")
        assert (
            load_persona_section("identity", repo_root=sections_repo_root)
            == "You are {name}."
        )

    def test_strips_single_trailing_newline(self, sections_repo_root: Path) -> None:
        # Editor convention adds a final newline.  Mirrors ``load_snippet``
        # so the runtime sees the same bytes the f-string composer
        # produced before the externalization.
        _write_section(sections_repo_root, "identity", "You are {name}.\n")
        assert (
            load_persona_section("identity", repo_root=sections_repo_root)
            == "You are {name}."
        )

    def test_does_not_strip_internal_newlines(
        self, sections_repo_root: Path
    ) -> None:
        _write_section(
            sections_repo_root, "identity", "You are {name}.\nRole: {role}\n"
        )
        assert (
            load_persona_section("identity", repo_root=sections_repo_root)
            == "You are {name}.\nRole: {role}"
        )

    def test_caches_repeated_reads(self, sections_repo_root: Path) -> None:
        # Pin the cache contract: a second read after deleting the file
        # still succeeds because the value is cached.  Hot-path
        # optimisation since every persona system-prompt assembly hits
        # the loader six times.
        _write_section(sections_repo_root, "identity", "hello")
        first = load_persona_section("identity", repo_root=sections_repo_root)
        (
            sections_repo_root
            / "prompts"
            / "runtime"
            / "persona"
            / "sections"
            / "identity.md"
        ).unlink()
        second = load_persona_section("identity", repo_root=sections_repo_root)
        assert first == second == "hello"


class TestLoadPersonaSectionErrors:
    def test_missing_section_raises_with_clear_error(
        self, sections_repo_root: Path
    ) -> None:
        with pytest.raises(PromptLoadError, match="not found"):
            load_persona_section("missing", repo_root=sections_repo_root)

    def test_empty_name_rejected(self, sections_repo_root: Path) -> None:
        with pytest.raises(PromptLoadError, match="basename"):
            load_persona_section("", repo_root=sections_repo_root)

    def test_forward_slash_rejected(self, sections_repo_root: Path) -> None:
        # Without this guard a caller could escape into another subtree
        # (e.g. ``../safety/user-message-delimiters``).
        with pytest.raises(PromptLoadError, match="basename"):
            load_persona_section(
                "../safety/user-message-delimiters", repo_root=sections_repo_root
            )

    def test_backslash_rejected(self, sections_repo_root: Path) -> None:
        with pytest.raises(PromptLoadError, match="basename"):
            load_persona_section("..\\sneaky", repo_root=sections_repo_root)

    def test_leading_dot_rejected(self, sections_repo_root: Path) -> None:
        with pytest.raises(PromptLoadError, match="basename"):
            load_persona_section(".hidden", repo_root=sections_repo_root)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="symlink creation requires admin/dev-mode on Windows",
    )
    def test_symlink_escape_rejected(
        self, sections_repo_root: Path, tmp_path: Path
    ) -> None:
        """A symlink inside sections/ that targets a file outside must reject.

        Mirrors the symlink-escape test for ``load_snippet``: pins the
        resolver to ``Path.resolve()`` so a regression to
        ``os.path.abspath`` (which does not collapse symlinks) is caught.
        """
        target = tmp_path / "outside.md"
        target.write_text("nope", encoding="utf-8")
        link = (
            sections_repo_root
            / "prompts"
            / "runtime"
            / "persona"
            / "sections"
            / "escape.md"
        )
        link.symlink_to(target)
        with pytest.raises(PromptLoadError, match="outside"):
            load_persona_section("escape", repo_root=sections_repo_root)


class TestShippedPersonaSectionsByteIdentity:
    """Regression guard: shipped section templates must match what the
    composer expects.

    RFC 0022 moved six section templates out of the f-string composer in
    ``agents/persona_runtime/prompt_assembly.py`` into
    ``prompts/runtime/persona/sections/``.  These assertions pin the
    template bodies so an accidental edit is caught by CI rather than
    by an LLM behavior shift.
    """

    PROD_REPO_ROOT = Path(__file__).resolve().parents[3]

    def test_identity(self) -> None:
        assert load_persona_section(
            "identity", repo_root=self.PROD_REPO_ROOT
        ) == "You are {name}.\n{title_line}Role: {role}"

    def test_background(self) -> None:
        assert load_persona_section(
            "background", repo_root=self.PROD_REPO_ROOT
        ) == "Background:\n{background}"

    def test_behavior(self) -> None:
        assert load_persona_section(
            "behavior", repo_root=self.PROD_REPO_ROOT
        ) == "Communication style:\n{behavior}"

    def test_quirks(self) -> None:
        assert load_persona_section(
            "quirks", repo_root=self.PROD_REPO_ROOT
        ) == "Quirks:\n{quirks}"

    def test_goals(self) -> None:
        assert load_persona_section(
            "goals", repo_root=self.PROD_REPO_ROOT
        ) == "Goals:\n{goals}"

    def test_current_state(self) -> None:
        assert load_persona_section(
            "current-state", repo_root=self.PROD_REPO_ROOT
        ) == "Current state:\n{state}"

    def test_default_repo_root_resolves_production_section(self) -> None:
        # The default anchor must locate the shipped section.  Mirror
        # the snippet test — clear the cache first so we exercise the
        # file read, not a cached result keyed by an explicit root.
        _read_persona_section.cache_clear()
        assert load_persona_section("identity").startswith("You are {name}.")
