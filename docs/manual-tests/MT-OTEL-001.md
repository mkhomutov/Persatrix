# Manual Test MT-OTEL-001: OpenTelemetry traces + metrics + Collector tail-sampling

**Test ID**: `MT-OTEL-001`
**Feature Area**: Observability (traces + metrics)
**Version**: 1.0
**Created**: 2026-04-24
**Last Updated**: 2026-04-24
**Status**: Active

---

## Overview

**Purpose**: Verify the operator-visible v0.2.3 traces + metrics surface end-to-end:

1. A workflow submitted via `persatrix run` produces a single connected trace tree in Jaeger
   spanning orchestrator HTTP → workflow run → step dispatch → gRPC client/server → tick / event
   handler → memory query → LLM call.
2. Histogram metrics on the Prometheus side carry exemplars whose `trace_id` resolves to the same
   trace in Jaeger (click-through correlation).
3. The Collector tail-sampling pipeline retains the documented categories (errors, slow LLM calls,
   workflow-driven traces) and samples the residual 1 % of healthy autonomous tick traces.
4. The `debug` exporter is no longer wired into the steady-state pipelines (RFC 0019 PR 4 review
   Should-Fix #4 — Collector log stream is not flooded by routine traffic).

**Scope**: Jaeger trace lookup by `persatrix.workflow_id`; Prometheus exemplar click-through;
Collector tail-sampling spot-check; Collector exporter list verification.

**Out of Scope**: Full Loki LogQL correlation (covered by [MT-LOGS-001](MT-LOGS-001.md) §6 and the
RFC 0019 closeout follow-up issue's Loki query-path item); per-agent dashboard / alerting rules
(non-goal for v0.2.3 per RFC 0019 §Non-Goals).

---

## Related Documentation

- [docs/observability.md](../observability.md) §11 (Observability stack — Jaeger, Prometheus, Collector)
- [docs/rfcs/0019-opentelemetry-completion.md](../rfcs/0019-opentelemetry-completion.md) §H (Sampling, Back-Pressure, and the Collector Pipeline)
- [config/observability/otel-collector.yaml](../../config/observability/otel-collector.yaml)
- [docker-compose.yaml](../../docker-compose.yaml) (Jaeger, Prometheus, Loki, otel-collector services)

**Related Automated Tests**:
- `tests/integration/test_observability_e2e.py`
- `tests/integration/test_observability_schema_parity.py`
- `tests/integration/test_log_trace_correlation.py`
- `agents/tests/test_observability_tracing.py`
- `agents/tests/test_observability_metrics.py`
- `internal/observability/telemetry_test.go`, `metrics_test.go`

---

## Preconditions

### System Requirements

- Docker + Docker Compose (the test exercises the compose-managed observability stack).
- `ANTHROPIC_API_KEY` exported in the shell environment for the workflow-driven steps. Without it,
  the workflow reaches a terminal `failed` state quickly; trace lookups still work but do not
  exercise the LLM-call span path.
- Free local ports: `:16686` (Jaeger UI), `:9090` (Prometheus UI), `:8080` (orchestrator REST),
  `:4317` / `:4318` (OTLP intake).

### Test Data

- Any workflow that triggers at least one agent step. `feature-builder` from
  `workflows/feature-builder.yaml` is the canonical choice.

---

## Test Procedure

### Step 1: Bring up the stack

**Action**:

```pwsh
docker compose up -d
docker compose ps
```

**Expected**:
- All services listed: `persatrix-orchestrator`, `persatrix-agent-*`, `jaeger`, `prometheus`,
  `loki`, `otel-collector`.
- `prometheus` and `loki` report `(healthy)`. `otel-collector` reports `Up …` (no healthcheck — the
  upstream `otel/opentelemetry-collector-contrib` image is distroless and ships no probe binary;
  this is documented as a deferred item in the RFC 0019 closeout).

**Verification**:
- [ ] All compose services running
- [ ] Jaeger UI reachable at <http://localhost:16686>
- [ ] Prometheus UI reachable at <http://localhost:9090>

---

### Step 2: Drive a workflow and capture identifiers

**Action**:

```pwsh
$run = ./bin/persatrix run feature-builder --input '{"user_request":"Add a ping endpoint"}' --wait | ConvertFrom-Json
$run.execution_id
```

**Expected**:
- Exit code 0; the wrapper prints a JSON object containing `execution_id`. Terminal state may be
  `succeeded` or `failed` depending on API key availability — both produce trace data.

**Verification**:
- [ ] `execution_id` captured

---

### Step 3: Jaeger trace lookup by `persatrix.workflow_id`

**Action**:

1. Open <http://localhost:16686>.
2. Service: `persatrix-orchestrator`. Click **Find Traces**.
3. In **Tags** add `persatrix.workflow_id=feature-builder` and re-run the search.
4. Open the most recent trace. Inspect the span tree.

**Expected**:
- The trace contains spans from both the orchestrator (`workflow.submit`, `workflow.run`,
  `step.dispatch`, gRPC client) **and** at least one agent (`agent.persona.tick` /
  `agent.event.dispatch`, `agent.memory.episodic.recall` and/or `agent.llm.call`).
- The tree is connected (single root, no orphan service nodes) — confirming W3C TraceContext
  propagated across the gRPC boundary.
- The LLM-call span (when the API key was present) carries the OTEL Gen-AI semantic-convention
  attributes: `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
  `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons` (a list, even when length 1).

**Verification**:
- [ ] Single connected trace tree
- [ ] Both orchestrator and agent service nodes present
- [ ] `gen_ai.*` attributes present on the LLM-call span (when API key configured)

---

### Step 4: Prometheus exemplar click-through

**Action**:

1. Open <http://localhost:9090/graph>.
2. Query: `histogram_quantile(0.95, sum by (le) (rate(agent_llm_duration_bucket[5m])))`.
3. Switch the panel to **Table** view, click **Show exemplars**.
4. Click an exemplar dot; copy its `trace_id`.
5. In Jaeger, paste the `trace_id` into the **Lookup by Trace ID** field at the top.

**Expected**:
- The exemplar resolves to a Jaeger trace whose root span belongs to the same workflow run.
- The metric name reaches Prometheus in OTLP-translated form (underscores, no dots) — i.e.
  `agent_llm_duration_bucket`, not `agent.llm.duration_bucket`. This is the documented
  Collector → Prometheus exporter behaviour.
- If no exemplars are visible, confirm Prometheus was started with
  `--enable-feature=exemplar-storage` (the dev compose `prometheus` service `command:` block
  in [docker-compose.yaml](../../docker-compose.yaml) already enables it; forks pointing at
  their own Prometheus must enable the feature flag — the requirement is also restated next
  to the `prometheus` exporter in [config/observability/otel-collector.yaml](../../config/observability/otel-collector.yaml)).

**Verification**:
- [ ] At least one exemplar is visible on the histogram quantile query
- [ ] Pasting the exemplar `trace_id` into Jaeger opens the same workflow trace

---

### Step 5: Collector tail-sampling spot-check

**Action**:

1. Submit one workflow that fails (omit `ANTHROPIC_API_KEY` for the orchestrator process or pass
   an obviously invalid input):

   ```pwsh
   ./bin/persatrix run feature-builder --input '{}' --wait | Out-Null
   ```

2. Submit ~20 quick `feature-builder` runs back-to-back (or let an autonomous persona tick for a
   few minutes if one is configured). The healthy-tick policy samples 1 % of unmarked traces, so a
   handful of runs is enough to see the rule applied.
3. In Jaeger search by `service=persatrix-orchestrator` over the last 15 minutes.

**Expected**:
- The error trace from sub-step 1 is **always retained** (matches the `errors` policy on
  `status_code: ERROR`).
- Workflow-driven traces from sub-step 2 are **all retained** (the `workflow-traces` policy keeps
  every trace carrying `persatrix.workflow_id`). This is by design — operators want full visibility
  on user-driven runs.
- If autonomous tick traces are present, they are sampled at ~1 % (the `healthy-tick-sample`
  probabilistic policy). Ten ticks should produce roughly zero kept traces; a hundred should
  produce roughly one.

**Verification**:
- [ ] Error trace visible in Jaeger
- [ ] All workflow-tagged traces from sub-step 2 visible in Jaeger
- [ ] (If autonomous ticks were running) tick-trace count in Jaeger ≪ ticks emitted

---

### Step 6: Confirm `debug` exporter is no longer in steady-state pipelines

**Action**:

```pwsh
docker compose logs --tail=200 otel-collector | Select-String -Pattern "TracesExporter|MetricsExporter|LogsExporter|ResourceSpans #"
```

**Expected**:
- No per-trace / per-metric stdout dump from the Collector during normal operation. Routine
  pipeline activity is observable only at the receiver / batch processor / exporter telemetry
  level (info-level lifecycle log lines), not as serialised payloads.
- The Collector configuration retains the `debug` exporter definition with a comment explaining
  how to opt it back into a pipeline for incident triage. This keeps the operator escape-hatch
  available without flooding the steady-state log stream (RFC 0019 PR 4 review Should-Fix #4).

**Verification**:
- [ ] Collector log tail shows no payload dumps from the `debug` exporter
- [ ] [config/observability/otel-collector.yaml](../../config/observability/otel-collector.yaml) `service.pipelines.{traces,metrics,logs}.exporters` lists do not contain `debug`

---

## Pass / Fail Criteria

- **Pass**: Steps 1–6 each meet their Verification checkboxes.
- **Accepted-with-known-gap**:
  - Loki LogQL `{trace_id="<id>"}` correlation may return zero matches against an unconfigured
    Loki 3.x OTLP receiver (`trace_id` lands as a structured-metadata field rather than a stream
    label). The cross-signal correlation is otherwise validated by Jaeger ↔ Prometheus exemplars
    in Step 4. Tracked in the RFC 0019 closeout follow-up issue's "Loki query path / loki-config
    pin" item.
  - The `otel-collector` service has no Docker healthcheck (upstream distroless image ships no
    probe binary). A cold-start `docker compose up -d` may briefly race the OTLP receiver bind and
    drop the first batch (visible as a one-off non-zero `agent.observability.{spans,logs}.dropped`
    metric on first boot). Steady-state operation is unaffected. Tracked in the same follow-up
    issue.
- **Fail**: Any step's Verification fails, or the Jaeger trace tree is not connected end-to-end.

---

## Results

_Recorded when the test is next executed._

| Date | Operator | Result | Notes |
|------|----------|--------|-------|
| _pending first run_ | | | |
