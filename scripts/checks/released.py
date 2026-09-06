"""Which versions have shipped, and which version-cycle documents are frozen.

Shared by the size checker (frozen plans are exempt from the word cap) and the
plan-status checker (frozen plans are not judged for stale rows). "Shipped" is
read from the tree, not from git: ``CHANGELOG.md`` carries one dated
``## [X.Y.Z] - YYYY-MM-DD`` heading per release (written at release-prep PR 3,
one PR before the tag), so the answer is the same in a full clone, a depth-1
CI checkout, a worktree, and a tarball. ``git tag`` was the first design and
failed in CI: actions/checkout fetches a pull_request ref with ``--depth=1``
and no tags, and ignores the fetch-tags input in that mode (ISSUE-0139).
"""

from __future__ import annotations

import re
from pathlib import Path

#: The version-cycle documents that freeze at the post-release follow-up.
VERSION_DOC_RE = re.compile(
    r"^docs/v(\d+\.\d+(?:\.\d+)?)-"
    r"(?:plan|scope-locks|plan-amendment-[0-9-]+|release-prep-plan|release-baseline"
    r"|test-findings-pr-plan)\.md$"
)

_CHANGELOG_RELEASE_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}", re.M)


def released_versions(repo_root: Path) -> frozenset[str]:
    """Versions with a dated CHANGELOG section under *repo_root*.

    Empty when there is no changelog at the root — a scan of a sub-tree or a
    temp dir. Empty means nothing counts as released, so every version-cycle
    doc is treated as live: the conservative side.
    """
    try:
        text = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()
    return frozenset(_CHANGELOG_RELEASE_RE.findall(text))


def is_released_version_doc(rel: str, released: frozenset[str]) -> bool:
    """True when *rel* is a version-cycle doc whose version has shipped.

    A two-part version (``v0.2``) matches its ``.0`` release: the v0.2
    release-prep plan shipped as ``0.2.0``.
    """
    match = VERSION_DOC_RE.match(rel)
    if not match:
        return False
    version = match.group(1)
    if version in released:
        return True
    return version.count(".") == 1 and f"{version}.0" in released
