"""RFC 0031 Phase 1 — ``PERSATRIX_SESSION_ID`` env-var resolution.

Extracted from :mod:`agents.persona_runtime` so the package's ``__init__``
stays under the 500-line repo-wide soft cap enforced by
``scripts/checks/file_size.py --strict``.  Pure module: no side effects
at import time; the only public surface is :func:`resolve_session_id`.

Mirrors the orchestrator-side resolution at
``cmd/orchestrator/startup.go::resolveSessionID``.  Unset / blank env →
the synthetic ``"legacy"`` carve-out (RFC 0031 OQ #2).  Hard validation
of non-canonical values lives in Phase 3 CLI's ``persatrix session new``
— Phase 1 accepts the env value verbatim once it is non-empty, but
emits a WARN log when the value falls outside ``[A-Za-z0-9_-]`` so an
operator running ``PERSATRIX_SESSION_ID="my session"`` sees the same
diagnostic line that the Go side already prints (RFC 0031 PR plan PR 4
finding #3 — Python/Go parity).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Final

SESSION_ID_ENV_VAR: Final[str] = "PERSATRIX_SESSION_ID"
LEGACY_SESSION_ID: Final[str] = "legacy"

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
