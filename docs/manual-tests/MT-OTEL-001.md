# Manual Test MT-OTEL-001: OpenTelemetry traces + metrics + Collector tail-sampling

**Test ID**: `MT-OTEL-001`
**Feature Area**: Observability (traces + metrics)
**Version**: 1.1
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
- Current images. Run `docker compose build` before the first `docker compose up -d`, and after any
  change under [agents/](../../agents/) or [cmd/orchestrator/](../../cmd/orchestrator/). Stale
  images silently skip the OTEL SDK init path — Steps 3 and 4 then fail because no agent spans
  reach Jaeger and no agent metrics reach Prometheus.
- `ANTHROPIC_API_KEY` exported in the shell environment for the workflow-driven steps. Without it,
  the workflow reaches a terminal `failed` state quickly; trace lookups still work but do not
  exercise the LLM-call span path.
- Free local ports: `:16686` (Jaeger UI), `:9091` (Prometheus UI), `:8080` (orchestrator REST),
  `:9090` (orchestrator gRPC), `:4317` / `:4318` (OTLP intake). Note: Prometheus is published on
  host `9091` (mapped to container `9090`) because the orchestrator already owns host `:9090` for
  gRPC — see [docker-compose.yaml](../../docker-compose.yaml) `prometheus.ports`.

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
- All compose services listed: `orchestrator`, `agent-planner`, `agent-coder`, `agent-reviewer`,
  `jaeger`, `prometheus`, `loki`, `otel-collector` (container names are project-prefixed, e.g.
  `persatrix-orchestrator-1`).
- `orchestrator`, agent services, `prometheus`, and `loki` report `(healthy)`. `otel-collector`
  reports `Up …` (no healthcheck — the upstream `otel/opentelemetry-collector-contrib` image is
  distroless and ships no probe binary; this is documented as a deferred item in the RFC 0019
  closeout).

**Verification**:
- [ ] All compose services running
- [ ] Jaeger UI reachable at <http://localhost:16686>
- [ ] Prometheus UI reachable at <http://localhost:9091>

---

### Step 2: Drive a workflow and capture identifiers

**Action**:

```pwsh
$output = ./bin/persatrix run feature-builder --input '{"user_request":"Add a ping endpoint"}'
$run_id = ($output | Select-String 'run_id:\s*([0-9a-f-]+)').Matches.Groups[1].Value
$run_id

# Poll until the run reaches a terminal state (succeeded or failed).
do {
    Start-Sleep -Seconds 3
    $status = ./bin/persatrix status $run_id
    $status | Select-String 'Status:'
} while ($status -match 'Status:\s*(pending|running)')
```

**Expected**:
- `persatrix run` submits asynchronously and prints
  `OK Workflow feature-builder submitted (run_id: <uuid>)` followed by `Status: pending` — plain
  text, not JSON. The `run_id` parse above captures it.
- Terminal state may be `succeeded` or `failed` depending on API key availability and workflow
  execution — both produce trace data.

**Verification**:
- [ ] `run_id` captured
- [ ] Run reaches a terminal state (`succeeded` or `failed`)

---

### Step 3: Jaeger trace lookup by `persatrix.workflow_id`

Service names are set via `OTEL_SERVICE_NAME`. The orchestrator ships as `persatrix-server`
(docker-compose `orchestrator.environment`); the three agent containers share the Python default
`persatrix-agent` (no per-agent override in compose today).

**Action**:

1. Open <http://localhost:16686>.
2. Service: `persatrix-server`. Click **Find Traces**.
3. In **Tags** add `persatrix.workflow_id=feature-builder` and re-run the search.
4. Open the most recent trace. Inspect the span tree.
5. Switch Service to `persatrix-agent` and confirm agent-side spans exist for the same time window
   (see the known gap note below on why they may currently be in a separate trace).

**Expected**:
- The orchestrator trace contains `workflow.run`, `workflow.step`, and `agent.dispatch` spans,
  all carrying `persatrix.workflow_id=feature-builder`.
- An agent trace exists for the same time window with `/persatrix.v1.AgentService/ExecuteTask`
  plus `agent.llm.call` (and, when exercised, `agent.tool.execute`, `agent.memory.episodic.recall`,
  `agent.persona.tick`).
