"""Log record → wire conversion for the log shipper (RFC 0018 PR 5).

Split out of :mod:`agents.observability.log_shipper`, which holds the
stream and its counters, when ISSUE-0207's fix would have taken that file
past the 500-line size warning.  The seam was already named there: the
shipper accepts plain ``dict`` records (the ``event_dict`` the structlog
chain produces after ``_reorder_keys``) and this module maps them onto
``persatrix.v1.LogEntry`` proto messages.  Unknown keys go into the
``attributes`` ``Struct`` so the downstream JSON line layer still
surfaces them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from google.protobuf import struct_pb2, timestamp_pb2

from ..generated import log_service_pb2 as logpb

__all__ = ["record_to_proto"]

#: Reserved keys lifted directly onto the proto's typed fields rather
#: than packed into the attributes Struct.  Order is irrelevant — the
#: lookup is by name.
_RESERVED_KEYS: frozenset[str] = frozenset({
    "schema_version",
    "timestamp",
    "level",
    "service.kind",
    "service.instance",
    "service.role",
    "message",
    "execution_id",
    "step_id",
    "agent_id",
    "request_id",
    "trace_id",
    "span_id",
    "source",
})

#: structlog / stdlib-logging bookkeeping keys that should never reach
#: the orchestrator's attributes Struct.  ``_record`` is structlog's
#: stdlib LogRecord shim; ``_from_structlog`` flags records that
#: round-tripped through the stdlib bridge; ``logger``/``stack_info``/
#: ``exc_info`` are stdlib LogRecord internals.  Filtering by exact
#: name keeps the contract stable; the additional ``_``-prefix sweep
#: in ``record_to_proto`` covers ad-hoc bookkeeping keys added by
#: third-party processors.  (PR #173 review nice-to-have #1.)
_BOOKKEEPING_KEYS: frozenset[str] = frozenset({
    "_record",
    "_from_structlog",
    "logger",
    "stack_info",
    "exc_info",
})


# ─── Record → proto conversion ──────────────────────────────────────────────


def record_to_proto(record: dict[str, Any]) -> logpb.LogEntry:
    """Convert a structlog event dict to ``persatrix.v1.LogEntry``."""
    e = logpb.LogEntry(
        schema_version=str(record.get("schema_version", "")),
        level=str(record.get("level", "")),
        service_kind=str(record.get("service.kind", "")),
        service_instance=str(record.get("service.instance", "")),
        service_role=str(record.get("service.role", "")),
        message=str(record.get("message", "")),
        execution_id=str(record.get("execution_id", "")),
        step_id=str(record.get("step_id", "")),
        agent_id=str(record.get("agent_id", "")),
        request_id=str(record.get("request_id", "")),
        trace_id=str(record.get("trace_id", "")),
        span_id=str(record.get("span_id", "")),
    )
    ts_parse_error = False
    if (ts := record.get("timestamp")) is not None:
        proto_ts, ts_parse_error = _to_timestamp(ts)
        e.timestamp.CopyFrom(proto_ts)
    if (src := record.get("source")) is not None and isinstance(src, dict):
        e.source.CopyFrom(logpb.LogEntry.Source(
            file=str(src.get("file", "")),
            line=int(src.get("line", 0)),
            function=str(src.get("function", "")),
        ))
    extras = {
        k: v
        for k, v in record.items()
        if k not in _RESERVED_KEYS
        and k not in _BOOKKEEPING_KEYS
        and not k.startswith("_")
    }
    if ts_parse_error and "timestamp_parse_error" not in extras:
        # Issue #179 Should-Fix #2: surface malformed-timestamp inputs
        # rather than silently emitting an epoch-zero entry that would
        # sort to the front of every chronological merge in the
        # orchestrator's handleListLogs / SSE broadcast path.
        #
        # Guard: if a producer record already carries its own
        # `timestamp_parse_error` key (unlikely but possible for
        # pass-through / replay logs), preserve the user value rather
        # than silently overwriting it.  The shipper's flag is a
        # best-effort signal; an explicit producer value takes
        # precedence.  (PR #182 review Should-Fix #1.)
        extras["timestamp_parse_error"] = True
    if extras:
        e.attributes.CopyFrom(_dict_to_struct(extras))
    return e


def _to_timestamp(ts: Any) -> tuple[timestamp_pb2.Timestamp, bool]:
    """Convert a record timestamp to a proto Timestamp.

    Returns ``(timestamp, parse_error)``.  ``parse_error`` is ``True``
    only for string inputs that failed RFC 3339 parsing; callers
    surface this as a ``timestamp_parse_error=true`` attribute so the
    downstream merge path can distinguish stamped-now entries from
    well-formed ones.  Issue #179 Should-Fix #2.
    """
    out = timestamp_pb2.Timestamp()
    if isinstance(ts, datetime):
        out.FromDatetime(ts)
        return out, False
    if isinstance(ts, (int, float)):
        # Preserve sub-second precision for float epoch inputs; the
        # previous FromSeconds(int(ts)) silently truncated milliseconds
        # so SSE / chronological merges across log sources lost
        # ordering information for entries within the same second.
        # (PR #173 review nice-to-have #5.)
        out.FromNanoseconds(int(ts * 1_000_000_000))
        return out, False
    if isinstance(ts, str):
        # Best-effort RFC 3339 parse.  On failure stamp the current
        # wall clock and flag the entry so it still sorts into the
        # vicinity of its siblings rather than collapsing the whole
        # merged view onto 1970-01-01.
        try:
            out.FromDatetime(datetime.fromisoformat(ts.replace("Z", "+00:00")))
            return out, False
        except ValueError:
            out.FromDatetime(datetime.now(UTC))
            return out, True
    # Unknown type: stamp now to keep the entry chronologically sane.
    out.FromDatetime(datetime.now(UTC))
    return out, True


def _dict_to_struct(d: dict[str, Any]) -> struct_pb2.Struct:
    s = struct_pb2.Struct()
    s.update({k: _coerce(v) for k, v in d.items()})
    return s


def _coerce(v: Any) -> Any:
    """Coerce arbitrary Python values into Struct-compatible types."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, dict):
        return {str(k): _coerce(val) for k, val in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_coerce(item) for item in v]
    return repr(v)
