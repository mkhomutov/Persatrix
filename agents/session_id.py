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
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from typing import Final

SESSION_ID_ENV_VAR: Final[str] = "PERSATRIX_SESSION_ID"
LEGACY_SESSION_ID: Final[str] = "legacy"

#: gRPC metadata header the orchestrator emits to carry the per-request
#: session id (ISSUE-0081 PR 2).  Lower-case by HTTP/2 convention; the
#: server-side lift (:func:`agents.session_metadata._session_from_metadata`)
#: matches it case-insensitively so a proxy / test harness presenting mixed
#: case still binds.  This is the cross-language contract with
#: ``cmd/orchestrator`` — a rename here is a conscious break.
SESSION_METADATA_GRPC_KEY: Final[str] = "persatrix-session"

#: Key under which the resolved session id rides on
#: :attr:`AgentEvent.metadata` from the gRPC ingress to
#: :meth:`agents.persona_runtime._LLMPersonaAgent.on_event`, where it is
#: bound into a :func:`session_scope` for the lifetime of the handler.
#: Namespaced (not a bare ``session_id``) because it shares the generic
#: ``event.metadata`` dict with unrelated keys — notably ``chat_session_id``,
#: a *different* concept (the CLI chat session, not the RFC 0031 operator
#: namespace) — so an un-prefixed name would invite collision/confusion.
#: Also intentionally distinct from the wire header
#: (:data:`SESSION_METADATA_GRPC_KEY`) so the in-process event envelope and
#: the HTTP/2 header can evolve independently.  In-process only — never
#: serialised back onto the wire.
EVENT_SESSION_METADATA_KEY: Final[str] = "persatrix_session"

#: Task-local active session id (ISSUE-0081 / RFC 0031 follow-up).
#:
#: RFC 0031 modelled "session = one process run": the env var is read
#: once at boot and cached at tier construction.  That is unsafe once one
#: persona process fields more than one conversation concurrently — the
#: shared ``(agent_id, session_id)`` namespace lets conversation A's
#: writes recall into conversation B's prompt.  A :class:`ContextVar` is
#: auto-copied into each :class:`asyncio.Task`, so a per-request
#: ``session_scope`` set on one task cannot bleed into a sibling task.
#:
#: ``None`` means "no per-request override is active" — callers fall back
#: to their construction-time env snapshot, which is why single-session
#: CLI / test / boot paths are unchanged when nothing sets the var.  The
#: gRPC correlation interceptor (RFC 0018 Phase 3 rail) binds this from
#: incoming request metadata in the follow-up PR.
_ACTIVE_SESSION_ID: ContextVar[str | None] = ContextVar(
    "persatrix_active_session_id", default=None,
)


def current_session_id() -> str | None:
    """Return the task-local active session id, or ``None`` when unset.

    Override-only reader: consults **only** the :data:`_ACTIVE_SESSION_ID`
    ContextVar, never the env var.  An unset scope is ``None`` even when
    :data:`SESSION_ID_ENV_VAR` is exported, so call-time recall / write
    seams can spell "context override, else my construction snapshot" as
    ``current_session_id() or self._active_session_id`` and preserve the
    snapshot semantics existing tests pin.
    """
    return _ACTIVE_SESSION_ID.get()


def resolve_session_id_silent() -> str:
    """Return the resolved active session id with no log output.

    Precedence: **task-local scope → env var → legacy carve-out**.  The
    :data:`_ACTIVE_SESSION_ID` ContextVar wins when a per-request
    ``session_scope`` is active; otherwise the env var is read, and an
    empty / unset / whitespace-only env → :data:`LEGACY_SESSION_ID`.

    Non-canonical characters are returned verbatim — including values the
    persona-runtime wrapper will WARN about; the leaf does not pre-filter
    so the WARN message can still fire on the same value the facade ended
    up tagging.

    Tier constructors call this with no scope active, so their cached
    snapshot keeps resolving from the env var exactly as before.
    """
    return (
        current_session_id()
        or os.environ.get(SESSION_ID_ENV_VAR, "").strip()
        or LEGACY_SESSION_ID
    )


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


@contextmanager
def session_scope(session_id: str | None) -> Iterator[str]:
    """Bind ``session_id`` as the task-local active session for the block.

    Sets the :data:`_ACTIVE_SESSION_ID` ContextVar to the normalised
    value (blank / ``None`` → :data:`LEGACY_SESSION_ID`, via
    :func:`normalize_session_id`) for the duration of the ``with`` block
    and restores the previous value on exit — including on exception, via
    the saved :class:`contextvars.Token`.

    Normalising up front means a blank per-request session can never
    silently fall through to a tier's construction snapshot; it collapses
    to the ``legacy`` carve-out, matching the storage-boundary contract.

    Yields the resolved id so callers can log / assert what was bound::

        with session_scope(request_session) as sid:
            ...  # current_session_id() == sid inside the block

    Concurrency: because the ContextVar is copied per :class:`asyncio.Task`,
    two tasks each entering their own ``session_scope`` do not see each
    other's value — this is the isolation a process-global env var cannot
    provide.  Enter the scope **inside** the task coroutine (not in the
    parent before spawning) so each task mutates its own context copy.
    """
    resolved = normalize_session_id(session_id)
    token = _ACTIVE_SESSION_ID.set(resolved)
    try:
        yield resolved
    finally:
        _ACTIVE_SESSION_ID.reset(token)


def session_id_from_metadata(metadata: Mapping[str, object]) -> str | None:
    """Read the per-request session off event metadata, or ``None``.

    The ONE validation seam behind both consumers of
    :data:`EVENT_SESSION_METADATA_KEY`: the handler-side scope binder
    (:func:`session_scope_from_metadata`) and the executor-hop structural
    lift (``DispatchContext.for_event`` — ISSUE-0118).  A present,
    non-empty string is the session; anything else (missing key, blank,
    or a tick event with no session, non-string) reads as ``None`` so
    both consumers agree on what "no per-request session" looks like and
    cannot drift on the key name or the validation rule.
    """
    sid = metadata.get(EVENT_SESSION_METADATA_KEY)
    if isinstance(sid, str) and sid:
        return sid
    return None


def session_scope_from_metadata(
    metadata: Mapping[str, object],
) -> AbstractContextManager[str | None]:
    """Return the per-request :func:`session_scope` for an event's metadata.

    ISSUE-0081 PR 2: :meth:`agents.persona_runtime._LLMPersonaAgent.on_event`
    enters this around :func:`asyncio.wait_for`, whose child task copies the
    task-local scope so recall + write seams inside ``_on_event_inner`` resolve
    to *this* conversation even when a sibling runs concurrently in-process.

    Reads :data:`EVENT_SESSION_METADATA_KEY` via
    :func:`session_id_from_metadata`.  A present, non-empty string binds a
    scope; anything else (missing key, blank, or a tick event with no
    session) yields a :func:`~contextlib.nullcontext` so call-time
    resolution falls back to the construction snapshot — leaving the
    single-session / CLI / tick paths unchanged.
    """
    sid = session_id_from_metadata(metadata)
    if sid is not None:
        return session_scope(sid)
    return nullcontext()
