"""Tests for the task-agent prompt loader (``agents.prompt_loader``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.prompt_loader import PromptLoadError, resolve_instructions


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    """Create a fake repo root with an empty prompts/ subtree."""
    (tmp_path / "prompts" / "runtime" / "task-agents").mkdir(parents=True)
    return tmp_path


def _write_prompt(repo_root: Path, rel: str, body: str) -> None:
    p = repo_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


# ─── Inline / absent fields ──────────────────────────────────


class TestInlineAndAbsent:
    def test_returns_inline_when_only_inline_set(self, repo_root: Path) -> None:
        cfg = {"id": "x", "instructions": "be helpful"}
        assert resolve_instructions(cfg, repo_root) == "be helpful"

    def test_returns_none_when_neither_set(self, repo_root: Path) -> None:
        assert resolve_instructions({"id": "x"}, repo_root) is None

    def test_inline_and_file_set_raises(self, repo_root: Path) -> None:
        _write_prompt(repo_root, "prompts/runtime/task-agents/x.md", "from file")
        cfg = {
            "id": "x",
            "instructions": "inline",
            "instructions_file": "prompts/runtime/task-agents/x.md",
        }
        with pytest.raises(PromptLoadError, match="mutually exclusive"):
            resolve_instructions(cfg, repo_root)


# ─── File loading ────────────────────────────────────────────


class TestFileLoading:
    def test_loads_file_when_only_file_ref_set(self, repo_root: Path) -> None:
        _write_prompt(
            repo_root,
            "prompts/runtime/task-agents/planner.md",
            "You are a planner.\n",
        )
        cfg = {
            "id": "planner",
            "instructions_file": "prompts/runtime/task-agents/planner.md",
        }
        assert resolve_instructions(cfg, repo_root) == "You are a planner.\n"

    def test_missing_file_raises_with_clear_error(self, repo_root: Path) -> None:
        cfg = {
            "id": "planner",
            "instructions_file": "prompts/runtime/task-agents/missing.md",
        }
        with pytest.raises(PromptLoadError, match="not found"):
            resolve_instructions(cfg, repo_root)

    def test_empty_string_ref_raises(self, repo_root: Path) -> None:
        cfg = {"id": "x", "instructions_file": ""}
        with pytest.raises(PromptLoadError, match="non-empty string"):
            resolve_instructions(cfg, repo_root)

    def test_whitespace_ref_raises(self, repo_root: Path) -> None:
        cfg = {"id": "x", "instructions_file": "   "}
        with pytest.raises(PromptLoadError, match="non-empty string"):
            resolve_instructions(cfg, repo_root)

    def test_non_string_ref_raises(self, repo_root: Path) -> None:
        cfg = {"id": "x", "instructions_file": 42}
        with pytest.raises(PromptLoadError, match="non-empty string"):
            resolve_instructions(cfg, repo_root)


# ─── Path safety ─────────────────────────────────────────────


class TestPathSafety:
    def test_traversal_outside_prompts_rejected(self, repo_root: Path) -> None:
        # Place a file outside prompts/ and try to read it via ../
        (repo_root / "secret.md").write_text("nope", encoding="utf-8")
        cfg = {
            "id": "x",
            "instructions_file": "prompts/../secret.md",
        }
        with pytest.raises(PromptLoadError, match="outside"):
            resolve_instructions(cfg, repo_root)

    def test_absolute_path_rejected(self, repo_root: Path, tmp_path: Path) -> None:
        # An absolute path resolves outside <repo_root>/prompts.
        outside = tmp_path / "outside.md"
        outside.write_text("nope", encoding="utf-8")
        cfg = {"id": "x", "instructions_file": str(outside)}
        with pytest.raises(PromptLoadError, match="outside"):
            resolve_instructions(cfg, repo_root)

    def test_directory_ref_rejected(self, repo_root: Path) -> None:
        cfg = {"id": "x", "instructions_file": "prompts/runtime/task-agents"}
        with pytest.raises(PromptLoadError, match="not found"):
            resolve_instructions(cfg, repo_root)
