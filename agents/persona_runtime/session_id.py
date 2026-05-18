"""RFC 0031 Phase 1 — ``PERSATRIX_SESSION_ID`` log-emitting resolver.

Wraps the silent leaf reader at :mod:`agents.session_id` with the
INFO / WARN log lines that mirror the orchestrator-side
``cmd/orchestrator/startup.go::resolveSessionID``.  Two log paths:

* Empty / unset env → INFO that names the env var + the legacy
  carve-out.
* Non-canonical value (chars outside ``[A-Za-z0-9_-]``) → WARN that
  names the regex verbatim so operators grep for one phrase across
  both binaries (RFC 0031 PR plan PR 4 finding #3).

The silent read itself lives in :mod:`agents.session_id` so the
``MemoryStore`` construction-time read can call it without the
``persona_runtime → persona → base → memory.store`` import cycle.
PR #337 deep review finding M2.

The env-var name and legacy constant are re-exported from this module
so existing imports of :data:`SESSION_ID_ENV_VAR` /
:data:`LEGACY_SESSION_ID` from
``agents.persona_runtime.session_id`` keep working without churn (see
:file:`tests/unit/python/test_session_id_resolve.py`).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Final

from agents.session_id import (
    LEGACY_SESSION_ID as _LEGACY_SESSION_ID,
)
from agents.session_id import (
    SESSION_ID_ENV_VAR as _SESSION_ID_ENV_VAR,
)

# Re-exports for backward compatibility with callers that import these
# names from the persona-runtime wrapper (see module docstring).
SESSION_ID_ENV_VAR: Final[str] = _SESSION_ID_ENV_VAR
LEGACY_SESSION_ID: Final[str] = _LEGACY_SESSION_ID

# Same canonical shape the Go side enforces in ``cmd/orchestrator/startup.go``
# (``sessionIDPattern``).  Kept as a module-level singleton so the regex is
# compiled once per process; the helper is called at agent construction.
_CANONICAL_SESSION_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9_-]+$",
)


def resolve_session_id_and_log(logger: logging.Logger) -> str:
    """Resolve ``PERSATRIX_SESSION_ID`` and emit the parity log line.

    Empty or unset env → ``"legacy"`` carve-out with an INFO log that
    mirrors the Go side's ``resolveSessionID`` message.  Non-canonical
    values (characters outside ``[A-Za-z0-9_-]``) are accepted verbatim
    but emit a WARN that names the regex — operators grep for the same
    phrase across both binaries.  Well-formed non-empty values are
    silent so persona-runtime boot stays uncluttered.

    Reads the env directly (rather than delegating to
    :func:`agents.session_id.resolve_session_id_silent`) so the
    INFO-on-empty branch can fire BEFORE the leaf collapses the
    empty / whitespace cases into the legacy constant — the operator
    needs the INFO line that names :data:`SESSION_ID_ENV_VAR`.
    """
    raw = os.environ.get(SESSION_ID_ENV_VAR, "").strip()
    if not raw:
        logger.info(
            "%s unset; defaulting to '%s' session "
            "(RFC 0031 Phase 1 carve-out)",
            SESSION_ID_ENV_VAR, LEGACY_SESSION_ID,
        )
        return LEGACY_SESSION_ID
    if not _CANONICAL_SESSION_ID_PATTERN.match(raw):
        logger.warning(
            "%s contains characters outside [A-Za-z0-9_-]; "
            "accepting verbatim (hard validation in Phase 3 CLI)",
            SESSION_ID_ENV_VAR,
        )
    return raw
