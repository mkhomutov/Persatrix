# Manual Test Index

This directory contains manual test documents for Persatrix. Each document provides step-by-step
procedures, expected results, and a results table for recording execution outcomes.

Tests are organised by **feature area**. IDs follow the pattern `MT-<AREA>-<NNN>`.

---

## Workflow

| ID | Title | Status |
|----|-------|--------|
| [MT-WORKFLOW-001](MT-WORKFLOW-001.md) | Submit YAML workflow via REST, poll to completion | Active |
| [MT-WORKFLOW-002](MT-WORKFLOW-002.md) | Submit invalid workflow, verify clean error response | Active |

## Agent

| ID | Title | Status |
|----|-------|--------|
| [MT-AGENT-001](MT-AGENT-001.md) | Task agent executes a builtin tool (no LLM required) | Active |

## CLI

| ID | Title | Status |
|----|-------|--------|
| [MT-CLI-001](MT-CLI-001.md) | `persatrix run` end-to-end against a running orchestrator | Active |

## Channels (CLI)

| ID | Title | Status |
|----|-------|--------|
| [MT-CHANNEL-001](MT-CHANNEL-001.md) | `persatrix channel list` / `join` against a docker-composed orchestrator | Active |
| [MT-CHANNEL-002](MT-CHANNEL-002.md) | `persatrix channel send` / `reply` / `history` against a docker-composed orchestrator | Active |
| [MT-CHANNEL-003](MT-CHANNEL-003.md) | `persatrix channel watch` polling, dedup, full-page warning | Active |
| [MT-CHANNEL-004](MT-CHANNEL-004.md) | Human-mentions-agent end-to-end (live LLM reply) | Active |
| [MT-CHANNEL-005](MT-CHANNEL-005.md) | DM canonicalization round-trip | Active |
| [MT-CHANNEL-006](MT-CHANNEL-006.md) | Channel deletion + cascade (REST DELETE pair) | Active |

## Session

| ID | Title | Status |
|----|-------|--------|
| [MT-SESSION-001](MT-SESSION-001.md) | `PERSATRIX_SESSION_ID` cross-process write contract | Active |

## Config

| ID | Title | Status |
|----|-------|--------|
| [MT-CONFIG-001](MT-CONFIG-001.md) | `make validate` catches malformed `config/agents.yaml` | Active |

## Persona

| ID | Title | Status |
|----|-------|--------|
| [MT-PERSONA-001](MT-PERSONA-001.md) | Start semi-autonomous persona; verify tick loop and logged actions | Active |
| [MT-PERSONA-002](MT-PERSONA-002.md) | Persona handles an inbound channel message and produces a logged response | Active |
| [MT-PERSONA-003](MT-PERSONA-003.md) | Empty-context TICK short-circuit suppresses LLM calls (RFC 0017 §F) | Active |
| [MT-PERSONA-004](MT-PERSONA-004.md) | Persona does not adopt user-name in first person (grounding clause) | Active |
| [MT-PERSONA-CONVERSATION-001](MT-PERSONA-CONVERSATION-001.md) | Persona conversational continuity (DM) | Draft |

## Chat

| ID | Title | Status |
|----|-------|--------|
| [MT-CHAT-001](MT-CHAT-001.md) | Chat REST endpoint: send message, receive reply | Active |
| [MT-CHAT-002](MT-CHAT-002.md) | `persatrix chat` CLI interactive session | Active |
| [MT-CHAT-003](MT-CHAT-003.md) | Chat session continuity: messages persist across agent restart | Active |
| [MT-CHAT-004](MT-CHAT-004.md) | User-agent relationship: trust score evolves after chat exchanges | Active |

## Memory

| ID | Title | Status |
|----|-------|--------|
| [MT-MEMORY-001](MT-MEMORY-001.md) | Episodic memory: write and recall across agent restart | Active |
| [MT-MEMORY-002](MT-MEMORY-002.md) | Relationship memory: trust score updates after N exchanges | Active |
| [MT-MEMORY-003](MT-MEMORY-003.md) | Working memory: summarisation triggers near context-window threshold | Complete |
| [MT-MEMORY-004](MT-MEMORY-004.md) | Memory injection token budget: per-event bound holds (RFC 0017 §B) | Active |
| [MT-MEMORY-005](MT-MEMORY-005-dementia-test.md) | Persona Memory — Dementia Test | Draft |

