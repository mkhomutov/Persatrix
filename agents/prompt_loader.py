"""Resolve task-agent instruction text from external prompt files.

Task agents may declare their system-prompt instructions inline (the
``instructions`` field) or by reference (the ``instructions_file`` field).
This module loads referenced files and substitutes the resolved text into
the agent config before the agent is constructed.

Security
--------
``instructions_file`` paths are resolved relative to the **repo root** and
must live under the ``prompts/`` subtree.  Anything outside that subtree
— including ``..`` traversals, absolute paths, and symlink targets — is
rejected.  This deny-by-default rule prevents an attacker who can edit
``config/agents.yaml`` from also reading arbitrary files at agent-load
time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Subtree under which prompt files may live.  Paths in ``instructions_file``
# resolve relative to ``<repo_root>`` and must stay inside this subtree.
_PROMPTS_SUBDIR = "prompts"


class PromptLoadError(Exception):
    """Raised when an ``instructions_file`` reference cannot be resolved."""


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
