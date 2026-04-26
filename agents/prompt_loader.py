"""Resolve task-agent instructions and safety snippets from prompt files.

Two related surfaces live here:

* ``resolve_instructions`` — task-agent ``instructions_file`` resolution.
  Loads markdown referenced from ``config/agents.yaml`` before agent
  construction.
* ``load_snippet`` — short safety/behavior fragments loaded by the
  runtime itself (persona system prompt, episodic summarizer system
  prompt, auto-reflection nudge).  Cached so hot paths read once.

Security
--------
Both functions enforce the same deny-by-default rule: candidate paths
must resolve under ``<repo_root>/prompts/``.  Anything outside —
including ``..`` traversals, absolute paths, and symlink targets — is
rejected.  ``load_snippet`` further restricts resolution to the
``prompts/runtime/safety/`` subtree and forbids path separators in the
snippet name so a caller cannot reach sibling subtrees by passing
``"../task-agents/planner"``.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

# Subtree under which prompt files may live.  Paths in ``instructions_file``
# resolve relative to ``<repo_root>`` and must stay inside this subtree.
_PROMPTS_SUBDIR = "prompts"

# Subtree confined to safety/behavior snippets.  ``load_snippet`` resolves
# names relative to ``<repo_root>/<_SAFETY_SUBDIR>``.
_SAFETY_SUBDIR = Path("prompts") / "runtime" / "safety"


class PromptLoadError(Exception):
    """Raised when a prompt-asset reference cannot be resolved."""


def resolve_instructions(
    agent_config: dict[str, Any],
    repo_root: Path,
) -> str | None:
    """Return the resolved instruction text for ``agent_config``.

    Resolution rules:

    * If both ``instructions`` and ``instructions_file`` are set,
      ``PromptLoadError`` is raised — the config is ambiguous.
    * If only ``instructions_file`` is set, the referenced file is read
      and its contents returned.
    * If only ``instructions`` is set, it is returned unchanged.
    * If neither is set, ``None`` is returned (callers decide whether
      that is a hard error for the agent's type).
    """
    inline = agent_config.get("instructions")
    file_ref = agent_config.get("instructions_file")
    agent_id = agent_config.get("id", "?")

    if inline is not None and file_ref is not None:
        raise PromptLoadError(
            f"Agent {agent_id!r}: 'instructions' and 'instructions_file' "
            f"are mutually exclusive — choose one"
        )

    if file_ref is None:
        return inline

    if not isinstance(file_ref, str) or not file_ref.strip():
        raise PromptLoadError(
            f"Agent {agent_id!r}: 'instructions_file' must be a non-empty string"
        )

    repo_root_resolved = repo_root.resolve()
    prompts_root = (repo_root_resolved / _PROMPTS_SUBDIR).resolve()

    candidate = (repo_root_resolved / file_ref).resolve()
    try:
        candidate.relative_to(prompts_root)
    except ValueError as exc:
        raise PromptLoadError(
            f"Agent {agent_id!r}: 'instructions_file' {file_ref!r} resolves "
            f"outside {prompts_root} (deny-by-default)"
        ) from exc

    if not candidate.is_file():
        raise PromptLoadError(
            f"Agent {agent_id!r}: 'instructions_file' {file_ref!r} not found "
            f"at {candidate}"
        )

    return candidate.read_text(encoding="utf-8")


# ─── Safety / behavior snippets ──────────────────────────────


def _default_repo_root() -> Path:
    """Anchor used to resolve safety-snippet paths in production.

    Mirrors ``server_persona.load_agent``'s default: the package's parent
    directory (``Path(__file__).parent.parent``), independent of where
    ``--config`` points.  Tests pass an explicit ``repo_root`` instead of
    monkeypatching this helper.
    """
    return Path(__file__).resolve().parent.parent


@functools.lru_cache(maxsize=64)
def _read_snippet(name: str, safety_root: Path) -> str:
    """Read and validate a snippet under ``safety_root``.

    Cached because these snippets are loaded on hot paths (every
    ``_build_system_prompt`` call, every auto-reflect tick, every
    episodic summarization).  Cache key includes ``safety_root`` so
    test fixtures with distinct ``tmp_path`` roots don't collide with
    each other or with the production root.
    """
    if not name or any(sep in name for sep in ("/", "\\")) or name.startswith("."):
        raise PromptLoadError(
            f"Snippet name {name!r} must be a simple basename "
            f"(no path separators, no leading dot)"
        )

    candidate = (safety_root / f"{name}.md").resolve()
    try:
        candidate.relative_to(safety_root)
    except ValueError as exc:
        raise PromptLoadError(
            f"Snippet {name!r} resolves outside {safety_root} "
            f"(deny-by-default)"
        ) from exc

    if not candidate.is_file():
        raise PromptLoadError(
            f"Snippet {name!r} not found at {candidate}"
        )

    text = candidate.read_text(encoding="utf-8")
    # Strip exactly one trailing newline.  Editors save markdown with a
    # final newline by convention, but the previously-inlined strings
    # had no trailing newline; absorbing the editor convention here keeps
    # the bytes the runtime sees byte-identical to the inlined source.
    if text.endswith("\n"):
        text = text[:-1]
    return text


def load_snippet(name: str, repo_root: Path | None = None) -> str:
    """Load a safety/behavior snippet from ``prompts/runtime/safety/<name>.md``.

    ``name`` is a basename without the ``.md`` suffix (e.g.
    ``"user-message-delimiters"``).  Path separators in ``name`` are
    rejected so a caller cannot escape the safety subtree by passing
    ``"../task-agents/planner"``.

    ``repo_root`` defaults to ``Path(__file__).parent.parent`` to match
    the production source-tree layout; tests pass a fixtured root.

    Returns the file contents with a single trailing newline stripped.
    Raises :class:`PromptLoadError` for missing files, malformed names,
    or paths that resolve outside the safety subtree.
    """
    root = Path(repo_root) if repo_root is not None else _default_repo_root()
    safety_root = (root / _SAFETY_SUBDIR).resolve()
    return _read_snippet(name, safety_root)
