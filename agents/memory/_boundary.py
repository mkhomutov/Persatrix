"""Personal/society storage-boundary guard rail (RFC 0029 Phase 1).

The ``MemoryStore`` facade exists so personal-tier storage has exactly
one entry point.  RFC 0029 PR 2 makes that boundary enforceable with two
halves of one guard rail:

* a ruff ``TID251`` rule (configured in ``agents/pyproject.toml``) blocks
  a direct ``import aiosqlite`` in any file outside ``agents/memory/``;
* :func:`warn_external_construction` — used here — emits a
  ``DeprecationWarning`` when a per-tier memory class (``EpisodicMemory``
  / ``RelationshipMemory``) is constructed outside ``agents/memory/``.

Construction *inside* ``agents/memory/`` (the ``MemoryStore`` facade, the
shared-pool wrapper, the tier modules themselves) stays silent — the
facade is the supported builder.  The check is by file path, not module
name, so it holds whether the package is imported as ``agents.*`` (repo
root on ``sys.path``) or ``persatrix_agents.*`` (installed editable).
"""

from __future__ import annotations

import os
import sys
import warnings

#: Normalised absolute path of the ``agents/memory/`` package directory —
#: the one place direct per-tier construction (and direct ``aiosqlite``)
#: is allowed.  ``normcase`` folds Windows drive-letter / separator casing
#: so the ``startswith`` comparison below is host-correct.
_MEMORY_DIR: str = os.path.normcase(os.path.dirname(os.path.abspath(__file__)))


def is_construction_external(caller_file: str) -> bool:
    """Return ``True`` when *caller_file* lies outside ``agents/memory/``.

    Pure path classification — split out from
    :func:`warn_external_construction` so the boundary rule is
    unit-testable without a synthetic stack frame.  An empty / unknown
    filename counts as external (the safe default for a guard rail).
    """
    if not caller_file:
        return True
    resolved = os.path.normcase(os.path.abspath(caller_file))
    return not resolved.startswith(_MEMORY_DIR + os.sep)


def warn_external_construction(class_name: str) -> None:
    """Emit a ``DeprecationWarning`` if the caller is outside ``agents/memory/``.

    Call from a per-tier class ``__init__``.  Walks two frames up — past
    this function and the ``__init__`` — to the construction site, so the
    warning is attributed to the caller, not to this module.
    """
    try:
        caller = sys._getframe(2)
    except ValueError:
        # Stack too shallow to classify — stay silent rather than crash.
        return
    if is_construction_external(caller.f_code.co_filename):
        warnings.warn(
            f"Direct construction of {class_name} is deprecated and will be "
            f"removed in v0.4.0 — construct personal-tier memory through "
            f"agents.memory.MemoryStore instead (RFC 0029 personal/society "
            f"storage split).",
            DeprecationWarning,
            stacklevel=3,
        )
