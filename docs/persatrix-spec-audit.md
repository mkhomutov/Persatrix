# Persatrix Specification Audit — Findings

> **STATUS: ALL 45 ISSUES RESOLVED** (April 8, 2026)
>
> Audit of `ai-agents-orchestration-spec.md` (main, 1413 lines) and
> `persatrix-extension-spec.md` (extension, 3059 lines). 45 findings across
> 8 categories, all fixed. See resolution notes below each category.

---

## Resolution Summary

| Category | Count | Resolution |
|----------|-------|------------|
| Broken numbering | 5 | ✅ All section/subsection numbers corrected |
| Schema contradictions | 8 | ✅ Unified agent YAML schema; dual protobuf explained; context/temp guidance added |
| Interface contract gaps | 7 | ✅ PersonaAgent async base class; 30+ API endpoints; SSE streaming; spawn_sub_agent() |
| Missing specifications | 10 | ✅ Added §6.7-6.9, §12.1-12.8 (error handling, testing, config validation, health checks, logging, human participants, hot-reload, state persistence) |
| Security gaps | 5 | ✅ Added §6.8 (rate limiting), §6.9 (input sanitization), E10.3 (observer privacy) |
| Architecture gaps | 5 | ✅ Diagram updated; project structure expanded; embedding infra added; state persistence specified; dependency DAG added |
| Duplication/overlap | 3 | ✅ Cross-references added between E7↔E9.7, E8.8↔E9.2↔§6.3, E9.3↔E9.6 |
| Minor issues | 2 | ✅ Project named "Persatrix" throughout; BUSL 1.1 license specified |

---

## 1. Broken Section Numbering (5 issues)

These will confuse anyone navigating the docs and break any cross-references.

| # | Location | Problem |
|---|----------|---------|
| 1 | Extension E7 (Memory) | Subsections are labeled **E6.1** and **E6.2** instead of E7.1 and E7.2 |
| 2 | Extension E11 (Blueprints) | Subsections are labeled **E8.1**, **E8.2**, **E8.3** instead of E11.1, E11.2, E11.3 |
| 3 | Extension E3.2 (Sub-Agents) | Formatted as `## E3.2` (top-level heading) instead of `### E3.2` (nested under E3). This makes it appear as a peer to E3 rather than a subsection |
| 4 | Extension E3.2 sub-headings | "Why Sub-Agents", "How It Works", "Sub-Agent Definition", etc. use `###` without numbered prefixes, inconsistent with every other section in both docs |
| 5 | Extension E10 (Observation) | Subsections were renumbered to E10.1 and E10.2, but the content still references "E7.1" patterns internally |

---

## 2. Schema Contradictions & Incompatibilities (8 issues)

The main spec and extension spec define overlapping but incompatible data models.

| # | Problem | Details |
|---|---------|---------|
| 6 | **Agent YAML schema divergence** | Main spec (§4.1) uses `role: "Writes clean code"` as a flat string. Extension (E2.1) uses `persona.title: "VP of Engineering"` with a rich nested structure. No guidance on how these merge — does `role` become `persona.title`? Is `role` deprecated? Can you have an agent without a persona? |
| 7 | **Capabilities field missing from persona** | Main spec agents have `capabilities: [code_generation, code_review]`. The extension persona schema has no `capabilities` field at all. Where do capabilities live in the extended model? |
| 8 | **Retry/timeout fields missing from persona** | Main spec agents have `max_retries: 2` and `timeout_seconds: 120`. Extension persona schema doesn't include these. Are they inherited from a global config? Moved to a different section? |
| 9 | **Two competing protobuf schemas** | Main spec (§4.3) defines `TaskMessage` for orchestrator↔agent task passing. Extension (E5.3) defines `AgentMessage` for inter-agent communication. No document explains the relationship between these two schemas, when each is used, or how they coexist in the same system. |
| 10 | **Temperature guidance contradicts** | Main spec example: `temperature: 0.3` on code-writer agent. Extension example: `temperature: 0.7` with comment "higher for personality variance." No guidance on when to use which, or whether persona agents should always have higher temperature. |
| 11 | **Optimization profile uses invalid YAML** | E9.10 simulation profile: `tiers: [none < 1h, summarize < 24h, distill < 7d, abstract > 7d]` — this is pseudo-syntax, not valid YAML. Would fail parsing. |
| 12 | **Context window size contradiction** | E9.3 sets `max_context_tokens: 80000` as the default. E9.10 quality profile sets `max_context_tokens: 150000`. No mention of which models support 150k context, or what happens if the selected model can't handle the configured window. |
| 13 | **MCP transport mismatch** | Main spec (§5.2) mentions `stdio` and `sse` MCP transports. A2A protocol (E8.6) uses HTTP/JSON-RPC. No specification for how A2A's HTTP transport maps to the internal gRPC communication layer. |

---

## 3. Interface Contract Gaps (7 issues)

The Python `BaseAgent` interface and Go API are severely underspecified for the extended functionality.

