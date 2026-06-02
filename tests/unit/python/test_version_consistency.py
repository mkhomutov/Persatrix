"""Guards the hand-maintained version constants tracked by
``scripts/bump_version.py``'s ``VERSION_FILES`` against silent drift.

``bump_version.py`` is the *sync* mechanism — running it rewrites every tracked
version string in lockstep. But nothing *verifies* that the files actually
agree: a manual edit to a single constant (for example the Go
``defaultServiceVersion`` fallback the web console reports via
``/api/v1/ui/config``, RFC 0048) leaves the others stale. Because none of these
constants is a build input, ``make all`` stays green while the console, the
observability stack, and the published package each report a different version.

This test reads every entry in ``VERSION_FILES`` the same way ``bump()`` applies
it (identical regex, ``re.MULTILINE``, first match only), extracts the version
each file currently carries, and asserts they are all identical and valid
semver. It is self-maintaining: any future ``VERSION_FILES`` entry is covered
automatically, so the guard can never lag the sync logic it protects.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Repo root is four levels up from tests/unit/python/<this file>.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.bump_version import ROOT, SEMVER_RE, VERSION_FILES  # noqa: E402


def _extract_version(rel_path: str, pattern: str) -> str:
    """Pull the version a file currently carries for one ``VERSION_FILES`` entry.

    Mirrors how ``bump()`` applies each pattern (``re.MULTILINE``, first match).
    Every ``VERSION_FILES`` pattern has the shape ``(prefix)version(suffix)``
    with the version uncaptured between groups 1 and 2, so the version is the
    slice between the end of group 1 and the start of group 2.
    """
    text = (ROOT / rel_path).read_text(encoding="utf-8")
    match = re.search(pattern, text, flags=re.MULTILINE)
    assert match is not None, (
        f"{rel_path}: its VERSION_FILES pattern no longer matches — "
        "bump_version.py can no longer update this file, fix the pattern"
    )
    return text[match.end(1) : match.start(2)]


def test_all_tracked_version_strings_agree() -> None:
    """Every file tracked by VERSION_FILES must carry the same version string."""
    found: dict[str, str] = {}
    for rel_path, pattern, _template in VERSION_FILES:
        found[f"{rel_path} :: {pattern}"] = _extract_version(rel_path, pattern)

    distinct = set(found.values())
    assert len(distinct) == 1, (
        "version constants have drifted across VERSION_FILES — run "
        "`python scripts/bump_version.py <version>` to resync:\n"
        + "\n".join(f"  {entry} -> {version!r}" for entry, version in found.items())
    )


def test_tracked_version_is_valid_semver() -> None:
    """The shared version must be valid semver — the same check bump() enforces."""
    rel_path, pattern, _template = VERSION_FILES[0]
    version = _extract_version(rel_path, pattern)
    assert SEMVER_RE.match(version), f"{version!r} (from {rel_path}) is not valid semver"