## Cost

| ID | Title | Status |
|----|-------|--------|
| [MT-COST-001](MT-COST-001.md) | `GET /api/v1/cost/summary` reports token usage for a completed run | Active |
| [MT-COST-002](MT-COST-002.md) | Workflow exceeding budget is aborted with the expected reason | Active |

## Integration

| ID | Title | Status |
|----|-------|--------|
| [MT-INTEGRATION-001](MT-INTEGRATION-001.md) | Docker Compose full-stack smoke test end-to-end | Active |

## Observability

| ID | Title | Status |
|----|-------|--------|
| [MT-LOGS-001](MT-LOGS-001.md) | `persatrix logs` end-to-end (REST + SSE follow + restart durability + pretty mode) | Active |
| [MT-OTEL-001](MT-OTEL-001.md) | OpenTelemetry traces + metrics + Collector tail-sampling | Active |

---

## Execution Reports

| Version | Report | Status |
|---------|--------|--------|
| v0.2.0 | [v0.2-execution-report.md](v0.2-execution-report.md) | ✅ Complete |
| v0.2.1 | [v0.2.1-execution-report.md](v0.2.1-execution-report.md) | ✅ Complete |
| v0.2.2 | [v0.2.2-execution-report.md](v0.2.2-execution-report.md) | ✅ Complete |
| v0.2.3 | [v0.2.3-execution-report.md](v0.2.3-execution-report.md) | ✅ Complete |
| v0.3.0 | [v0.3.0-execution-report.md](v0.3.0-execution-report.md) | ✅ Complete |
| v0.3.1 | [v0.3.1-execution-report.md](v0.3.1-execution-report.md) | 🔄 In progress |

---

## 2026-04-18 Full Pass Summary

Executed by mkhomutov on Windows 11. All testable tests run from clean state.

| ID | Result | Notes |
|----|--------|-------|
| MT-CONFIG-001 | **Pass** | All 5 mutation steps pass. |
| MT-CLI-001 | **Pass** | All 5 steps + 3 edge cases pass. |
| MT-WORKFLOW-001 | **Pass** | API terminal-state mode; terminal `failed` in <1 s. |
| MT-WORKFLOW-002 | **Pass** | All 4 error cases and health check pass. |
| MT-AGENT-001 | **Pass** | All 7 integration tests pass with `PYTHONPATH=agents/generated`. **Code issue**: `make test-integration` / `make run-agent` fail without PYTHONPATH (grpc stub bare import). |
| MT-COST-001 | **Partial** | Step 1 (endpoint shape) pass; Steps 2–4 require live agents + API key. |
| MT-COST-002 | **Partial** | Fixture YAML corrected (wrong format); Step 1 HTTP code corrected (200→201). Steps 1–5 require live agents + API key. |
| MT-MEMORY-001 | **Pass** | All 4 steps pass. **Doc fix**: Step 2 needs `logging.basicConfig()`. |
| MT-MEMORY-002 | **Pass** | All 5 steps pass. **Doc fix**: `get_relationship()` replaced with `get_trust()` / `get_relationship_summary()`. |
| MT-MEMORY-003 | **Partial** | Step 1 (threshold detection) pass. Steps 2–3 require API key. **Doc fix**: `set_section` → `add_section(ContextSection(...))`, `_sections.values()` → `total_tokens()`. **Code fix**: wrong model name `claude-haiku-4` → `claude-haiku-4-5`; `LLMClient()` requires `LLMClient(AnthropicProvider())`. |
| MT-PERSONA-001 | **Partial** | Startup log verified with PYTHONPATH fix. Steps 2–5 require API key. **Code issue**: `make run-agent` fails without `PYTHONPATH=agents/generated`; port 50051 may conflict. |
| MT-PERSONA-002 | **Fail** | Step 1 health-check pass (`SERVING`) on `127.0.0.1:50345`; Step 2 fails with gRPC `UNIMPLEMENTED` (`ChannelService/SendMessage` method not registered in agent server). |
| MT-INTEGRATION-001 | **Not run** | Requires `ANTHROPIC_API_KEY` (Docker available). |

