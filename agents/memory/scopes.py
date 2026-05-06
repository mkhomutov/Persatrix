"""
Scope vocabulary helpers (RFC 0020 §G + §D scope-prefix table).

Extracted from :mod:`agents.memory.interactions` to keep that module
under the 500-line review cap. Scope strings carry the channel-type
prefix from RFC 0020 §D so the ``idx_episodes_scope`` index plays well
with ``LIKE 'thread:%'`` style scans. Helper builders keep the format
in one place; ad-hoc string concatenation at call sites is intentionally
avoided so the prefix vocabulary cannot drift from the storage-model
spec.

Public symbols are re-exported from :mod:`agents.memory.interactions`
for backward compatibility with the 19 existing import sites.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


SCOPE_TICK: str = "tick"

# Per RFC 0020 §G — scope strings carry the channel-type prefix from
# RFC 0020 §D. The helpers are idempotent: an input that already begins
# with the canonical prefix is returned unchanged so callers can pass
# either the bare key (``"planning"``) or the wire-side channel id
# (``"group:planning"``) without re-deriving the prefix at every site.
# PR 5 is the first call site to consume the wire-side form.
_GROUP_PREFIX: str = "group:"
_THREAD_PREFIX: str = "thread:"
_DM_PREFIX: str = "dm:"


def scope_for_dm(local_agent_id: str, peer_id: str) -> str:
    """DM scope: deterministic, symmetric in the two participants."""
    a, b = sorted((local_agent_id, peer_id))
    return f"{_DM_PREFIX}{a}:{b}"


def scope_for_thread(thread_id_or_scope: str) -> str:
    """Thread scope (idempotent in the ``thread:`` prefix)."""
    if thread_id_or_scope.startswith(_THREAD_PREFIX):
        return thread_id_or_scope
    return f"{_THREAD_PREFIX}{thread_id_or_scope}"


def scope_for_group(channel_id_or_name: str) -> str:
    """Group scope (idempotent in the ``group:`` prefix)."""
    if channel_id_or_name.startswith(_GROUP_PREFIX):
        return channel_id_or_name
    return f"{_GROUP_PREFIX}{channel_id_or_name}"


# ─── Channel-event scope routing (RFC 0020 PR 5) ─────────────


def scope_for_channel_event(
    local_agent_id: str,
    *,
    channel_id: str | None,
    sender_id: str | None,
    thread_id: str | None,
    channel_type: str | None,
    on_unknown: Callable[[str, str], None] | None = None,
) -> str | None:
    """Resolve InteractionTracker scope for a CHANNEL_MESSAGE event (RFC 0020 §G).

    Discriminator cascade:

    1. ``thread_id`` — wins outright (a threaded reply belongs to the
       thread, not the parent channel).
    2. ``channel_type`` — when set ("dm" / "group" / "thread") it is
       authoritative; the channel_id prefix is checked only as a
       consistency cross-check.
    3. ``channel_id`` prefix — used when ``channel_type`` is missing
       (legacy chat / pre-RFC-0020 callers) or is unrecognised.
    4. ``sender_id`` — final legacy-chat fallback when neither
       ``channel_id`` nor ``thread_id`` is present.

    Returns ``None`` for an under-populated event (no channel_id, no
    thread_id, no sender_id) or a DM event missing ``sender_id``.

    Contradiction handling (PR-262 review finding L1):

    * ``channel_type`` set to one type while the channel_id prefix
      indicates a different type — ``on_unknown(channel_type, channel_id)``
      fires and the helper trusts the explicit ``channel_type``. The
      contradiction is observable in the operator log path rather than
      being silently rewritten into a malformed scope key. Pre-fix the
      OR-pattern ``norm == X or channel_id.startswith(...)`` let the
      first matching branch produce silently-malformed scopes such as
      ``"group:thread:abc"``; the new behaviour keeps the routing
      deterministic but surfaces the wire-side validator drift.
    * ``channel_type`` set to an unrecognised value (e.g. ``"forum"``)
      while the prefix is known — ``on_unknown`` fires and the helper
      routes by prefix.
    * Both unknown — ``on_unknown`` fires (with the raw type and
      channel_id) and a thread-shape fallback is returned so the row
      lands somewhere deterministic.

    The ``on_unknown`` callback receives stringified arguments even when
    ``channel_type`` arrives as a non-str at runtime (payload corruption
    / wire-side validator drift). The annotation is enforced at the call
    site — the ``isinstance(raw, str)`` guard inside ``norm`` already
    acknowledged that ``channel_type`` may not be a ``str``; PR-262
    review finding L2 closed the gap of forwarding it un-coerced to the
    callback.
    """
    if thread_id:
        return scope_for_thread(thread_id)
    raw = channel_type if channel_type is not None else ""
    norm = raw.strip().lower() if isinstance(raw, str) else ""
    if channel_id:
        prefix_kind = (
            "dm" if channel_id.startswith(_DM_PREFIX)
            else "group" if channel_id.startswith(_GROUP_PREFIX)
            else "thread" if channel_id.startswith(_THREAD_PREFIX)
            else None
        )
        type_kind = norm if norm in {"dm", "group", "thread"} else None

        # Surface wire-side drift — explicit ``channel_type`` disagrees
        # with the channel_id prefix, or ``channel_type`` is set to an
        # unrecognised value. Both cases mean the upstream wire schema
        # and the local routing vocabulary are out of sync; the operator
        # path needs to see it.
        type_set = bool(norm)
        if (
            on_unknown is not None
            and type_set
            and (
                (type_kind is None)  # unknown type
                or (prefix_kind is not None and type_kind != prefix_kind)  # contradiction
            )
        ):
            on_unknown(str(raw), channel_id)

        # Route by explicit type when known; otherwise by prefix.
        if type_kind == "dm":
            return scope_for_dm(local_agent_id, sender_id) if sender_id else None
        if type_kind == "group":
            return scope_for_group(channel_id)
        if type_kind == "thread":
            return scope_for_thread(channel_id)
        if prefix_kind == "dm":
            return scope_for_dm(local_agent_id, sender_id) if sender_id else None
        if prefix_kind == "group":
            return scope_for_group(channel_id)
        if prefix_kind == "thread":
            return scope_for_thread(channel_id)
        # Both unknown. Fire ``on_unknown`` only if we did not already
        # fire above (when ``norm`` was set), to avoid double-emitting
        # for a single event.
        if on_unknown is not None and not type_set:
            on_unknown(str(raw), channel_id)
        return scope_for_thread(channel_id)
    if sender_id:
        return scope_for_dm(local_agent_id, sender_id)
    return None


__all__ = [
    "SCOPE_TICK",
    "scope_for_channel_event",
    "scope_for_dm",
    "scope_for_group",
    "scope_for_thread",
]
