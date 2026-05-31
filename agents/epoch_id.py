"""ISSUE-0085 — ``PERSATRIX_EPOCH`` run/test-isolation leaf module.

The run-isolation analogue of the tenant-axis leaf
:mod:`agents.principal_id` and the room-continuity leaf
:mod:`agents.session_id`.  Born of the scope-axes reframing
(``docs/memory-scope-axes.md``): once ``session`` is redefined as
room-continuity (``(agent, channel)``, accumulating, with a ``legacy``
carve-out *for* continuity), it can no longer be the home of F-3
run/test isolation.  Isolation gets its own orthogonal axis — ``epoch``
— modelled structurally on :mod:`agents.principal_id`, not on the
session axis.

Like both sibling leaves this is a true leaf inside the :mod:`agents`
package: it imports only the Python standard library and exposes the
env-var name, the single-world default, the gRPC + in-process event
metadata keys, the task-local :class:`~contextvars.ContextVar`, and the
``epoch_scope`` context manager.  Keeping it dependency-free lets both
the memory facade (construction-time snapshot) and the persona-runtime
ingress import it without re-introducing the ``persona_runtime →
persona → base → memory.facade`` import cycle — the same rationale the
session and principal leaves were split out for.

Why a *separate* axis, and why it follows **principal** not **session**:

* **Session** answers "which room / conversation does this row belong
  to?" and its recall predicate always unions the ``legacy`` carve-out
  so pre-RFC rows stay visible from every session — the carve-out exists
  *for* continuity.
* **Principal** answers "which tenant owns this row?" and its recall
  predicate is **strict equality** — no carve-out, no ``"*"`` bypass.
* **Epoch** answers "which test run / logical branch wrote this row?"
  and needs the *same* strict-equality predicate: a fresh epoch must see
  **nothing**.  That is the whole point of run isolation, so this leaf
  deliberately has **no** ``LEGACY``-style "always visible" constant and
  **no** ``"*"`` "all epochs" sentinel (contrast
  :mod:`agents.memory._session_filter`).

:data:`DEFAULT_EPOCH_ID` is therefore *not* a carve-out: it is the epoch
every production / untagged deployment uses, and the ``DEFAULT 'live'``
value the (forthcoming) epoch migration backfills onto pre-existing
rows.  Production never changes it, so behaviour is unchanged; CI bumps
it per job, so a rerun reusing ``--user alice`` under a fresh epoch
inherits none of the prior run's trust, opinions, or episodes — the F-3
fix the reframing moves off the session axis.

The producer is the orchestrator: it resolves :data:`EPOCH_ID_ENV_VAR`
at boot (default :data:`DEFAULT_EPOCH_ID`) and emits it per request on
the :data:`EPOCH_METADATA_GRPC_KEY` rail.  Unlike the principal rail
(which stays silent until RFC 0039 supplies a verified principal), the
epoch rail has a live producer from day one — ``live`` in production,
a per-job id in CI.  The rail + ``ContextVar`` + filter ship as their
own enablers (mirroring the session / principal PR-1 split) ahead of the
migration + tier wiring + orchestrator emission that consume them.

*Silent by design* (mirrors the session / principal leaves): a future
contributor must NOT add ``import logging`` or any observability
dependency here.  :file:`tests/unit/python/test_epoch_id_leaf_module.py`
pins both the "no logging import" property and the resolution / scope
contract.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from typing import Final

#: Operator-facing env var carrying the single-world epoch for a process.
#: Production leaves it at :data:`DEFAULT_EPOCH_ID`; CI bumps it per job.
#: Parallel to :data:`agents.principal_id.PRINCIPAL_ID_ENV_VAR`.
EPOCH_ID_ENV_VAR: Final[str] = "PERSATRIX_EPOCH"

#: The epoch every production / untagged deployment uses, and the value
#: the epoch migration backfills onto pre-existing rows
#: (``epoch_id TEXT NOT NULL DEFAULT 'live'``).  NOT a cross-epoch
#: carve-out — see the module docstring.
DEFAULT_EPOCH_ID: Final[str] = "live"

#: gRPC metadata header the orchestrator emits to carry the per-request
#: epoch.  Lower-case by HTTP/2 convention; the server-side lift matches
#: it case-insensitively.  Cross-language contract with the orchestrator
#: — a rename here is a conscious break.  Sibling of
#: :data:`agents.principal_id.PRINCIPAL_METADATA_GRPC_KEY`.
EPOCH_METADATA_GRPC_KEY: Final[str] = "persatrix-epoch"

#: Key under which the resolved epoch rides on :attr:`AgentEvent.metadata`
#: from the gRPC ingress to
#: :meth:`agents.persona_runtime._LLMPersonaAgent.on_event`, where it is
#: bound into an :func:`epoch_scope` for the handler's lifetime.  Distinct
#: from the wire header so the in-process envelope and the HTTP/2 header
#: can evolve independently.  In-process only — never serialised back onto
#: the wire.  Sibling of
#: :data:`agents.principal_id.EVENT_PRINCIPAL_METADATA_KEY`.
EVENT_EPOCH_METADATA_KEY: Final[str] = "persatrix_epoch"

#: Task-local active epoch.  Auto-copied into each :class:`asyncio.Task`,
#: so a per-request ``epoch_scope`` set on one task cannot bleed into a
#: sibling task — the per-run isolation a process-global env var cannot
#: provide.  ``None`` means "no per-request override active": callers fall
#: back to their construction-time env snapshot, so single-world CLI /
#: test / boot paths are unchanged.
_ACTIVE_EPOCH_ID: ContextVar[str | None] = ContextVar(
    "persatrix_active_epoch_id", default=None,
)


def current_epoch_id() -> str | None:
    """Return the task-local active epoch, or ``None`` when unset.

    Override-only reader: consults **only** the :data:`_ACTIVE_EPOCH_ID`
    ContextVar, never the env var, so call-time recall / write seams can
    spell "context override, else my construction snapshot" as
    ``current_epoch_id() or self._active_epoch_id`` and preserve the
    snapshot semantics existing tests pin.
    """
    return _ACTIVE_EPOCH_ID.get()


def resolve_epoch_id_silent() -> str:
    """Return the resolved active epoch with no log output.

    Precedence: **task-local scope → env var → default epoch**.  An
    empty / unset / whitespace-only env → :data:`DEFAULT_EPOCH_ID`.  Tier
    constructors call this with no scope active, so their cached snapshot
    resolves from the env var.
    """
    return (
        current_epoch_id()
        or os.environ.get(EPOCH_ID_ENV_VAR, "").strip()
        or DEFAULT_EPOCH_ID
    )


def normalize_epoch_id(value: str | None) -> str:
    """Normalise a caller-supplied ``epoch_id`` at the storage boundary.

    Empty / whitespace-only / ``None`` → :data:`DEFAULT_EPOCH_ID`; any
    other value is returned stripped.  Mirrors
    :func:`agents.principal_id.normalize_principal_id` so a direct
    programmatic caller cannot persist a row tagged ``""`` that escapes
    the strict epoch-equality recall filter.
    """
    return (value or "").strip() or DEFAULT_EPOCH_ID


@contextmanager
def epoch_scope(epoch_id: str | None) -> Iterator[str]:
    """Bind ``epoch_id`` as the task-local active epoch for the block.

    Sets :data:`_ACTIVE_EPOCH_ID` to the normalised value (blank /
    ``None`` → :data:`DEFAULT_EPOCH_ID`) for the ``with`` block and
    restores the previous value on exit — including on exception, via the
    saved :class:`contextvars.Token`.  Yields the resolved id.

    Concurrency: because the ContextVar is copied per :class:`asyncio.Task`,
    two tasks each entering their own ``epoch_scope`` do not see each
    other's value.  Enter the scope **inside** the task coroutine so each
    task mutates its own context copy.
    """
    resolved = normalize_epoch_id(epoch_id)
    token = _ACTIVE_EPOCH_ID.set(resolved)
    try:
        yield resolved
    finally:
        _ACTIVE_EPOCH_ID.reset(token)


def epoch_scope_from_metadata(
    metadata: Mapping[str, object],
) -> AbstractContextManager[str | None]:
    """Return the per-request :func:`epoch_scope` for an event's metadata.

    Reads :data:`EVENT_EPOCH_METADATA_KEY` off ``metadata``.  A present,
    non-empty string binds a scope; anything else (missing key, blank,
    tick event, non-string) yields a :func:`~contextlib.nullcontext` so
    call-time resolution falls back to the construction snapshot — leaving
    single-world / CLI / tick paths unchanged.  Sibling of
    :func:`agents.principal_id.principal_scope_from_metadata`.
    """
    eid = metadata.get(EVENT_EPOCH_METADATA_KEY)
    if isinstance(eid, str) and eid:
        return epoch_scope(eid)
    return nullcontext()
