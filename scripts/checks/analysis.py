"""Source-code analysis utilities for check scripts.

Provides helpers for recognising allow-comment suppression markers.

All utilities use only Python stdlib.  Minimum Python version: 3.8.
"""

from __future__ import annotations

import re
from typing import List, Sequence, Tuple

__all__ = [
    "has_allow_comment",
]


def has_allow_comment(line: str, prev_line: str, marker: str) -> bool:
    """Return True if *line* or *prev_line* contains the suppression *marker*.

    This allows individual violations to be silenced with an inline or
    above-line comment containing the marker string.
    """
    return marker in line or marker in prev_line
