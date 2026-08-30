"""ISSUE-0081 PR 3 — ``PERSATRIX_PRINCIPAL_ID`` tenant-axis leaf module.

The tenant/principal analogue of the session-axis leaf
:mod:`agents.session_id` (RFC 0031 §C amendment).  Like that module this
is a true leaf inside the :mod:`agents` package: it imports only the
Python standard library and exposes the env-var name, the single-tenant
default, the gRPC + in-process event metadata keys, the task-local
:class:`~contextvars.ContextVar`, and the ``principal_scope`` context
manager.  Keeping it dependency-free lets both the memory facade
(construction-time snapshot) and the persona-runtime ingress import it
without re-introducing the ``persona_runtime → persona → base →
memory.facade`` import cycle — the same rationale the session leaf was
split out for (PR #337 finding M2).

Why a *separate* axis from :data:`~agents.session_id.LEGACY_SESSION_ID`
and friends:

* **Session** answers "which operator run / conversation wrote this
  row?" and its recall predicate always unions the ``legacy`` carve-out
  so pre-RFC rows stay visible from every session.
* **Principal** answers "which tenant / authenticated human owns this
  row?" and its recall predicate is **strict equality** — a row tagged
  with one principal is invisible to every other principal.  That is the
  whole point of the multi-tenant boundary, so this leaf deliberately
  has **no** ``LEGACY``-style "always visible" constant.

:data:`DEFAULT_PRINCIPAL_ID` is therefore *not* a carve-out: it is just
the principal every single-tenant / unauthenticated deployment uses, and
the ``DEFAULT 'local'`` value migration v11 backfills onto pre-existing
rows.  In a single-tenant deployment everything is the default principal,
so behaviour is unchanged; once a second tenant exists, default-principal
rows are visible only to the default principal — never bridged.

The verified per-request principal source is RFC 0039 (User Accounts &
Authentication, still *proposed*); until it lands the orchestrator emits
nothing on the :data:`PRINCIPAL_METADATA_GRPC_KEY` rail and every request
resolves to :data:`DEFAULT_PRINCIPAL_ID`.  The rail + ``ContextVar`` ship
now (mirroring the PR 1 / PR 2 session enabler split) so the storage
layer is ready the day auth supplies a real principal.

*Silent by design* (mirrors the session leaf): a future contributor must
NOT add ``import logging`` or any observability dependency here.
:file:`tests/unit/python/test_principal_id_leaf_module.py` pins both the
"no logging import" property and the resolution / scope contract.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from typing import Final

#: Operator-facing env var carrying the single-tenant principal for a
#: process.  Parallel to :data:`agents.session_id.SESSION_ID_ENV_VAR`.
PRINCIPAL_ID_ENV_VAR: Final[str] = "PERSATRIX_PRINCIPAL_ID"

#: The principal every single-tenant / unauthenticated deployment uses,
#: and the value migration v11 backfills onto pre-existing rows
#: (``principal_id TEXT NOT NULL DEFAULT 'local'``).  NOT a cross-tenant
#: carve-out — see the module docstring.
DEFAULT_PRINCIPAL_ID: Final[str] = "local"

#: gRPC metadata header the orchestrator emits to carry the per-request
#: principal (ISSUE-0081 PR 3).  Lower-case by HTTP/2 convention; the
#: server-side lift matches it case-insensitively.  Cross-language
#: contract with ``cmd/orchestrator`` — a rename here is a conscious
#: break.  Sibling of :data:`agents.session_id.SESSION_METADATA_GRPC_KEY`.
PRINCIPAL_METADATA_GRPC_KEY: Final[str] = "persatrix-principal"

#: Key under which the resolved principal rides on
#: :attr:`AgentEvent.metadata` from the gRPC ingress to
#: :meth:`agents.persona_runtime._LLMPersonaAgent.on_event`, where it is
#: bound into a :func:`principal_scope` for the handler's lifetime.
#: Distinct from the wire header so the in-process envelope and the
#: HTTP/2 header can evolve independently.  In-process only — never
#: serialised back onto the wire.  Sibling of
#: :data:`agents.session_id.EVENT_SESSION_METADATA_KEY`.
EVENT_PRINCIPAL_METADATA_KEY: Final[str] = "persatrix_principal"

#: Byte ceiling on a principal seeded from a REST payload
#: (:func:`seed_principal_metadata`).  Mirrors the sibling wire seeds'
#: bounds; generous against real subject ids, and far below anything that
#: makes an unbounded metadata value interesting.
_SEEDED_PRINCIPAL_MAX_BYTES: Final[int] = 256

#: Task-local active principal (ISSUE-0081 PR 3).  Auto-copied into each
#: :class:`asyncio.Task`, so a per-request ``principal_scope`` set on one
#: task cannot bleed into a sibling task — the per-tenant isolation a
#: process-global env var cannot provide.  ``None`` means "no per-request
#: override active": callers fall back to their construction-time env
#: snapshot, so single-tenant CLI / test / boot paths are unchanged.
_ACTIVE_PRINCIPAL_ID: ContextVar[str | None] = ContextVar(
    "persatrix_active_principal_id", default=None,
)


def current_principal_id() -> str | None:
    """Return the task-local active principal, or ``None`` when unset.

    Override-only reader: consults **only** the :data:`_ACTIVE_PRINCIPAL_ID`
    ContextVar, never the env var, so call-time recall / write seams can
    spell "context override, else my construction snapshot" as
    ``current_principal_id() or self._active_principal_id`` and preserve
    the snapshot semantics existing tests pin.
    """
    return _ACTIVE_PRINCIPAL_ID.get()


def resolve_principal_id_silent() -> str:
    """Return the resolved active principal with no log output.

    Precedence: **task-local scope → env var → default principal**.  An
    empty / unset / whitespace-only env → :data:`DEFAULT_PRINCIPAL_ID`.
    Tier constructors call this with no scope active, so their cached
    snapshot resolves from the env var.
    """
    return (
        current_principal_id()
        or os.environ.get(PRINCIPAL_ID_ENV_VAR, "").strip()
        or DEFAULT_PRINCIPAL_ID
    )


def normalize_principal_id(value: str | None) -> str:
    """Normalise a caller-supplied ``principal_id`` at the storage boundary.

    Empty / whitespace-only / ``None`` → :data:`DEFAULT_PRINCIPAL_ID`; any
    other value is returned stripped.  Mirrors
    :func:`agents.session_id.normalize_session_id` so a direct
    programmatic caller cannot persist a row tagged ``""`` that escapes
    the strict principal-equality recall filter.
    """
    return (value or "").strip() or DEFAULT_PRINCIPAL_ID


@contextmanager
def principal_scope(principal_id: str | None) -> Iterator[str]:
    """Bind ``principal_id`` as the task-local active principal for the block.

    Sets :data:`_ACTIVE_PRINCIPAL_ID` to the normalised value (blank /
    ``None`` → :data:`DEFAULT_PRINCIPAL_ID`) for the ``with`` block and
    restores the previous value on exit — including on exception, via the
    saved :class:`contextvars.Token`.  Yields the resolved id.

    Concurrency: because the ContextVar is copied per :class:`asyncio.Task`,
    two tasks each entering their own ``principal_scope`` do not see each
    other's value.  Enter the scope **inside** the task coroutine so each
    task mutates its own context copy.
    """
    resolved = normalize_principal_id(principal_id)
    token = _ACTIVE_PRINCIPAL_ID.set(resolved)
    try:
        yield resolved
    finally:
        _ACTIVE_PRINCIPAL_ID.reset(token)


def principal_id_from_metadata(metadata: Mapping[str, object]) -> str | None:
    """Read the per-request principal off event metadata, or ``None``.

    The ONE validation seam behind both consumers of
    :data:`EVENT_PRINCIPAL_METADATA_KEY`: the handler-side scope binder
    (:func:`principal_scope_from_metadata`) and the executor-hop
    structural lift (``DispatchContext.for_event`` — ISSUE-0118 / PR #809
    review finding 4).  A present, non-empty string is the principal;
    anything else (missing key, blank, tick event, non-string) reads as
    ``None`` so both consumers agree on what "no per-request principal"
    looks like and cannot drift on the key name or the validation rule.
    Sibling of :func:`agents.epoch_id.epoch_id_from_metadata` /
    :func:`agents.session_id.session_id_from_metadata`.
    """
    pid = metadata.get(EVENT_PRINCIPAL_METADATA_KEY)
    if isinstance(pid, str) and pid:
        return pid
    return None


def principal_scope_from_metadata(
    metadata: Mapping[str, object],
) -> AbstractContextManager[str | None]:
    """Return the per-request :func:`principal_scope` for an event's metadata.

    Reads :data:`EVENT_PRINCIPAL_METADATA_KEY` via
    :func:`principal_id_from_metadata`.  A present, non-empty string binds
    a scope; anything else (missing key, blank, tick event, non-string)
    yields a :func:`~contextlib.nullcontext` so call-time resolution falls
    back to the construction snapshot — leaving single-tenant / CLI / tick
    paths unchanged.  Sibling of
    :func:`agents.session_id.session_scope_from_metadata`.
    """
    pid = principal_id_from_metadata(metadata)
    if pid is not None:
        return principal_scope(pid)
    return nullcontext()


def seed_principal_metadata(
    metadata: dict[str, object], raw: object,
) -> bool:
    """Seed ``metadata`` with a principal carried on a REST payload.

    The REPLAY-side twin of the live ingress's
    ``event.metadata[EVENT_PRINCIPAL_METADATA_KEY] = request_principal``
    (``agents.server_servicers``), added for ISSUE-0130 shape (b): the
    on-startup catch-up replay is not a live dispatch, so its principal
    arrives as the ``principal_id`` field of a history row
    (channel-store v12) rather than as gRPC metadata.  Both producers
    write the SAME key, so the one binder
    (:func:`principal_scope_from_metadata`) attributes a replayed turn
    exactly as it attributes a live one.

    ``raw`` is accepted untyped because it comes off parsed JSON.  A
    string that is non-empty AFTER STRIPPING and within
    :data:`_SEEDED_PRINCIPAL_MAX_BYTES` seeds its stripped form and
    returns ``True``; anything else — a missing key, ``""``, whitespace,
    an over-long value, a non-string — seeds nothing and returns
    ``False``.

    Stripping is what makes the write end agree with the value that
    ultimately decides the tenant.  :func:`principal_id_from_metadata`
    tests bare truthiness, but the record key runs the bound principal
    through :func:`normalize_principal_id`, which strips and falls back
    to :data:`DEFAULT_PRINCIPAL_ID` — so seeding ``" "`` verbatim would
    mark the span ATTRIBUTED while landing its content in the shared
    ``local`` tenant, which is the leak the attribution exists to stop.
    Seeding the stripped form keeps the presence bit and the resolved
    value telling the same story (v0.3.15 PR B2 review).

    The byte bound is the same reason the sibling seeds on the adjacent
    lines carry one (:func:`agents.channel_event_classification
    .seed_channel_classification`, :func:`agents.channel_wire_metadata
    .seed_replay_metadata`): this value arrives on the REST history
    surface, which shares the cleartext gRPC port's TLS-deferred trust
    boundary, and it is written to a strict-equality tenant column.  An
    unbounded one is both a memory vector and a way to strand rows in a
    tenant no real caller can name.

    **The boolean is the consumer's real question**, not a convenience:
    absent means *this row predates the column* (a pre-v12 orchestrator),
    which is unattributable, while a present ``"local"`` is a real answer
    ("no verified tenant" — an unauthenticated publish, or the whole
    deployment under ``auth.mode: disabled``) and IS attributable.  Only
    the presence distinguishes them, which is why
    ``channelMessageResponse.PrincipalID`` is deliberately not
    ``omitempty`` on the Go side.  ``agents.persona_runtime.close_path``
    branches on exactly this to decide whether a replayed span may derive.

    This is a WRITE-side attribution only.  The seeded value is evidence
    of the same grade as the publish that produced it — the publish
    ingress is ``policyPublic``, so a spoofed ``sender_id`` inside a live
    dispatch's TTL can steer the ISSUE-0124 re-stamp — and it must never
    become a READ selector: the replay handler performs no recall (the
    ``replay_mode`` short-circuit returns before the gate and the LLM),
    so this binds nothing but where the derived rows land.
    """
    if not isinstance(raw, str):
        return False
    seeded = raw.strip()
    if not seeded or len(seeded.encode("utf-8")) > _SEEDED_PRINCIPAL_MAX_BYTES:
        return False
    metadata[EVENT_PRINCIPAL_METADATA_KEY] = seeded
    return True