- The LLM-call span (when the API key was present) carries the OTEL Gen-AI semantic-convention
  attributes: `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
  `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons` (a list, even when length 1).

**Verification**:
- [ ] `persatrix-server` trace found by the `persatrix.workflow_id=feature-builder` tag filter
- [ ] `persatrix-agent` trace visible for the same time window
- [ ] `gen_ai.*` attributes present on the LLM-call span (when API key configured)

> **Known gap — cross-process trace propagation (tracked in RFC 0019 closeout follow-up)**:
> Agent spans currently land in a **separate** root trace instead of being stitched as children of
> the orchestrator's `agent.dispatch` span. The Go client injects W3C TraceContext into gRPC
> metadata via `otelgrpc.NewClientHandler()`, but the Python
> `GrpcAioInstrumentorServer().instrument()` does not re-parent the incoming RPC to it — the
> `ExecuteTask` server span has no parent reference. The test therefore verifies the two sides
> independently (both services appear, correct spans and attributes) rather than a single
> connected tree. Once the propagation bug is fixed, this step's verification flips back to
> "single connected trace tree, both service nodes present."

---

### Step 4: Prometheus exemplar click-through

**Action**:

1. Open <http://localhost:9091/graph>.
2. Query: `histogram_quantile(0.95, sum by (le) (rate(agent_llm_duration_milliseconds_bucket[5m])))`.
3. Switch the panel to **Table** view, click **Show exemplars**.
4. Click an exemplar dot; copy its `trace_id`.
5. In Jaeger, paste the `trace_id` into the **Lookup by Trace ID** field at the top.

**Expected**:
- The exemplar resolves to a Jaeger trace. While the Step 3 known gap is outstanding the
  exemplar will typically land in the agent-side trace (LLM spans emit the exemplar), not the
  orchestrator trace.
- The metric name reaches Prometheus in OTLP-translated form (underscores, no dots) **and** with
  the unit suffix applied — i.e. `agent_llm_duration_milliseconds_bucket`, not
  `agent.llm.duration_bucket` and not `agent_llm_duration_bucket`. The Collector's Prometheus
  exporter appends `_milliseconds` because the OTLP metric declares unit `ms`; this is the
  default behaviour (`add_metric_suffixes` is not disabled in
  [config/observability/otel-collector.yaml](../../config/observability/otel-collector.yaml)).
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

1. Submit one workflow that fails (pass an obviously invalid input — the orchestrator will fail
   dispatch and mark the run `failed`):

   ```pwsh
   ./bin/persatrix run feature-builder --input '{}' | Out-Null
   ```

2. Submit ~20 quick `feature-builder` runs back-to-back (or let an autonomous persona tick for a
   few minutes if one is configured). The healthy-tick policy samples 1 % of unmarked traces, so a
   handful of runs is enough to see the rule applied.
3. In Jaeger search by `service=persatrix-server` over the last 15 minutes.

**Expected**:
- The error trace from sub-step 1 is **always retained** (matches the `errors` policy on
  `status_code: ERROR`).
- Orchestrator-side traces from sub-step 2 are **all retained** (the `workflow-traces` policy
  keeps every trace carrying `persatrix.workflow_id`; this attribute is currently set on
  orchestrator spans — see Step 3's known gap for the agent side). This is by design — operators
  want full visibility on user-driven runs.
- Agent-side traces (the orphaned `persatrix-agent` traces from the Step 3 known gap) do **not**
  carry `persatrix.workflow_id`, so they fall through to `healthy-tick-sample` and are retained
  at ~1 %. This sampling behaviour is what we want long-term for autonomous tick traces; we
  currently see it applied to workflow-driven agent traces as a side-effect of the propagation
  bug.

**Verification**:
- [ ] Error trace visible in Jaeger (`persatrix-server` service)
- [ ] All workflow-tagged orchestrator traces from sub-step 2 visible in Jaeger
- [ ] Agent-side (`persatrix-agent`) trace count in Jaeger ≪ workflow runs emitted

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
  - Cross-process trace propagation: agent spans land in a separate root trace from the
    orchestrator (see Step 3 callout). Both sides are verified independently until the Python
    `GrpcAioInstrumentorServer` re-parenting bug is fixed. Tracked in the RFC 0019 closeout
    follow-up issue.
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
- **Fail**: Any step's Verification fails.

---

## Results

_Recorded when the test is next executed._

| Date | Operator | Result | Notes |
|------|----------|--------|-------|
| _pending first run_ | | | |
