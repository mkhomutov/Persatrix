---
id: ISSUE-0251
summary: "`docker-compose.yaml` runs `jaegertracing/all-in-one:1.76.0`, the last release of Jaeger v1, which its project stopped maintaining on 2025-12-31. Jaeger v2 is published under another image name, so Dependabot, which follows tags of one name, will never propose a move."
status: open
severity: low
area: observability
created: 2026-11-14
refs:
  - docker-compose.yaml
  - .github/dependabot.yml
  - docs/observability.md
  - docs/issues/ISSUE-0218-advisory-scanning-covers-rust-only.md
---

# ISSUE-0251: The compose file runs a Jaeger line that has ended

## Summary

The tracing service in `docker-compose.yaml` is
`jaegertracing/all-in-one:1.76.0`. Docker Hub shows that tag pushed on
2025-12-03 and no newer one under that name. Jaeger's download page says
v1 reached end of life on 2025-12-31 and receives no more updates; v2
ships as a different image, `jaegertracing/jaeger`.

## Context

The fix for
[ISSUE-0218](ISSUE-0218-advisory-scanning-covers-rust-only.md) gave the
compose images an update path: a monthly Dependabot pull request. That
path does not reach this image. Dependabot proposes a newer tag of the
same image name, and there is none. GitHub raises no security alert for
compose images either, so a vulnerability found in Jaeger v1 from now on
reaches nothing in this repository.

The service stores the traces the collector sends it. Its web page is
published on the loopback interface only, and no agent or orchestrator
request passes through it.

Read from the compose file, Docker Hub's tag list and Jaeger's download
page on 2026-11-14. No image was pulled or run.

## Impact

Proposed low: a trace store reachable from the host only. The severity
is proposed here, not ruled.

## Proposed fix / investigation path

Move the service to the v2 image. Jaeger v2 is configured as an
OpenTelemetry Collector, so the service's flags and environment change,
and the ports the collector service and `docs/observability.md` name
need checking against it. Run the stack and confirm a trace arrives
before merging: no CI job starts these images.

## Notes

> 2026-11-14 — noticed while predicting what the new Dependabot entry
> for compose files would propose.