---

## 2026-04-18 Retest Pass Summary

Executed by mkhomutov on Windows 11 after code fix (`PYTHONPATH` in Makefile + `conftest.py`).

| ID | Result | Notes |
|----|--------|-------|
| MT-CONFIG-001 | **Pass** | All 5 mutation steps + teardown pass. No new issues. |
| MT-AGENT-001 | **Pass** | All 7 tests pass via `make test-integration` (no manual PYTHONPATH needed). PYTHONPATH code issue resolved. |
| MT-MEMORY-001 | **Pass** | All 4 steps pass. Scripts verified correct. |
| MT-MEMORY-002 | **Pass** | All 5 steps pass. **Doc fix**: added pre-run cleanup step (remove stale DB + WAL files). |
| MT-MEMORY-003 | **Pass** | All 3 steps pass. 1 212 → 961 tokens, compression logs present, all sections non-empty. **Code fix**: `compression_model` default `claude-haiku-4` → `claude-haiku-4-5`; `LLMClient(AnthropicProvider())` required. |
| MT-WORKFLOW-001 | **Pass** | HTTP 201, terminal `failed` (<1 s), all required fields present. |
| MT-WORKFLOW-002 | **Pass** | All 5 error cases pass. |
| MT-CLI-001 | **Pass** | All 5 steps pass. `make run-agent` PYTHONPATH fix verified. |
| MT-COST-001 | **Pass** | Full execution completed on port 8081 with live agents and API key. Workflow reached terminal `failed` (acceptable), and cost summary showed non-zero usage (`daily_output_tokens: 746`) with stable response structure across repeated calls. |
| MT-COST-002 | **Pass** | Full live retest after fix `1232236` (`recordStepUsage` on error path). `run_id=e624e00b`. Steps 1–3 pass (HTTP 201, `failed` in ~7 s, error = `"max_tokens limit reached"`). Step 4 PASS: `daily_output_tokens=246` from zero — tokens recorded despite abort. Step 5 N/A (fixture committed). |
| MT-PERSONA-001 | **Pass** | Full live pass. Steps 1–4 all pass (scheduler log, 13+ LLM ticks, no errors). Step 5 interactive Ctrl+C verified graceful shutdown logs (`Shutting down...`, `Tick scheduler stopped`, `Agent server stopped`); Windows PowerShell reports exit code `1` for this SIGINT path. **Note**: non-fatal episodic/notes FTS5 query fallback warnings were observed during persona activity. |
| MT-PERSONA-002 | **Pass** | Full live pass on `127.0.0.1:50354` using generated Python gRPC stubs (`grpcurl` not installed). HealthCheck returned `SERVING`; `SendMessage` returned `delivered=true`; logs show inbound prompt routing and `Event: message_received -> Actions: ['complete_task']`. No traceback or timeout. No idle-skip lines appeared after the message because the persona remained active. Non-blocking FTS5 fallback warnings still occur on punctuation-heavy queries. |
| MT-INTEGRATION-001 | **Pass** | Full live run completed after fix (`Dockerfile.orchestrator` Go 1.25). All compose services healthy; `/healthz` pass; agents listed healthy (`planner`, `code-writer`, `code-reviewer`); workflow terminal `failed` in ~41 s with `finished_at` present (acceptable for this smoke test, investigate `Max LLM call iterations exceeded` separately); Jaeger traces present (`workflow.run` + child spans); teardown pass with no residual `persatrix_*` volumes. |

---

## Conventions

- **Test IDs** are unique across all areas and never reused after deprecation.
- **Status** values: `Active` | `Draft` | `Deprecated`
- A test file template is at [docs/templates/MANUAL_TEST_TEMPLATE.md](../templates/MANUAL_TEST_TEMPLATE.md).
- Tests that require `ANTHROPIC_API_KEY` are noted in their **Preconditions** section.
