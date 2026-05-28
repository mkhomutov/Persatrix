"""RFC 0031 Phase 1 — ``PERSATRIX_SESSION_ID`` env-var leaf module.

A true leaf inside the :mod:`agents` package: imports only the Python
standard library and exposes the env-var name, the legacy carve-out
constant, and the silent reader.  Two call sites depend on it:

* :class:`agents.memory.facade.MemoryStore` — reads the env value at
  construction time so the task-agent / sub-agent write path inherits
  the operator-namespace tag without an explicit kwarg at every site.
* :mod:`agents.persona_runtime.session_id` — wraps the silent reader
  with the INFO / WARN log lines that mirror the orchestrator-side
  ``cmd/orchestrator/startup.go::resolveSessionID``.

Before this refactor, both call sites inlined the env-read sequence
because importing :mod:`agents.persona_runtime.session_id` from
:mod:`agents.memory.facade` re-introduces the
``persona_runtime → persona → base → memory.facade`` import cycle.
This leaf module breaks the cycle: it has zero internal dependencies
so any of the other modules can import it freely.

PR #337 deep review finding M2.

The leaf is *silent by design*: a future contributor must NOT add an
``import logging`` or any other observability dependency here.  The
facade's construction-time read must stay silent so the operator does
not see two INFO lines for the same env-resolution decision (one from
the facade per task agent, one from the persona-runtime boot path);
the canonical INFO / WARN parity with the Go side is the job of
:func:`agents.persona_runtime.session_id.resolve_session_id_and_log`.
:file:`tests/unit/python/test_session_id_leaf_module.py` pins both
the "no logging import" property and the facade/leaf agreement.
"""

from __future__ import annotations

import os
from typing import Final

SESSION_ID_ENV_VAR: Final[str] = "PERSATRIX_SESSION_ID"
LEGACY_SESSION_ID: Final[str] = "legacy"


def resolve_session_id_silent() -> str:
    """Return the resolved ``PERSATRIX_SESSION_ID`` with no log output.

    Empty / unset / whitespace-only env → :data:`LEGACY_SESSION_ID`.
    Any other value is returned verbatim — including non-canonical
    characters that the persona-runtime wrapper will WARN about; the
    leaf does not pre-filter so the WARN message can still fire on
    the same value the facade ended up tagging.
    """
    return os.environ.get(SESSION_ID_ENV_VAR, "").strip() or LEGACY_SESSION_ID


def normalize_session_id(value: str | None) -> str:
    """Normalise a caller-supplied ``session_id`` at the storage boundary.

    Empty / whitespace-only / ``None`` → :data:`LEGACY_SESSION_ID`.  Any
    other value is returned with surrounding whitespace stripped.

    Mirrors :func:`resolve_session_id_silent`'s contract for the
    env-var read so a direct programmatic caller (or test fixture)
    cannot persist a row tagged ``""`` that escapes both the real-
    session and the legacy-carve-out recall filters.  Applied at the
    four persona-memory tier write boundaries (``episodes`` /
    ``relationships`` / ``facts`` / ``notes``) so the invariant is
    uniform across tiers.

    RFC 0031 Phase 2 PR 4 (PR 1 review F16 carry-forward): factored out
    of the per-tier ``(session_id or "").strip() or LEGACY_SESSION_ID``
    pattern so adding a fifth tier in the future inherits the
    normalisation by calling one helper instead of re-implementing it.
    """
    return (value or "").strip() or LEGACY_SESSION_ID
