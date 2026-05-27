---
id: ISSUE-0073
summary: "The chat REST endpoint's default 30 s reply timeout is too short for CPU-only local-model (Ollama) inference: a `make demo-ollama` chat turn returns DEADLINE_EXCEEDED on CPU unless the caller passes the per-request `timeout_seconds` override (which the endpoint already supports, clamped to <= 300 s). Capability is sound (real tokens, $0 cloud); the gap is the advertised demo's default UX on CPU."
status: open
severity: low
area: internal/server
created: 2026-05-27
refs:
  - docs/manual-tests/v0.3.4-execution-report.md
  - docs/manual-tests/MT-OLLAMA-001.md
  - docker-compose.ollama.yaml
  - internal/server/chat_handler.go
---

## Summary

The synchronous chat REST endpoint waits a **default 30 s** for the agent's
reply ([`internal/server/chat_handler.go:42`](../../internal/server/chat_handler.go)
`chatDefaultTimeout = 30 * time.Second`). On CPU-only Ollama inference
(`make demo-ollama`, default model `llama3.2`), a full-length persona generation
plus the one-time ~8 s model load runs past 30 s, so the turn returns
`DEADLINE_EXCEEDED` ("agent did not respond in time") even though the agent
completes the call server-side and records real usage.

The endpoint **already supports** a per-request `timeout_seconds` override,
clamped to `[1 s, 300 s]` ([`chat_handler.go:42-44,259-265`](../../internal/server/chat_handler.go)).
Passing `"timeout_seconds":120` makes the same turn succeed (`reply_status="ok"`
in ~93 s, observed in [MT-OLLAMA-001](../manual-tests/MT-OLLAMA-001.md)). So this
is a **default-UX / documentation gap on the advertised demo path**, not a
routing, cost, or correctness defect — the local-model routing, real token
counts (1232/4096), and $0 cloud spend all verify.

## Context

Found during the v0.3.4 release-prep PR 1 MT execution
([report § MT-OLLAMA-001](../manual-tests/v0.3.4-execution-report.md#mt-ollama-001--ollama-local-model-evidence-live),
finding F-6). The README and `make demo-ollama` advertise "chat ember-owl" as
the first thing to try after the local-model society is up
([`docker-compose.ollama.yaml`](../../docker-compose.ollama.yaml),
[`Makefile`](../../Makefile) `demo-ollama`), but neither the demo output nor the
README mentions that a CPU run needs `timeout_seconds`. A user on the most common
(CPU) setup following the README verbatim therefore gets an error on their first
turn.

- [`internal/server/chat_handler.go:42-44`](../../internal/server/chat_handler.go) —
  `chatDefaultTimeout = 30s`, `chatMinTimeout = 1s`, `chatMaxTimeout = 300s`.
- [`internal/server/chat_handler.go:259-265`](../../internal/server/chat_handler.go) —
  `req.TimeoutSeconds` overrides the default, clamped to the bounds above.
- The offline (`MockProvider`) and cloud (Anthropic / OpenAI) paths reply well
  within 30 s, so this is specific to slow local inference.

## Impact

Low — UX on an advertised demo, not correctness:

1. **`make demo-ollama` + a default-timeout chat turn returns `DEADLINE_EXCEEDED`
   on CPU.** First-run experience for the "run a real local model" promise
   face-plants unless the user knows to pass `timeout_seconds`.
2. No data/cost impact: the agent completes the generation server-side, records
   real tokens, and spends $0 cloud. A GPU host with the default 30 s is fine.

## Proposed fix / investigation path

Pick one (cheapest first); all are doc/config, no new mechanism:

1. **Document `timeout_seconds` in the demo-ollama path** — the `make demo-ollama`
   success banner and the README "Run a real local model" section show a chat
   example that passes `"timeout_seconds":120`, with a one-line note that CPU
   inference is slow. (MT-OLLAMA-001 already does this; mirror it user-facing in
   release-prep PR 2's provider-neutral onboarding sweep.)
2. **Raise the default chat timeout when a local provider mode is active**
   (`PERSATRIX_OLLAMA` / a local `provider`) — e.g. default to a higher value for
   local modes so the out-of-the-box demo works without the override, while cloud
   modes keep the snappy 30 s.
3. **Lower the demo's `max_tokens`** so a CPU generation fits inside 30 s
   (trades reply completeness for latency on the demo only).

Weigh (2) against keeping a single default: option (1) is the smallest and is the
natural fold-in to PR 2's onboarding work; (2) gives the best zero-config demo UX.

## Notes

> 2026-05-27 — captured during the v0.3.4 release-prep PR 1 MT sweep (finding
> F-6). Not a PR 1 blocker and not a v0.3.4 correctness gate: the MT passes with
> the existing `timeout_seconds` knob; this tracks the advertised-demo default UX.
