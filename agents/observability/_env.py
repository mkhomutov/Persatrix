"""Shared environment-variable parsing helpers for observability modules.

Both :mod:`agents.observability.metrics` and
:mod:`agents.observability.tracing` previously carried their own copies of
``_env`` / ``_int_env``.  Centralised here to prevent silent parser drift
(for example one module trimming whitespace and the other not).  Captured
in the PR #170 review (RFC 0019 PR 3).
"""

from __future__ import annotations

import os


def env_str(key: str, default: str) -> str:
    """Return ``os.environ[key]`` stripped, or *default* if empty/unset."""
    v = os.environ.get(key, "").strip()
    return v if v else default


def env_int(key: str, default: int) -> int:
    """Return ``os.environ[key]`` parsed as a positive int, or *default*.

    Empty / non-numeric / non-positive values fall back to *default*.
    Mirrors the original behaviour from
    :mod:`agents.observability.metrics`.
    """
    v = os.environ.get(key, "").strip()
    if not v:
        return default
    try:
        parsed = int(v)
        return parsed if parsed > 0 else default
    except ValueError:
        return default
