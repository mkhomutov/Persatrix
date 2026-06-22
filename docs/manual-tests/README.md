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

## Channel Governance (RFC 0030)

| ID | Title | Status |
|----|-------|--------|
| [MT-CHANNEL-GOV-002](MT-CHANNEL-GOV-002.md) | Floor control — ordered, mutually-aware multi-persona replies (RFC 0030 Layer 2.5) | Active |
| [MT-CHANNEL-RELEVANCE-001](MT-CHANNEL-RELEVANCE-001.md) | Relevance gate Tier A — addressing-aware directedness (a `@`-mention to one persona is not answered by everyone) (RFC 0030 Layer 3) | Active |

## Channel Configuration (RFC 0050)

| ID | Title | Status |
|----|-------|--------|
| [MT-CHANNEL-CONFIG-001](MT-CHANNEL-CONFIG-001.md) | Live-edit a governance knob from the CLI — the running channel honors it without restart, and it survives one (Phase 1) | Active |
| [MT-CHANNEL-CONFIG-002](MT-CHANNEL-CONFIG-002.md) | Edit a governance knob from the web console — the running channel honors it, and the CLI reads back the same value (Phase 2, G4) | Active |

## Channel Memory / Verbatim Recall (RFC 0036 + RFC 0035)

| ID | Title | Status |
|----|-------|--------|
| [MT-PERSONA-RECALL-001](MT-PERSONA-RECALL-001.md) | Verbatim recall is scoped to where the persona was present — add → remove → re-add recalls both stints, never the removal gap (RFC 0036 over the RFC 0035 ledger) | Active |

## Web Console (RFC 0048)

| ID | Title | Status |
|----|-------|--------|
| [MT-CONSOLE-001](MT-CONSOLE-001.md) | Web Console fresh-stack Interactions slice (`--enable-ui` → chat + channel timeline) | Active |
| [MT-CONSOLE-002](MT-CONSOLE-002.md) | Web Console `@`-mention compose & fan-out (typeahead → `mentions` → highlight) | Active |

## Session

| ID | Title | Status |
|----|-------|--------|
| [MT-SESSION-001](MT-SESSION-001.md) | `PERSATRIX_SESSION_ID` cross-process write contract | Active |
| [MT-SESSION-002](MT-SESSION-002.md) | Session operator surface, live (`new`/`use`/`list`/`archive`/`current` + resolution chain) | Active |
| [MT-SESSION-003](MT-SESSION-003.md) | F-3 recall isolation + within-session continuity | Active |

## Epoch

| ID | Title | Status |
|----|-------|--------|
| [MT-EPOCH-001](MT-EPOCH-001.md) | Epoch structural run-isolation (fresh `PERSATRIX_EPOCH`/`--epoch`, same room+user, inherits nothing) | Active |

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
| [MT-PERSONA-005](MT-PERSONA-005.md) | Benign user message is not deflected as a prompt-injection (external-data carve-out, F-1) | Active |
| [MT-PERSONA-006](MT-PERSONA-006.md) | Persona describes its conversation window honestly — no memory denial, no invented count (F-2) | Active |
| [MT-PERSONA-007](MT-PERSONA-007.md) | Persona does not over-promise cross-conversation memory; admits empty recall (F-3a) | Active |
| [MT-PERSONA-008](MT-PERSONA-008.md) | A person introduced in one channel is recalled in another; room notes stay scoped (F-3b) | Active |
| [MT-PERSONA-009](MT-PERSONA-009.md) | Group channel has a shared roster (who's here + roles); DM has none (F-4) | Active |
| [MT-PERSONA-CONVERSATION-001](MT-PERSONA-CONVERSATION-001.md) | Persona conversational continuity (DM) | Active |
| [MT-PERSONA-CONVERSATION-002](MT-PERSONA-CONVERSATION-002.md) | Persona conversational continuity (group channel — per-peer attribution + cross-peer pronoun binding, RFC 0034 Phase 2) | Active |

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
| [MT-MEMORY-005](MT-MEMORY-005-dementia-test.md) | Persona Memory — Dementia Test | Active |

## Cost

| ID | Title | Status |
|----|-------|--------|
| [MT-COST-001](MT-COST-001.md) | `GET /api/v1/cost/summary` reports token usage for a completed run | Active |
| [MT-COST-002](MT-COST-002.md) | Workflow exceeding budget is aborted with the expected reason | Active |

## Model Alias / Provider (RFC 0033 — v0.3.4)

| ID | Title | Status |
|----|-------|--------|
| [MT-ALIAS-001](MT-ALIAS-001.md) | Alias-routed agent reports correctly-keyed, non-zero cost (live) + `model_alias` span | Active |
| [MT-ALIAS-002](MT-ALIAS-002.md) | One-line provider swap re-routes the same agent (headline claim) | Active |
| [MT-OFFLINE-001](MT-OFFLINE-001.md) | Offline mode (`MockProvider`) — full round-trip at $0, zero network | Active |
| [MT-OLLAMA-001](MT-OLLAMA-001.md) | Ollama local model — real tokens, $0 cloud spend | Active |

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
| v0.3.1 | [v0.3.1-execution-report.md](v0.3.1-execution-report.md) | ✅ Complete |
| v0.3.2 | [v0.3.2-execution-report.md](v0.3.2-execution-report.md) | ✅ Complete |
| v0.3.3 | [v0.3.3-execution-report.md](v0.3.3-execution-report.md) | ✅ Complete |
| v0.3.4 | [v0.3.4-execution-report.md](v0.3.4-execution-report.md) | ✅ Complete |
| v0.3.5 | [v0.3.5-execution-report.md](v0.3.5-execution-report.md) | ✅ Complete |
| v0.3.6 | [v0.3.6-execution-report.md](v0.3.6-execution-report.md) | ✅ Complete |
| v0.3.7 | [v0.3.7-execution-report.md](v0.3.7-execution-report.md) | ✅ Complete — clean pass on tip `92a5a00` (the first-run blocker [ISSUE-0094](../issues/ISSUE-0094-everyone-broadcast-rejected-by-agent-inbound-validation.md) / MT-CHANNEL-RELEVANCE-001 Step 4 fixed in [#562](https://github.com/mkhomutov/Persatrix/pull/562)/[#563](https://github.com/mkhomutov/Persatrix/pull/563), re-verified live) |
| v0.3.8 | [v0.3.8-execution-report.md](v0.3.8-execution-report.md) | ✅ Complete — clean pass on tip `8897727` (Tier B no-pile-on + end-vote convergence + chair stall + interaction-summary surface + RFC 0050 channel config + the combined convergence walkthrough, all live; MT-INTERACTION-SUMMARY-001 Part A DM `idle_gap` accepted-with-known-gap, structurally pinned; one non-blocking finding F-1) |
| v0.3.9 | [v0.3.9-execution-report.md](v0.3.9-execution-report.md) | 🔄 In progress — recall-surface structural / automated release-blocker gates all green live on host (tip `2bd72a8`); the headline live MT (MT-PERSONA-RECALL-001) pending the Docker stack |

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
