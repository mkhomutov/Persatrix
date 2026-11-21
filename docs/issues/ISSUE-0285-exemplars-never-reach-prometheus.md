---
id: ISSUE-0285
summary: "No exemplar reaches Prometheus, so the documented click from a latency spike to its trace finds nothing. The agents attach exemplars and the Collector receives them, trace id included, but its Prometheus exporter writes none out, even with `enable_open_metrics: true` and a scrape that asks for OpenMetrics. Seen the same on Collector 0.103.1 with Prometheus v2.55 and on 0.165.0 with v3.16."
status: open
severity: low
area: observability
created: 2026-11-21
refs:
  - config/observability/otel-collector.yaml
  - config/observability/prometheus.yaml
  - docker-compose.yaml
  - agents/observability/metrics.py
  - docs/observability.md
  - docs/manual-tests/MT-OTEL-001.md
  - https://github.com/mkhomutov/Persatrix/pull/1122
---

# ISSUE-0285: No exemplar reaches Prometheus

## Summary

An exemplar is a sample trace id attached to a metric. It lets someone
looking at a slow-call spike in Prometheus jump straight to one of the slow
calls in Jaeger. The docs promise this jump, and step 4 of
[MT-OTEL-001](../manual-tests/MT-OTEL-001.md) tests it. It does not work:
Prometheus holds no exemplars at all.

The exemplars are lost inside the OpenTelemetry Collector. The agents
attach them and the Collector receives them, but its Prometheus exporter,
the endpoint Prometheus scrapes, writes none of them out.

## Context

Found on 2026-11-21 while checking the observability image bumps in pull
request 1122 on a live stack. Nothing in CI starts that stack. A small
probe sent one trace, the `agent.llm.duration` histogram (unit `ms`) and
two counters through the Collector with the agents' own exporter (the
Python SDK's OTLP/HTTP one, as in `agents/observability/metrics.py`). Each
measurement was recorded inside a span, so the SDK attached an exemplar to
it.

What was seen, hop by hop:

- **The SDK attaches them.** Read in-process, the histogram point carried
  one exemplar whose trace id matched the open span.
- **The Collector receives them.** A scratch Collector 0.165.0 with a
  `debug` exporter (`verbosity: detailed`) beside the Prometheus exporter
  printed `Exemplar #0`, `#1` and `#2` on `agent.llm.duration`, each with
  the probe's trace id and span id.
- **Its Prometheus exporter writes none.** Asked for OpenMetrics
  (`Accept: application/openmetrics-text; version=1.0.0`), the exporter on
  port 8889 answered in OpenMetrics with the histogram buckets and no
  exemplar annotation (`# {trace_id=...}`) on any line.
  `enable_open_metrics: true` was set.
- **So Prometheus has none.** With `--enable-feature=exemplar-storage` on,
  `/api/v1/query_exemplars` for `agent_llm_duration_milliseconds_bucket`
  returned an empty list. Prometheus v3.16 asks for OpenMetrics first when
  it scrapes.

The same probe on the images before that bump (Collector 0.103.1,
Prometheus v2.55.0) gave the same empty result. The bump neither caused
the loss nor fixed it. Everything else the probe checked arrived as the
docs say: the metric names, the tail-sampling decisions, and the log in
Loki with its `trace_id`.

## Impact

- The "one click from a p99 spike to the trace" path in
  [observability.md](../observability.md) (§ 10 and § 11.3) does not
  exist. Someone debugging a slow call has to search Jaeger by time and
  service instead.
- MT-OTEL-001 cannot pass step 4 on any image the compose files have
  pinned.
- Nothing else depends on exemplars. Metrics, traces and logs each arrive
  whole.

## Proposed fix / investigation path

1. Read the Collector's Prometheus exporter for the condition under which
   it writes an exemplar. The trace id, the span id and the value were all
   present on the received points, so the cause may be something the
   exporter expects that the SDK does not send.
2. If the exporter cannot carry them, let Prometheus take OTLP directly.
   Prometheus v3 has its own OTLP receiver
   (`--web.enable-otlp-receiver`), and the Collector could push metrics to
   it instead of being scraped. That is a larger change to the stack, and
   whether exemplars survive that path has not been checked either.
3. Whichever fix lands, add a check that the live stack's exemplars reach
   `/api/v1/query_exemplars`, since no test runs the stack today.

## Notes

> 2026-11-21 — found and narrowed to the Collector's Prometheus exporter
> while checking pull request 1122 on a live stack.
