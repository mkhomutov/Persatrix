"""File-walking utilities for check scripts.

Provides ``walk_files()`` helper that yields paths under a directory tree
while respecting glob-based exclusion patterns.

All utilities use only Python stdlib.  Minimum Python version: 3.11.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from fnmatch import fnmatch
from pathlib import Path

__all__ = [
    "DEFAULT_EXCLUDES",
    "walk_files",
]

DEFAULT_EXCLUDES = [
    "target/**",
    ".git/**",
    "bin/**",
    "node_modules/**",
    "__pycache__/**",
    "*.pyc",
]


def _walk_files_impl(
    directory: str | Path,
    extensions: list[str] | None,
    exclude_patterns: list[str],
) -> Iterator[Path]:
    """Yield files under *directory*, applying extension and exclude filters."""
    root = Path(directory)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        dirnames[:] = [
            d for d in dirnames
            if not any(
                fnmatch(
                    (rel_dir + "/" + d if rel_dir != "." else d) + "/dummy",
                    pat.replace("\\", "/"),
                )
                for pat in exclude_patterns
            )
        ]
        for fname in filenames:
            if extensions is not None:
                if not any(fname.endswith(ext) for ext in extensions):
                    continue

            full = Path(dirpath) / fname
            rel = full.relative_to(root).as_posix()

            skip = False
            for pat in exclude_patterns:
                norm_pat = pat.replace("\\", "/")
                if fnmatch(rel, norm_pat):
                    skip = True
                    break
            if not skip:
                yield full


def walk_files(
    directory: str | Path,
    extensions: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> Iterator[Path]:
    """Yield files under *directory*, optionally filtering by extension.

    Parameters:
        directory: Root directory to walk.
        extensions: Optional list of file extensions to include (e.g.
            ``[".md", ".toml"]``).  If *None*, all files are yielded.
        exclude_patterns: Optional list of glob patterns to skip.
    """
    return _walk_files_impl(
        directory,
        extensions=extensions,
        exclude_patterns=exclude_patterns or [],
    )