| # | Problem | Details |
|---|---------|---------|
| 14 | **BaseAgent can't support personas** | `BaseAgent` (§8.1) has only `handle(task) → output` and `capabilities`. No hooks for: receiving channel messages, accessing persona state, spawning sub-agents, delegating to other agents, sending messages to channels, or running the autonomous agent loop. A `PersonaAgent` base class is implied but never defined. |
| 15 | **TaskOutput too limited** | `TaskOutput` has only `status`, `result`, `metadata`. No way to express: "I want to spawn a sub-agent", "I'm delegating this to Mike", "I'm posting this to #eng-general", or "I need human approval." These are all actions a persona agent can take, but the interface can't represent them. |
| 16 | **No async interface** | `handle()` is synchronous. Autonomous agents (E3.1) need an async event loop. Meeting protocols need turn-based interaction. Long-running tasks need progress reporting. None of this fits a sync `handle → return` pattern. |
| 17 | **Orchestrator API drastically incomplete** | §8.2 lists only 6 REST endpoints (workflow run, agent register/list/delete, execution logs). Missing endpoints for: channels (CRUD, message send, history), organizations (CRUD), bridges (CRUD, status), A2A (agent cards, external agent management), nodes (register, drain, migrate), observers (attach, detach), personas (state, relationships), sub-agents (active list, kill), cost (budget status, reports), evaluations (run, results), optimization (cache stats, profile switch). |
| 18 | **No WebSocket/SSE endpoint** | The orchestrator needs a real-time streaming endpoint for: live observation of agent conversations, session replay, dashboard integration, and bridge event delivery. Only REST polling is specified. |
| 19 | **No sub-agent interface from agent side** | Extension (E3.2) defines `SubAgentRequest` and `SubAgentResult` as Python dataclasses, but there's no mechanism for an agent to actually call this — no `spawn_sub_agent()` method on the base class, no gRPC service definition for it, no explanation of how the request reaches the orchestrator. |
| 20 | **Tool interface doesn't support async tools** | The `@tool` decorator (§5.1) shows synchronous functions. MCP tools, HTTP requests, and shell commands can take significant time. No `async` support is specified. |

---

## 4. Missing Specifications (10 issues)

Entire areas that are implied or mentioned but never specified.

| # | Missing Area | Impact |
|---|-------------|--------|
| 21 | **Error handling & resilience** | No specification for: LLM provider errors (rate limits, 5xx, context overflow), agent process crashes, MCP server failures, bridge connection drops, partial workflow failures. Only "retry on failure" is mentioned. Need: circuit breakers, fallback chains, dead letter queues, graceful degradation. |
| 22 | **Testing framework** | No testing strategy section. How do you test agents? Mock LLM responses? Record/replay? Deterministic seed mode? Test personas in isolation? Test workflows end-to-end? This is critical for AgentOps lifecycle (development → testing → production). |
| 23 | **Configuration validation** | How is YAML validated? What errors does the user see for invalid schemas? Are there JSON Schema definitions for agent/workflow/org/channel configs? Can you `orch validate` before running? |
| 24 | **Schema versioning & migration** | No version field in any YAML schema. How do you evolve the agent definition format? What happens when upgrading from v0.1 to v0.2 config format? Need: schema version field, migration tooling, backward compatibility policy. |
| 25 | **Health checks & liveness** | How does the orchestrator know an agent process is alive? gRPC health checking protocol? Heartbeat? What's the detection time for a dead agent? This is especially critical for distributed mesh. |
| 26 | **Graceful shutdown & draining** | What happens to in-flight tasks when a node shuts down? When an agent is unregistered? When a workflow is cancelled? Need: drain mode, task handoff, state persistence, cleanup hooks. |
| 27 | **Logging format** | "Structured JSON logging" is mentioned repeatedly but the actual log schema is never defined. What fields? What levels? How do logs correlate with OTEL traces? |
| 28 | **Human as a participant** | Human-in-the-loop is only specified as approval gates (§6.5). No specification for a human as an actual participant in the agent society — receiving messages in channels, responding in conversations, taking a turn in a debate protocol. The bridge architecture could support this, but it's not called out. |
| 29 | **Agent hot-reload** | No specification for changing a persona's config (system prompt, tools, permissions) without restarting the agent process. Critical for iterating on persona design in development and for live adjustments in long-running simulations. |
| 30 | **Conversation/state export & portability** | No specification for exporting an entire simulation state (agent memories, channel histories, relationship graphs) for: backup, migration between instances, sharing experiments, or reproducibility. Checkpoints are mentioned in E10.2 but format is unspecified. |

---

## 5. Security Gaps (5 issues)

