"""RFC 0031 Phase 1 — ``PERSATRIX_SESSION_ID`` env-var resolution.

Extracted from :mod:`agents.persona_runtime` so the package's ``__init__``
stays under the 500-line repo-wide soft cap enforced by
``scripts/checks/file_size.py --strict``.  Pure module: no side effects
at import time; the only public surface is :func:`resolve_session_id`.

Mirrors the orchestrator-side resolution at
``cmd/orchestrator/startup.go::resolveSessionID``.  Unset / blank env →
the synthetic ``"legacy"`` carve-out (RFC 0031 OQ #2).  Hard validation
of non-canonical values lives in Phase 3 CLI's ``persatrix session new``
— Phase 1 accepts the env value verbatim once it is non-empty.
"""

from __future__ import annotations

import logging
import os
from typing import Final

SESSION_ID_ENV_VAR: Final[str] = "PERSATRIX_SESSION_ID"
LEGACY_SESSION_ID: Final[str] = "legacy"


def resolve_session_id_and_log(logger: logging.Logger) -> str:
    """Resolve ``PERSATRIX_SESSION_ID`` and emit the unset-fallback INFO line.

    Empty or unset env → ``"legacy"`` carve-out, with one INFO log line
    that matches the Go side's ``resolveSessionID`` message.  Happy
    path is silent so persona-runtime boot stays uncluttered.
    """
    raw = os.environ.get(SESSION_ID_ENV_VAR, "").strip()
    if not raw:
        logger.info(
            "%s unset; defaulting to '%s' session "
            "(RFC 0031 Phase 1 carve-out)",
            SESSION_ID_ENV_VAR, LEGACY_SESSION_ID,
        )
        return LEGACY_SESSION_ID
    return raw
