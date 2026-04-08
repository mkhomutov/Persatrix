"""Shared utilities for cross-platform Python check scripts.

All utilities use only Python stdlib (pathlib, re, os, dataclasses, etc.).
Minimum Python version: 3.8.

Implementation is split across focused submodules:

- :mod:`~scripts.checks.walking`  — file-walking helpers
- :mod:`~scripts.checks.analysis` — source analysis (test-module
  detection, string/comment stripping, allow-comment suppression)
- :mod:`~scripts.checks.patterns` — data classes, pattern checking,
  reporting, and encoding helpers

This ``__init__.py`` re-exports the full public API so that existing
``from scripts.checks import …`` statements keep working unchanged.
"""

from __future__ import annotations

# --- walking ---------------------------------------------------------------
from scripts.checks.walking import (  # noqa: F401
    DEFAULT_EXCLUDES,
    walk_files,
)

# --- analysis --------------------------------------------------------------
from scripts.checks.analysis import (  # noqa: F401
    has_allow_comment,
)

# --- patterns / reporting / encoding ---------------------------------------
from scripts.checks.patterns import (  # noqa: F401
    Pattern,
    Violation,
    _in_ranges,
    _reconfigure_stream,
    check_patterns,
    ensure_utf8_stdout,
    ensure_utf8_streams,
    report_violations,
)

__all__ = [
    "DEFAULT_EXCLUDES",
    "Pattern",
    "Violation",
    "check_patterns",
    "ensure_utf8_stdout",
    "ensure_utf8_streams",
    "has_allow_comment",
    "report_violations",
    "walk_files",
]