| # | Gap | Risk |
|---|-----|------|
| 31 | **A2A → internal permission mapping** | External A2A agents with "restricted" trust are mentioned (E8.6) but there's no specification for how A2A task requests map to the internal permission system. Can an external agent trigger tool use? Access channels? What's the permission boundary? |
| 32 | **Prompt injection via bridges** | §6.5 covers prompt injection from tool outputs. No equivalent protection for: inbound email content, Slack messages, or A2A task payloads. These are all untrusted external inputs that flow directly into agent context. |
| 33 | **Blocked bridge message handling** | Bridge security (E5.4) has content filtering that blocks messages with patterns like "password" or "credit card". No specification for what happens to blocked messages — silently dropped? Notification to sender? Logged as security event? The agent might keep retrying. |
| 34 | **Agent action rate limiting** | Token budgets limit LLM spend, but there's no rate limiting on agent *actions*: messages per minute, tool calls per minute, sub-agent spawns per minute. An autonomous agent could flood a channel with 1000 messages/minute without hitting a token budget. |
| 35 | **Observation privacy** | Passive observers (E10.1) can see "all_direct_messages" and "agent_internal_state." In a social experiment with informed consent protocols, there's no specification for: what agents are told about being observed, whether consent is required, or data anonymization for research export. |

---

## 6. Architecture & Design Gaps (5 issues)

| # | Gap | Impact |
|---|-----|--------|
| 36 | **Main spec architecture diagram outdated** | §3.2 shows a simple 3-agent diagram with no persona layer, no channels, no bridges, no mesh, no sub-agents. The extension (E13) has a more complete diagram but they're disconnected. A reader of the main spec gets a misleading mental model. |
| 37 | **Project structure incomplete** | §10 directory tree is missing: `channels/`, `personas/`, `protocols/`, `bridges/`, `observers/`, `optimization/`, `a2a/`, `mesh/`, `memory/`, `templates/`, `blueprints/`, `evaluators/`. It only reflects v0.1 scope but should at least hint at where v0.2+ code will live. |
| 38 | **Embedding model infrastructure missing** | Semantic cache (E9.4) and relevance-filtered history injection (E9.6) both require embedding models, but no embedding infrastructure is specified: no model selection, no storage backend, no indexing strategy, no latency budget. |
| 39 | **State persistence unspecified** | In-memory state storage is mentioned for MVP, but: what state exactly? Agent persona state, channel message history, workflow execution context, relationship graphs, optimization caches — these are all different state domains with different persistence requirements, consistency models, and backup needs. |
| 40 | **No dependency graph between features** | Features are listed in MVP phases but dependencies between them aren't explicit. E.g., communication protocols (v0.2) depend on channels (v0.2) and the autonomous agent loop (v0.2) depends on channels too. If channels slip, everything slips. A dependency DAG would make phasing more realistic. |

---

## 7. Duplication & Overlap (3 issues)

| # | Overlap | Resolution needed |
|---|---------|-------------------|
| 41 | **Memory: E7 vs E9.7** | E7 defines the memory architecture (tiers, config). E9.7 defines memory compression (tiered compression, relationship compression, dedup). These are the same topic split across two sections with no cross-reference. Should be consolidated or at minimum explicitly linked. |
| 42 | **Cost tracking: §6 vs E8.8 vs E9.2** | Main spec resource limits (§6.3), AgentOps cost management (E8.8), and model tiering (E9.2) all touch cost from different angles. No single authoritative section on "how cost works end-to-end." |
| 43 | **Context management: E9.3 vs E9.6** | Context window management (E9.3 — priority scoring, what to drop) and communication optimization (E9.6 — channel history injection strategy, notification filtering) both decide what goes into an agent's context. These interact heavily but are specified independently. |

---

## 8. Minor Issues (2 issues)

| # | Issue | Details |
|---|-------|---------|
| 44 | **Inconsistent project name** | Main spec never names the project. Extension uses "Persatrix" throughout. CLI examples use `orch`. The YAML examples show `Persatrix/` as the project root. Should be established once in §1 of the main spec. |
| 45 | **No license or contribution model** | No mention of: license choice, contribution guidelines, code of conduct, or governance model. This affects adoption decisions. |

---

## Summary

| Category | Count | Severity |
|----------|-------|----------|
| Broken numbering | 5 | Low (cosmetic, easy fix) |
| Schema contradictions | 8 | **High** (blocks implementation) |
| Interface contract gaps | 7 | **Critical** (can't build without resolving) |
| Missing specifications | 10 | **High** (will cause ad-hoc decisions during implementation) |
| Security gaps | 5 | **High** (must resolve before production) |
| Architecture gaps | 5 | Medium (affects planning, not immediate implementation) |
| Duplication/overlap | 3 | Medium (causes confusion, not blockers) |
| Minor issues | 2 | Low |
| **Total** | **45** | |

### Recommended Priority for Resolution

1. **Fix immediately (before implementation):** #6–9 (schema reconciliation), #14–19 (interface contracts), #21 (error handling), #44 (project name)
2. **Fix before v0.2:** #22 (testing), #23 (config validation), #28 (human participant), #31–34 (security gaps), #36–37 (architecture/structure alignment)
3. **Fix before v0.3:** #25–26 (health checks, graceful shutdown), #38 (embedding infra), #39–40 (state persistence, dependency graph)
4. **Fix anytime:** #1–5 (numbering), #24 (schema versioning), #41–43 (dedup), #45 (license)
