---
id: ISSUE-0279
summary: "In the agent's default JSON log format a record logged with an exception carries no stack frames: a standard-library record shows the exception's class, its message and the text `<traceback object at …>`, and a structlog record shows only `\"exc_info\": true`. The copy shipped to the orchestrator drops the field. Only `PERSATRIX_LOG_FORMAT=pretty` prints where the exception was raised."
status: open
severity: low
area: agents/observability
created: 2026-11-16
refs:
  - agents/observability/logging.py
  - agents/observability/log_record_proto.py
  - docs/observability.md
  - docs/issues/ISSUE-0273-log-shipper-treats-a-missing-log-service-as-an-outage.md
  - https://github.com/mkhomutov/Persatrix/pull/1135
---

# ISSUE-0279: The agent's JSON log line carries no traceback

## Summary

An agent writes its log as JSON lines unless `PERSATRIX_LOG_FORMAT=pretty`
is set. A record logged with an exception (`logger.exception(…)`, or any
level with `exc_info=True`) is meant to say where the exception was
raised. In the JSON format it does not: no stack frame reaches the line.

## Context

Run with logging set up as an agent sets it up
(`configure_logging(service_kind="agent")`, as `agents/server_cli.py`
does), one exception logged four ways:

| Logged with | What the JSON line holds under `exc_info` |
|---|---|
| a standard-library logger, `logger.exception(…)` or `exc_info=True` | three strings: `<class 'ValueError'>`, `ValueError('the cause')`, `<traceback object at 0x…>` |
| a structlog logger, `.exception(…)` or `exc_info=True` | `true` |

Why: the processor chain in `agents/observability/logging.py`
(`_build_processors`) has no step that turns an exception into text.
The JSON renderer then meets the raw value and writes whatever its
fallback gives: the `repr` of each part for the standard-library tuple,
and the bare flag for structlog. The `pretty` renderer formats the
exception itself, which is why the traceback shows there.

The copy of the record that the log shipper sends to the orchestrator
has no `exc_info` at all (`agents/observability/log_record_proto.py`
leaves the field out), so `persatrix logs` and the log routes do not
show it either.

The agents' code logs with an exception in about ninety places.

## Impact

An operator reading an agent's default log sees that something failed
and, for a standard-library record, the exception's message. They do not
see the file, the line or the call path, which is what a traceback is
for. For a structlog record they do not see the exception at all. The
way round it is to run the agent again with
`PERSATRIX_LOG_FORMAT=pretty` and wait for the fault to come back.

Nothing breaks: no record is lost and no agent stops.

## Proposed fix / investigation path

Add a step to the shared chain that renders the exception as text, or as
a structured list of frames, before the JSON renderer: structlog ships
both (`format_exc_info`, `dict_tracebacks`). Decide in the same change:

- whether the shipped copy carries it, and under which key: a traceback
  makes such a record many times larger, in the stream and in the
  orchestrator's log buffer;
- whether the redactor runs over the text, since a frame can quote a
  line of source and an exception message can quote a value;
- whether the log shipper's own repeated warning
  ([ISSUE-0273](ISSUE-0273-log-shipper-treats-a-missing-log-service-as-an-outage.md))
  should then log without an exception, or each of its lines grows by a
  traceback.

## Notes

> 2026-11-16 — found by the review of pull request 1135, while checking
> how large the log shipper's warning is. Older than that pull request,
> which does not change it. Severity proposed from the tracker's
> precedent
> ([ISSUE-0246](ISSUE-0246-the-log-shippers-counters-reach-no-metric.md),
> low: an observability gap with nothing lost); not ruled by the
> maintainer.

> 2026-11-28 — severity ruled `low`, as proposed: the maintainer's
> ruling at the
> [strategy review of 2026-11-28](../project-strategy-review-2026-11-28.md#3-the-maintainers-rulings),
> ratified by ruling (ab) of the
> [sequencing Amendment 2026-11-28](../v0.4.x-sequencing-2026-11-28.md#rulings).
