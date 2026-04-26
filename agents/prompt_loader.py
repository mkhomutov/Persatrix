"""Resolve task-agent instructions and safety snippets from prompt files.

Three related surfaces live here:

* ``resolve_instructions`` — task-agent ``instructions_file`` resolution.
  Loads markdown referenced from ``config/agents.yaml`` before agent
  construction.
* ``load_snippet`` — short safety/behavior fragments loaded by the
  runtime itself (persona system prompt, episodic summarizer system
  prompt, auto-reflection nudge).  Cached so hot paths read once.
* ``load_dimension_descriptions`` — structured persona behavioral-
  dimension descriptions loaded from
  ``prompts/runtime/persona/sections/behavior-dimensions.yaml``.
  Cached and shape-checked.

Security
--------
All three functions enforce the same deny-by-default rule: candidate
paths must resolve under ``<repo_root>/prompts/``.  Anything outside —
including ``..`` traversals, absolute paths, and symlink targets — is
rejected.  ``load_snippet`` further restricts resolution to the
``prompts/runtime/safety/`` subtree and forbids path separators in the
snippet name so a caller cannot reach sibling subtrees by passing
``"../task-agents/planner"``.  ``load_dimension_descriptions`` reads a
single fixed file inside ``prompts/runtime/persona/sections/`` and
takes no caller-controlled name, so the path-traversal surface is
empty by construction.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml

# Subtree under which prompt files may live.  Paths in ``instructions_file``
# resolve relative to ``<repo_root>`` and must stay inside this subtree.
_PROMPTS_SUBDIR = "prompts"

# Subtree confined to safety/behavior snippets.  ``load_snippet`` resolves
# names relative to ``<repo_root>/<_SAFETY_SUBDIR>``.
_SAFETY_SUBDIR = Path("prompts") / "runtime" / "safety"

# Fixed path to the behavioral-dimension descriptions YAML.  Hard-coded
# (not caller-supplied) because there is exactly one such file and the
# loader's structural shape check is tied to it.
_BEHAVIOR_DIMENSIONS_PATH = (
    Path("prompts") / "runtime" / "persona" / "sections" / "behavior-dimensions.yaml"
)


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


# ─── Persona behavioral-dimension descriptions ───────────────


@functools.lru_cache(maxsize=8)
def _read_dimension_descriptions(path: Path) -> dict[str, dict[str, str]]:
    """Read and shape-check the behavior-dimensions YAML at ``path``.

    Cached because ``render_behavior`` is called for every persona
    system-prompt assembly.  Cache key is the resolved path, so test
    fixtures with distinct ``tmp_path`` roots don't collide with
    each other or with the production root.

    The YAML must be an outer dict keyed by dimension, each value
    itself a dict keyed by value, with all leaves being strings.
    Any deviation raises :class:`PromptLoadError` so a malformed
    file fails loudly at import time rather than silently producing
    a degraded persona prompt.
    """
    if not path.is_file():
        raise PromptLoadError(
            f"Behavior-dimension descriptions not found at {path}"
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PromptLoadError(
            f"Behavior-dimension descriptions at {path} are not valid YAML: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise PromptLoadError(
            f"Behavior-dimension descriptions at {path} must be a mapping, "
            f"got {type(raw).__name__}"
        )

    for dimension, values in raw.items():
        if not isinstance(dimension, str):
            raise PromptLoadError(
                f"Behavior-dimension key {dimension!r} at {path} must be a "
                f"string, got {type(dimension).__name__}"
            )
        if not isinstance(values, dict):
            raise PromptLoadError(
                f"Behavior-dimension {dimension!r} at {path} must map to a "
                f"dict of value→description, got {type(values).__name__}"
            )
        for value, desc in values.items():
            if not isinstance(value, str):
                raise PromptLoadError(
                    f"Value key {value!r} under dimension {dimension!r} at "
                    f"{path} must be a string, got {type(value).__name__}"
                )
            if not isinstance(desc, str):
                raise PromptLoadError(
                    f"Description for {dimension!r}/{value!r} at {path} must "
                    f"be a string, got {type(desc).__name__}"
                )

    return raw


def load_dimension_descriptions(
    repo_root: Path | None = None,
) -> dict[str, dict[str, str]]:
    """Load persona behavioral-dimension descriptions.

    Reads
    ``<repo_root>/prompts/runtime/persona/sections/behavior-dimensions.yaml``
    and returns the parsed structure: an outer dict keyed by dimension
    name, mapping to an inner dict keyed by value, with string
    descriptions as leaves.

    The path is fixed (not caller-supplied) so there is no traversal
    surface — tests vary the layout via ``repo_root`` only.

    Raises :class:`PromptLoadError` if the file is missing, not valid
    YAML, or does not match the expected ``dict[str, dict[str, str]]``
    shape.
    """
    root = Path(repo_root) if repo_root is not None else _default_repo_root()
    path = (root / _BEHAVIOR_DIMENSIONS_PATH).resolve()
    return _read_dimension_descriptions(path)
