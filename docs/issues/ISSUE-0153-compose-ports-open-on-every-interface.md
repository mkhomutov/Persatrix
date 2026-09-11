---
id: ISSUE-0153
summary: "The demo compose stack still publishes the agents' gRPC ports (50051–50057) and the observability ports (4317, 4318, 8889, 16686, 9091, 3100) on every host interface; the agent gRPC service has no authentication, so anyone on the developer's network can run persona turns and spend provider budget"
status: open
severity: medium
area: deployment/docker
created: 2026-09-11
refs:
  - docker-compose.yaml
  - docker-compose.multivendor.yaml
  - tests/unit/python/test_docker_compose_host_publish.py
  - proto/task.proto
  - agents/server.py
  - docs/guides/web-console.md
---

## Summary

`docker-compose.yaml` publishes most of its host ports without a host
address. Docker publishes such a mapping on every interface (`0.0.0.0` and
`[::]`), so the port is reachable from the developer's whole network, not only
from their own machine. PR #918 fixed this for the orchestrator's `:8080` (the
REST API and the web console) and added a guard test that checks the
orchestrator service only. Found in the review of PR #918 (findings F-1 and
F-4).

## Context

The publishes that still name no address:

| Service | Host port(s) | What answers there |
|---|---|---|
| `agent-planner`, `agent-coder`, `agent-reviewer`, `agent-ember-owl`, `agent-iron-fox`, `agent-nova-sparrow` | 50051–50056 | The agent gRPC service |
| `agent-slate-heron` (`docker-compose.multivendor.yaml`) | 50057 | The agent gRPC service |
| `otel-collector` | 4317, 4318, 8889 | OTLP ingest (gRPC and HTTP) and the Collector's Prometheus exporter |
| `jaeger` | 16686 | The Jaeger UI (traces) |
| `prometheus` | 9091 | The Prometheus UI and API (metrics) |
| `loki` | 3100 | The Loki push and query API (logs) |

The agent gRPC service (`proto/task.proto`) listens through
`add_insecure_port` with only a logging interceptor (`agents/server.py`), so it
has no authentication. The agents do not need these host publishes to work:
they register with the orchestrator over the compose network, advertising
`agent-*:5005x`.

The guard test, `tests/unit/python/test_docker_compose_host_publish.py`, has
two blind spots the same review noted. It reads only the `orchestrator`
service. And `network_mode: host` on the orchestrator would expose its
`0.0.0.0:8080` with no `ports` entry at all, which the test would not see. (It
also globs only the repo-root `docker-compose*.yaml`, which is every compose
file today.)

## Impact

- On a demo stack running a paid provider, anyone on the same network can send
  the agents work over gRPC — running persona turns and spending the
  operator's provider budget — and read what the agents serve back.
- Loki, Jaeger and Prometheus serve logs, traces and metrics, which can carry
  conversation content, and the Collector accepts unauthenticated OTLP writes.
- The web console guide's rule — keep the unauthenticated surface on
  localhost — holds for the orchestrator only.

## Proposed fix / investigation path

1. Extend the guard test from the orchestrator to every service in every
   `docker-compose*.yaml`, and make it reject `network_mode: host`. Confirm it
   fails.
2. Bind each publish to `127.0.0.1` (for example `"127.0.0.1:50051:50051"`), or
   drop an agent's host publish if nothing on the host uses it.
3. Keep the host-side users working: the README links
   `http://localhost:16686` and `http://localhost:9091`, and
   `tests/integration/test_observability_e2e.py` defaults to `localhost` for
   16686, 9091, 3100 and 4318. A loopback publish still serves all of them.

## Notes

> 2026-09-11 — filed from the review of PR #918 (F-1: the remaining ports had
> no tracking issue; F-4: the guard test's blind spots).
