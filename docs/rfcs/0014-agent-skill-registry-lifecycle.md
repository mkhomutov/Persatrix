# RFC 0014 — Agent Skill Registry & Lifecycle

**Type**: architecture  
**Status**: 📋 Proposed  
**Author**: Engineering Team  
**Date**: 2026-04-16  
**Target**: v0.2  
**Depends on**: RFC 0008, RFC 0009

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Skill Taxonomy](#a-skill-taxonomy)
  - [B. Skill Data Model](#b-skill-data-model)
  - [C. Skill Registry](#c-skill-registry)
  - [D. Skill Scoping — Least Capability Principle](#d-skill-scoping--least-capability-principle)
  - [E. Skill Validation — Pre- and Postcondition Checks](#e-skill-validation--pre--and-postcondition-checks)
  - [F. Skill Failure Handling](#f-skill-failure-handling)
  - [G. Skill Composition](#g-skill-composition)
  - [H. Skill Acquisition](#h-skill-acquisition)
  - [I. Skill Observability](#i-skill-observability)
  - [J. Domain Knowledge Skills](#j-domain-knowledge-skills)
  - [K. Meta-Skills](#k-meta-skills)
  - [L. Skills and Procedural Memory](#l-skills-and-procedural-memory)
  - [M. Skill Lifecycle Governance](#m-skill-lifecycle-governance)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

This RFC introduces a first-class **Skill Registry** for Persatrix: a centralized catalogue of agent capabilities spanning tool invocations, reasoning patterns, domain knowledge, procedural workflows, and meta-cognitive behaviors. Skills are versioned, health-tracked assets that the orchestrator consults when routing tasks and that agents use as the authoritative source of their own capability inventory. The registry enforces the least-capability principle at the skill layer, defines precondition/postcondition validation contracts, types failure modes with fallback chains, and establishes a deliberate lifecycle from authoring through deprecation.

## Motivation

Persatrix has a well-designed atomic tool layer (`agents/tools/`) and a sound permission model. Capabilities above that level — multi-step reasoning patterns, domain expertise, procedural workflows, and meta-cognitive behaviors — are currently implicit in agent configuration, system prompts, and code structure. They are not discoverable, not versioned, not monitored for health, and not composable as first-class objects.

**What exists today:**

1. Atomic tools are registered in `agents/tools/registry.py` and protected by `PermissionGate` with deny-by-default semantics (RFC 0004).
2. Agent roles are declared in `config/agents.yaml` with allowed tool categories and system prompts.
3. `TaskAgent` is data-driven from YAML config — its effective "capabilities" are the tools it can call and the system prompt it uses.
4. RFC 0009 defines `AgentCapabilityToken.Capabilities []string` as the enforcement point for capability claims, but there is no registry that defines what valid capability strings are, what they guarantee, or what their current health is.

**What is missing:**

1. **No skill discovery layer.** The orchestrator routes tasks to agents by agent name, not by capability. If a task requires domain-specific knowledge, the workflow author must know which agent has that capability — it is not queryable from the orchestrator.
2. **No capability versioning.** When a system prompt is updated or a tool API changes, there is no way to track which version of a skill an agent has, roll back to a prior version, or detect that a skill's behavior changed.
3. **No health tracking at the skill level.** RFC 0006 tracks execution cost and RFC 0009 tracks security events, but neither tracks whether a specific *capability* is succeeding or degrading over time.
4. **No typed failure modes.** When a skill fails, the agent returns a generic error. The orchestrator has no information about *why* it failed (timeout? auth failure? schema mismatch? capability gap?) and cannot respond with a targeted fallback.
5. **No lifecycle governance.** Skills enter the system via code changes and are never formally deprecated. This creates accumulating dead weight and an unboundedly growing security audit surface.
6. **`AgentCapabilityToken.Capabilities` lacks a backing registry.** RFC 0009 defines token enforcement but the capability strings it enumerates are arbitrary identifiers with no schema, I/O contract, or health record.

If left unchanged, agent capability management degrades with scale: routing becomes hardcoded, failures become opaque, and the security audit surface grows without a corresponding cleanup mechanism.

## Goals

1. Define a **`SkillSpec` data model** with name, description, version, input/output JSON Schema contracts, owning agent role, skill layer, and health status.
2. Implement a **`SkillRegistry`** (Python) as the canonical runtime catalogue of available skills, queryable by name, layer, role, and health status.
3. Expose a Go-side **`SkillCatalogue`** populated at agent registration so the orchestrator can perform skill-aware task routing without calling into Python.
4. Enforce the **least-capability principle** at the skill layer: agents are instantiated with only the skills declared in their config; sub-agents receive narrowed skill sets, not inherited parent skills; skills cannot be self-assigned.
5. Add **precondition and postcondition validation** to skill invocations: schema-check inputs before invocation, schema-check outputs after return.
6. Define **typed `SkillFailureMode` values** and **fallback chains** so the orchestrator can respond with precision rather than generic retry.
7. Define a **skill acquisition model** with three classes (authored, learned, delegated) and explicit promotion gates.
8. Add **per-skill observability metrics** (success rate, latency percentiles, invocation count) to the telemetry pipeline introduced in RFC 0006.
9. Define **domain knowledge skill conventions**: mandatory confidence fields, knowledge cutoff dating, and HITL escalation for high-stakes domains.
10. Add **meta-skill hooks** (`on_skill_uncertainty`, `on_skill_exhausted`, `on_output_self_assessment`, `on_subtask_routing`) to `BaseAgent`.
11. Establish a **skill lifecycle** (Design → Review → Register → Scope → Deploy → Monitor → Validate → Deprecate) with a formal gate at each stage.

## Non-Goals

- Replacing or modifying the existing `agents/tools/` atomic tool layer — skills sit above tools, not in place of them.
- Vector-based semantic skill matching or embedding-driven routing — Phase 1 uses config-declared skill assignments and direct name lookup.
- Cross-node skill federation or mesh-level skill discovery — that is v0.3 scope, after the distributed mesh RFC is written.
- LLM-generated skill code (auto-programming agents) — skills are human-authored or carefully promoted from observed runs with explicit review.
- Skill quality scoring beyond execution success/failure — output evaluation frameworks are future work.
- Replacing the RFC 0009 capability token model — this RFC extends it at the skill metadata layer.

---

## Design / Implementation

### A. Skill Taxonomy

Skills operate across six layers. The taxonomy is descriptive — it informs how skills are authored, validated, and monitored, not their mechanical implementation.

| Layer | Description | Examples |
|-------|-------------|---------|
| **Tool** | Invokes one or more atomic operations | `file_read`, `shell_exec`, `http_request` |
| **Reasoning** | Applies a structured thinking pattern | chain-of-thought, critique, problem decomposition |
| **Domain** | Encodes knowledge of a specific field | legal-contract-review, medical-symptom-triage |
| **Procedural** | Knows how to accomplish a task class step by step | code-review-workflow, incident-response-runbook |
| **Social** | Calibrates communication and manages clarification | tone-calibration, ambiguity-detection, user-modelling |
| **Meta** | Governs how an agent uses its other skills | stop-escalate, ask-clarify, delegate-subtask, self-assess |

Tool-layer skills already exist in `agents/tools/`. This RFC adds framework support for the five higher layers.

### B. Skill Data Model

A `Skill` is a structured descriptor. Skills are declared in YAML and loaded at agent startup.

```python
from dataclasses import dataclass, field
from enum import Enum

class SkillLayer(str, Enum):
    TOOL       = "tool"
    REASONING  = "reasoning"
    DOMAIN     = "domain"
    PROCEDURAL = "procedural"
    SOCIAL     = "social"
    META       = "meta"

class SkillHealth(str, Enum):
    HEALTHY    = "healthy"     # Success rate ≥ threshold; no alerts
    DEGRADED   = "degraded"    # Success rate declining; still routable
    UNHEALTHY  = "unhealthy"   # Below floor threshold; not routable
    UNKNOWN    = "unknown"     # No data yet (new or reset skill)
    DEPRECATED = "deprecated"  # Formally deprecated; not assignable to new agents

@dataclass
class SkillSpec:
    name: str                  # e.g. "code-review-workflow"
    version: str               # semver, e.g. "1.2.0"
    layer: SkillLayer
    description: str
    owner_role: str            # agent role that primarily owns this skill
    input_schema: dict         # JSON Schema for skill inputs
    output_schema: dict        # JSON Schema for skill outputs
    tags: list[str] = field(default_factory=list)
    fallback_chain: list[str] = field(default_factory=list)  # skill names, in order
    health: SkillHealth = SkillHealth.UNKNOWN
    deprecated_at: str | None = None
    superseded_by: str | None = None    # name of replacement skill
    high_stakes: bool = False           # always triggers HITL (domain skills)
    confidence_required: bool = False   # output must include confidence field
    knowledge_cutoff: str | None = None # ISO8601 date; domain skills only
    source: str = "authored"            # "authored" | "learned" | "delegated"
    reviewed_by: str | None = None      # required for source="learned"
    requires_capabilities: list[str] = field(default_factory=list)  # RFC 0009 token caps
```

Skills are declared in a new top-level config file `config/skills.yaml`:

```yaml
skills:
  - name: code-review-workflow
    version: "1.0.0"
    layer: procedural
    description: "Reviews code for correctness, style, and security issues."
    owner_role: reviewer
    input_schema:
      type: object
      required: [code, language]
      properties:
        code: { type: string }
        language: { type: string }
        context: { type: string }
    output_schema:
      type: object
      required: [findings, summary]
      properties:
        findings: { type: array, items: { type: object } }
        summary: { type: string }
        severity: { type: string, enum: [low, medium, high, critical] }
    fallback_chain: ["generic-code-review"]
    requires_capabilities: ["tool:file_read", "tool:shell_exec"]

  - name: gdpr-compliance-check
    version: "1.0.0"
    layer: domain
    description: "Reviews content for GDPR compliance issues."
    owner_role: legal-analyst
    high_stakes: true
    confidence_required: true
    knowledge_cutoff: "2026-01-01"
    input_schema: { ... }
    output_schema: { ... }
```

Agent YAML config gains a `skills:` field:

```yaml
# config/agents.yaml (excerpt)
agents:
  - id: code-reviewer
    role: reviewer
    skills:
      - code-review-workflow
      - chain-of-thought
      - self-assess-output
    permissions:
      allowed_tools: [file_read, shell_exec]
```

A JSON Schema (`schemas/skill.schema.json`) validates `config/skills.yaml`. The existing `make validate` target is extended to include this file.

### C. Skill Registry

The `SkillRegistry` is the authoritative runtime catalogue. It loads from `config/skills.yaml` at startup and exposes query and mutation APIs.

```python
class SkillRegistry:
    def register(self, spec: SkillSpec) -> None: ...
    def get(self, name: str, version: str | None = None) -> SkillSpec | None: ...
    def query(
        self,
        layer: SkillLayer | None = None,
        owner_role: str | None = None,
        health: SkillHealth | None = None,
        tag: str | None = None,
    ) -> list[SkillSpec]: ...
    def mark_health(self, name: str, health: SkillHealth) -> None: ...
    def get_fallback_chain(self, name: str) -> list[SkillSpec]: ...
    def deprecate(self, name: str, superseded_by: str | None = None) -> None: ...
    def is_chain_compatible(self, skill_a: SkillSpec, skill_b: SkillSpec) -> bool: ...
```

**Orchestrator-side skill catalogue (Go):**

The Go orchestrator holds a `SkillCatalogueEntry` per skill, derived from agent registration payloads. This allows skill-aware task routing without crossing the Go/Python process boundary.

```go
type SkillCatalogueEntry struct {
    Name      string
    Layer     string
    OwnerRole string
    Health    string
    Version   string
    AgentIDs  []string // agents currently declaring this skill
}

type SkillCatalogue interface {
    Lookup(skillName string) ([]SkillCatalogueEntry, error)
    RouteToSkill(skillName string) (agentID string, err error)
    UpdateHealth(skillName string, health string) error
}
```

The catalogue is populated when agents self-register via the existing `RegisterAgent` gRPC call, which is extended to include the agent's declared skill names. The orchestrator uses it to answer "which agent can execute skill X?" without routing blindly by agent name.

**Skill vs Tool boundary:**

The skill registry sits above the tool registry. A skill may invoke one or more tools; a tool is not a skill. Skills are the unit of capability routing and versioning; tools are the unit of atomic execution and permission control. The existing `PermissionGate` and `PathValidator` continue operating at the tool layer without modification.

### D. Skill Scoping — Least Capability Principle

Mirror the least-privilege principle from security (RFC 0009) at the skill layer:

1. **Static skill assignment**: An agent may only use skills declared in its `config/agents.yaml` `skills:` list. Skills outside that list are unavailable regardless of what the agent's LLM requests.

2. **No skill inheritance**: A sub-agent spawned by an orchestrator agent does not automatically inherit the parent's skills. It receives only the skills explicitly listed in its spawn config. RFC 0010 will define the spawn contract; this RFC defines the `SkillSpec` data model that contract uses for the skill list.

3. **Dynamic skill injection**: The orchestrator may grant a skill to an agent for a specific task execution, scoped to that task's correlation ID. The grant is recorded as a `SkillGrant` entry in the state store and automatically revoked when the task completes. This is a scope extension, not a config change — the skill must already exist in the registry.

4. **No self-assignment**: An agent cannot add skills to its own registry entry at runtime. Skill assignment is exclusively a config-plane or orchestrator-plane operation. Any attempt by an agent to call `register()` on skills not in its declared set triggers an `AuditCapabilityViolation` event (RFC 0009 audit log).

**Integration with RFC 0009 capability tokens:**

The `requires_capabilities` field in `SkillSpec` maps directly to `AgentCapabilityToken.Capabilities`. When the orchestrator validates a token and routes a skill invocation, it verifies that the agent's token includes all capabilities listed in `skill.requires_capabilities`. If not, the invocation is rejected and logged. This extends RFC 0009's token model without replacing it.

### E. Skill Validation — Pre- and Postcondition Checks

Before and after a skill is invoked, the agent validates it:

```python
@dataclass
class SkillValidationResult:
    valid: bool
    failure_mode: SkillFailureMode | None = None
    detail: str | None = None

class SkillValidator:
    def check_preconditions(
        self,
        skill: SkillSpec,
        inputs: dict,
        agent_capabilities: list[str],
    ) -> SkillValidationResult:
        """
        1. JSON Schema-validate inputs against skill.input_schema.
        2. Verify skill.health is not UNHEALTHY or DEPRECATED.
        3. Verify all skill.requires_capabilities are present in agent_capabilities.
        """

    def check_postconditions(
        self,
        skill: SkillSpec,
        outputs: dict,
    ) -> SkillValidationResult:
        """
        1. JSON Schema-validate outputs against skill.output_schema.
        2. If skill.confidence_required, verify outputs contain a "confidence" field (float 0.0–1.0).
        """

    def check_availability(
        self,
        skill: SkillSpec,
    ) -> SkillValidationResult:
        """
        For skills that declare external service dependencies, perform a lightweight
        reachability check. Result is cached per agent session to avoid N redundant probes.
        """
```

A skill that completes without raising an exception but returns malformed output is a silent failure. Postcondition validation catches this and produces a `SkillFailureMode.SCHEMA_MISMATCH` rather than propagating bad output upstream.

### F. Skill Failure Handling

Typed failure modes allow targeted orchestrator responses:

```python
class SkillFailureMode(str, Enum):
    TIMEOUT           = "timeout"
    AUTH_FAILURE      = "auth_failure"
    SCHEMA_MISMATCH   = "schema_mismatch"  # bad input (pre) or bad output (post)
    RATE_LIMITED      = "rate_limited"
    CAPABILITY_GAP    = "capability_gap"   # agent lacks a required capability
    DEPENDENCY_DOWN   = "dependency_down"  # external service unreachable
    BUDGET_EXCEEDED   = "budget_exceeded"  # RFC 0006 budget exhausted
    CONFIDENCE_LOW    = "confidence_low"   # output confidence below threshold
    UNKNOWN           = "unknown"
```

**Failure behavior:**

1. When a skill fails, the agent emits a structured `SkillFailureEvent` with the typed `SkillFailureMode`. It does not attempt a workaround using unrelated tools.
2. The orchestrator reads the failure mode and consults the skill's `fallback_chain` in the `SkillCatalogue`.
3. Fallback chain traversal:
   - The orchestrator tries each skill in `fallback_chain` in order, routing to the appropriate agent.
   - If all fallbacks fail, the step fails with `SKILL_EXHAUSTED` and the failure is surfaced in step results (not silently swallowed).
   - `CAPABILITY_GAP` short-circuits fallback traversal immediately — trying alternative skills without the required capability will also fail.
4. Human escalation: if the fallback chain is exhausted and the skill is `high_stakes: true`, a HITL gate (RFC 0009 Section F) is opened for operator review.

**Relationship to RFC 0003 retry logic:**

RFC 0003's `GRPCExecutor` retry operates at the transport level (transient gRPC errors). Skill-level failure handling fires after the gRPC call succeeds but the skill result is invalid or the skill explicitly signals failure. The two layers are complementary and non-overlapping.

### G. Skill Composition

Individual skills are designed to compose:

1. **Single responsibility**: each skill does one thing well. A skill that retrieves, summarises, and formats is three skills bundled badly. The `layer` field helps enforce this — a skill that legitimately spans layers is a composition candidate, not a monolithic skill.

2. **Standardized I/O contracts**: `input_schema` and `output_schema` (JSON Schema) enable chaining without glue code. When Skill A's `output_schema` satisfies Skill B's `input_schema` requirements, they compose. `SkillRegistry.is_chain_compatible(skill_a, skill_b)` evaluates this formally.

3. **Skill pipelines as first-class objects**: A sequential skill composition can be declared in `config/skills.yaml`:

```yaml
  - name: code-review-pipeline
    layer: procedural
    pipeline:
      - step: parse-structure
        skill: code-parser
      - step: check-style
        skill: style-checker
        depends_on: [parse-structure]
      - step: security-scan
        skill: security-scanner
        depends_on: [parse-structure]
      - step: synthesize-findings
        skill: findings-synthesizer
        depends_on: [check-style, security-scan]
```

Pipeline skills are orchestrated by the executing agent, not by the workflow scheduler. The scheduler dispatches a single step to one agent; the pipeline decomposition is internal to that agent. This keeps workflow YAML semantics consistent.

4. **Schema compatibility check**: `SkillRegistry.is_chain_compatible(a, b)` returns `True` if `a.output_schema` properties are a superset of `b.input_schema.required` fields.

### H. Skill Acquisition

Three acquisition classes with explicit promotion gates:

**1. Authored skills** (highest trust):
- Hand-crafted by the team: prompts, tool integrations, reasoning patterns.
- Declared in `config/skills.yaml` with complete metadata; subject to RFC and code review.
- Health starts at `UNKNOWN`; transitions to `HEALTHY` once observability data confirms performance.

**2. Learned skills** (medium trust):
- Patterns extracted from successful past runs and codified as reusable skills.
- Require explicit human review (`reviewed_by` field) before registry promotion. A learned skill without `reviewed_by` is rejected by the registry at load time.
- Lifecycle: Candidate → Reviewed → Registered. Any change to the source data resets `reviewed_by` to `None`, blocking redeployment until re-reviewed.
- Phase 1 reserves the `source: "learned"` path in the data model without implementing the extraction pipeline. The mechanism is ready for a future implementation phase.

**3. Delegated skills** (session trust):
- Temporarily granted by the orchestrator for a specific task, scoped to the task's correlation ID.
- Implemented as `SkillGrant` records in the state store: created at grant time, looked up during execution, revoked automatically on task completion or timeout.
- Delegation is a scope extension only — the skill must already exist in the registry. Delegation cannot create new skills.
- All grants are written to the RFC 0009 audit trail.

### I. Skill Observability

Each skill has its own monitoring layer, distinct from RFC 0006's execution cost observability.

**Metrics emitted as structured telemetry events:**

| Metric | Description |
|--------|-------------|
| `skill_invocations_total` | Counter: by skill name, agent ID, outcome |
| `skill_success_rate` | Gauge: rolling success rate (default: 5-min window) |
| `skill_latency_p50_ms` | Histogram: median invocation latency |
| `skill_latency_p95_ms` | Histogram: 95th percentile invocation latency |
| `skill_failure_mode_count` | Counter: by `SkillFailureMode` |
| `skill_fallback_triggered_total` | Counter: fallback chain traversals |
| `skill_health_transitions_total` | Counter: health status changes |
| `skill_knowledge_stale` | Event: emitted when `knowledge_cutoff` > 90 days old |

**Health status derivation (defaults, configurable per skill):**

```
HEALTHY    → success_rate ≥ 0.95 AND p95_latency < 2× baseline
DEGRADED   → success_rate ∈ [0.80, 0.95) OR p95_latency ∈ [2×, 5×] baseline
UNHEALTHY  → success_rate < 0.80 OR p95_latency > 5× baseline
```

When a skill enters `UNHEALTHY`, the orchestrator stops routing to it and the `SkillCatalogue.RouteToSkill` call for that skill returns the first healthy fallback instead.

**Skill-level replay:**

Any skill invocation can be replayed with its original inputs:

```
GET /api/v1/skills/{name}/replay
Body: { "invocation_id": "...", "correlation_id": "..." }
```

The orchestrator retrieves the original input payload from step state metadata and re-dispatches to the owning agent with identical inputs. Replay invocations are tagged `source: "replay"` in the audit log.

**Relationship to RFC 0006 observability:**

RFC 0006 tracks execution cost (tokens, LLM calls, budget consumption) at the task level. RFC 0014 tracks quality and reliability at the skill level. Cost data tells you how much a skill costs; skill metrics tell you whether it is working. Both feed the same telemetry pipeline.

### J. Domain Knowledge Skills

Skills encoding domain knowledge (legal, medical, financial) require additional conventions:

1. **Mandatory `confidence_required: true`**: Domain skills must return a `confidence: float` field in their output. A skill that returns without this field fails postcondition validation (`SkillFailureMode.SCHEMA_MISMATCH`), regardless of whether the LLM produced an otherwise valid response.

2. **Knowledge cutoff dating**: Domain skills declare `knowledge_cutoff` (ISO8601 date). The registry emits a `skill_knowledge_stale` event when the cutoff is more than 90 days old. This is a warning, not a block — the skill remains routable, but operators are alerted to schedule a knowledge review.

3. **Mandatory HITL for `high_stakes: true`**: Domain skills with `high_stakes: true` always open a HITL gate (RFC 0009 Section F) before results are returned upstream. The gate presents the output, confidence value, and knowledge sources to the operator. This gate is not overridable by the `hitl_enabled: false` config flag — that flag controls interactive HITL for tool actions, not domain safety gates.

4. **Separation of reasoning and action**: A domain skill produces a recommendation; a separate step (with its own HITL gate if needed) executes any consequent action. A skill that both reasons about a domain and acts on that reasoning must be split.

### K. Meta-Skills

Meta-skills govern how an agent uses its other skills. They are implemented as hooks in `agents/base.py` with overridable default implementations.

```python
class MetaSkillDecision(str, Enum):
    PROCEED           = "proceed"
    ASK_CLARIFICATION = "ask_clarification"
    DELEGATE          = "delegate"
    ESCALATE          = "escalate"
    STOP              = "stop"

class BaseAgent(ABC):
    # ... existing interface ...

    async def on_skill_uncertainty(
        self,
        skill: SkillSpec,
        inputs: dict,
        context: TaskContext,
    ) -> MetaSkillDecision:
        """
        Called when task inputs are ambiguous or the specification is underspecified.
        Default for BaseAgent: ESCALATE (safe for non-interactive task agents).
        PersonaAgent overrides to: ASK_CLARIFICATION.
        """

    async def on_skill_exhausted(
        self,
        skill: SkillSpec,
        failure: SkillFailureEvent,
        context: TaskContext,
    ) -> MetaSkillDecision:
        """
        Called when a skill and its entire fallback chain have failed.
        Default: ESCALATE (fail the task with a structured error).
        """

    async def on_output_self_assessment(
        self,
        skill: SkillSpec,
        outputs: dict,
        context: TaskContext,
    ) -> SelfAssessmentResult:
        """
        Called after skill execution. Agent evaluates its own output quality
        before returning results upstream.
        Default: no-op (SelfAssessmentResult.PASS without evaluation).
        Agents that override this can add an LLM-powered review step.
        """

    async def on_subtask_routing(
        self,
        subtask: str,
        required_skill: str,
        context: TaskContext,
    ) -> MetaSkillDecision:
        """
        Called when the agent identifies a subtask that needs a skill it does not have.
        Default: DELEGATE (request routing to an agent with the required skill).
        """
```

Meta-skill decisions are logged as structured events so the orchestrator can observe agent decision patterns over time. The default for `BaseAgent` is `ESCALATE` for uncertainty (safe for non-interactive task agents); `PersonaAgent` overrides uncertainty to `ASK_CLARIFICATION` (consistent with its interactive behavior model).

### L. Skills and Procedural Memory

Skills and procedural memory are closely coupled. This section builds on RFC 0008 Section B (`MemoryFacade`) and Section G (procedural memory decay):

1. **Procedural memory is where skills live long-term.** Successful skill invocation patterns, known failure modes, and learned workarounds are written to agent-scoped procedural memory via RFC 0008's `MemoryFacade.store_procedure(key, content, confidence, expires_at)`. The skill name is the primary key so procedures are retrievable by skill.

2. **Minimal skill context.** When invoking a skill, the agent injects only the context relevant to that skill's `input_schema` fields (using RFC 0008's context packaging pipeline). The `input_schema` required fields serve as the query vector when RFC 0008's relevance scorer selects which prior context to include. The full conversation history is not injected.

3. **Skill output compression before returning upstream.** Skill output is schema-conformant and concise by design. Before a result is returned to the orchestrator, it passes through the RFC 0008 compression pipeline: only fields required by `output_schema` are included in the result envelope; raw intermediate LLM outputs are stored in episodic memory.

4. **Procedural memory decay applies to skill records.** RFC 0008's decay model ($c_t = c_0 \cdot e^{-\lambda t}$) applies to skill procedure entries. A skill whose procedural record has decayed below $c_{min}$ is flagged for revalidation before injection. Stale procedural knowledge is a leading indicator of `DEGRADED` skill health — the metrics from Section I and the memory confidence signal should be correlated in observability tooling.

### M. Skill Lifecycle Governance

Every skill moves through a deliberate lifecycle:

```
Design → Review → Register → Scope → Deploy
                                         ↓
                              Monitor → Validate → Deprecate
                                         ↑
                                    Update (version bump)
```

| Stage | Entry Gate | Exit Gate |
|-------|-----------|----------|
| Design | Idea or extraction candidate identified | Full `SkillSpec` draft with I/O schemas |
| Review | PR/RFC submission with complete spec | Code review approval; security review for `high_stakes: true` |
| Register | Review approval | Entry in `config/skills.yaml`; health = `UNKNOWN` |
| Scope | Agent config assignment | `config/agents.yaml` updated; `make validate` passes |
| Deploy | Agent restart with new config | First invocations observed; monitoring begins |
| Monitor | Invocations begin | ≥ 50 invocations; health transitions out of `UNKNOWN` |
| Validate | Version bump OR 90 days elapsed | Health confirmed `HEALTHY`; knowledge cutoff refreshed |
| Deprecate | Successor skill deployed and `HEALTHY` | `deprecated_at` set; skill removed from agent configs |
| Update | Bug fix or improvement needed | Version bumped; prior version retained with 30-day rollback window |

Governance rules enforced by the registry:

- `register()` rejects specs missing required fields.
- Deprecated skills (`health = DEPRECATED`) cannot be assigned to agents — `make validate` fails on agent configs that reference them.
- Learned skills without `reviewed_by` are rejected at load time.
- A `skill_stale_review` event is emitted when a skill has not been through the Validate stage in 90 days.

---

## Security Considerations

1. **Skill self-assignment**: An agent that could write to `config/skills.yaml` or call `SkillRegistry.register()` from a task handler could grant itself capabilities. Mitigation: the registry is loaded read-only at agent startup; `register()` calls from agent task contexts are rejected, triggering `AuditCapabilityViolation`.

2. **Fallback chain privilege escalation**: A fallback chain could inadvertently route a failed task to a more privileged skill or agent. Mitigation: fallback chain entries must reference skills with the same or a subset of `requires_capabilities` as the primary skill. The registry validates this at load time.

3. **Learned skill poisoning**: Patterns extracted from runs that contained errors or adversarial inputs could encode vulnerabilities. Mitigation: learned skills require explicit human review before promotion (Section H). The `reviewed_by` requirement is enforced at load time, not just at authoring time.

4. **Domain skill confidence manipulation**: An agent could return an artificially high `confidence` value to suppress human review. Mitigation: `high_stakes: true` skills always open a HITL gate regardless of the confidence value. Confidence is informational; it is not a gate bypass mechanism.

5. **Skill versioning rollback re-exposure**: Rolling back to a prior skill version might re-introduce a known vulnerability. Mitigation: all version rollbacks emit a `skill_version_rollback` audit event (RFC 0009) for security team review. Rollback requires an explicit version pin in agent config — not automatic.

6. **Availability probes as information leakage**: Precondition availability checks (Section E) ping external services. If probe responses include version headers or internal details, they could assist an attacker. Mitigation: availability checks use health-endpoint conventions (HTTP 200/503 only); full probe responses are discarded.

7. **Skill grant escalation via state store manipulation**: A `SkillGrant` record in the state store that persists past task completion could be replayed. Mitigation: `SkillGrant` records are keyed by `correlation_id` and are revoked — not just expired — at task completion; revoked grants are rejected even if replayed with the same correlation ID.

---

## Phased Implementation Plan

### Phase 1: Skill Data Model & Registry Infrastructure

Summary: Define `SkillSpec`, implement `SkillRegistry`, add `config/skills.yaml`, extend agent registration to include skill names, and expose `SkillCatalogue` in the orchestrator for skill-aware routing.

Deliverables:

1. `agents/skills/` package: `SkillSpec`, `SkillLayer`, `SkillHealth`, `SkillRegistry`.
2. `config/skills.yaml` with initial skill declarations for existing agent roles (code-writer, code-reviewer, planner, persona agents).
3. `schemas/skill.schema.json` — JSON Schema for `config/skills.yaml`.
4. Extend `config/agents.yaml` schema to include `skills: list[str]` per agent.
5. `internal/registry/skill_catalogue.go` — `SkillCatalogue` interface and `InMemorySkillCatalogue` implementation.
6. Extend `RegisterAgent` payload to include declared skill names (JSON in existing `metadata` map under reserved key `"_skills"` — no proto change; see Open Question 1).
7. Extend `make validate` to check `config/skills.yaml` against `schemas/skill.schema.json`.
8. Unit tests: registry CRUD, query filtering, health transitions, chain compatibility.

Dependencies: None (foundational).

### Phase 2: Skill Validation & Failure Handling

Summary: Add precondition/postcondition schema validation to skill invocations and wire typed failure modes with fallback chain traversal in the orchestrator.

Deliverables:

1. `agents/skills/validators.py` — `SkillValidator` with pre/postcondition checks, `SkillValidationResult`, `SkillFailureMode`.
2. `SkillFailureEvent` dataclass emitted by agents on skill failure.
3. `SkillFailureHandler` in Go executor — reads failure mode from step result, consults `SkillCatalogue`, traverses fallback chain, short-circuits on `CAPABILITY_GAP`.
4. Wire `SkillValidator.check_preconditions()` in agent task handler before skill dispatch.
5. Wire `SkillValidator.check_postconditions()` in agent task handler after skill returns.
6. Unit tests: schema validation pass/fail cases per `SkillFailureMode`; fallback chain traversal; `CAPABILITY_GAP` short-circuit.

Dependencies: Phase 1.

### Phase 3: Skill Acquisition & Lifecycle Governance

Summary: Add versioning with rollback, `SkillGrant` records for dynamic injection, deprecation enforcement, and the learned skill promotion gate.

Deliverables:

1. Versioning in `SkillRegistry` — `get(name, version)`, retain prior versions on update with 30-day rollback window.
2. `SkillGrant` record in state store — creation, correlation-ID scoping, automatic revocation on task completion.
3. Deprecation enforcement: `registry.deprecate()` sets `health = DEPRECATED`; `make validate` rejects agent configs referencing deprecated skills.
4. Learned skill gate: `source: "learned"` specs without `reviewed_by` rejected at load time.
5. Skill version rollback audit event emission via RFC 0009 `AuditLogger`.
6. `skill_stale_review` event emission for skills not validated in 90 days.
7. Unit tests: version pinning, grant lifecycle, deprecation rejection, rollback audit event.

Dependencies: Phase 1, RFC 0009 Phase 1 (audit logging).

### Phase 4: Skill Observability & Meta-Skills

Summary: Emit per-skill telemetry, implement health auto-derivation from rolling metrics, add meta-skill hooks to `BaseAgent`, implement skill replay endpoint, and integrate with RFC 0008 procedural memory.

Deliverables:

1. `agents/skills/observability.py` — all metrics from Section I as structured telemetry events.
2. Health auto-derivation in `SkillRegistry` from rolling metrics; configurable thresholds per skill.
3. `knowledge_cutoff` staleness check and `skill_knowledge_stale` event emission.
4. Meta-skill hooks in `agents/base.py`: `on_skill_uncertainty`, `on_skill_exhausted`, `on_output_self_assessment`, `on_subtask_routing`. `PersonaAgent` overrides `on_skill_uncertainty` to `ASK_CLARIFICATION`.
5. Skill replay REST endpoint (`GET /api/v1/skills/{name}/replay`) in `internal/server/`.
6. Domain skill postcondition confidence field enforcement.
7. Skill procedure storage via RFC 0008 `MemoryFacade.store_procedure()` on successful invocations.
8. Unit tests and integration tests for all observability paths, meta-skill default/override behavior.

Dependencies: Phase 2, RFC 0006 (telemetry pipeline), RFC 0008 Phase 2 (MemoryFacade).

---

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | `agents/skills/__init__.py` (new) | Package root |
| Python agents | `agents/skills/registry.py` (new) | SkillRegistry, SkillSpec, SkillLayer, SkillHealth |
| Python agents | `agents/skills/validators.py` (new) | SkillValidator, SkillValidationResult, SkillFailureMode |
| Python agents | `agents/skills/lifecycle.py` (new) | SkillGrant model, acquisition class helpers |
| Python agents | `agents/skills/observability.py` (new) | Per-skill metrics emission (Phase 4) |
| Python agents | `agents/base.py` | Meta-skill hook stubs (Phase 4) |
| Python agents | `agents/persona_runtime.py` | Meta-skill hook override for ASK_CLARIFICATION (Phase 4) |
| Python agents | `agents/task_agent.py` | Skill validation wiring (Phase 2) |
| Python agents | `agents/server.py` | Skill names in RegisterAgent payload (Phase 1) |
| Go orchestrator | `internal/registry/skill_catalogue.go` (new) | SkillCatalogue, InMemorySkillCatalogue (Phase 1) |
| Go orchestrator | `internal/executor/executor.go` | Skill failure mode handling, fallback chain traversal (Phase 2) |
| Go orchestrator | `internal/state/state.go` | SkillGrant record (Phase 3) |
| Go orchestrator | `internal/server/server.go` | Skill replay endpoint (Phase 4) |
| Config | `config/skills.yaml` (new) | Skill declarations |
| Config | `config/agents.yaml` | `skills:` list per agent |
| Schemas | `schemas/skill.schema.json` (new) | JSON Schema for config/skills.yaml |
| Schemas | `schemas/agent.schema.json` | `skills` array field addition |
| Tests | `tests/unit/python/test_skill_registry.py` (new) | Registry CRUD, query, health transitions |
| Tests | `tests/unit/python/test_skill_validators.py` (new) | Pre/postcondition, failure modes |
| Tests | `tests/unit/go/skill_catalogue_test.go` (new) | Catalogue lookup and routing |
| Tests | `tests/integration/test_skill_routing.py` (new) | End-to-end skill-aware dispatch |

## Test Strategy

- **Unit tests (Python)**: `SkillRegistry` CRUD, query filtering, and health transitions; `SkillValidator` precondition/postcondition schema pass/fail; `SkillFailureMode` enumeration coverage; meta-skill hook default and override behavior; `SkillGrant` creation and revocation.
- **Unit tests (Go)**: `SkillCatalogue` lookup and `RouteToSkill` fallback; skill failure mode handling in executor; fallback chain traversal with `CAPABILITY_GAP` short-circuit; `SkillGrant` state store lifecycle.
- **Integration tests**: agent registers with skill names → orchestrator `SkillCatalogue` updated → task routed by skill name; skill postcondition failure → fallback chain invoked → escalation audit logged; `SkillGrant` created, scoped to task, revoked on completion.
- **Validation tests**: `make validate` accepts well-formed `config/skills.yaml`; rejects missing required fields, unknown skill references in agent config, and deprecated skill assignments.
- **Observability tests**: telemetry events emitted with correct fields on success, failure, and fallback; health auto-derivation transitions at configured thresholds; `skill_knowledge_stale` fires when cutoff is overdue.

## Open Questions

1. **Proto change scope for skill names in `RegisterAgent`**: Should skill names be added as `repeated string skill_names` in `RegisterAgentRequest`, or passed as a JSON payload in the existing `metadata` map under a reserved key `"_skills"`? **Proposed default**: use `metadata["_skills"]` in Phase 1 (no proto change; consistent with RFC 0008 Open Question 2 resolution — typed fields deferred until schema is proven). Typed proto field added in the same revision as RFC 0008's context package fields (Phase 3 of both RFCs).

2. **Skill pipeline orchestration boundary**: Section G defines skill pipelines as agent-internal (not visible to the workflow scheduler). Should some pipelines eventually be externally orchestrated as workflow-level DAG steps? **Proposed default**: agent-internal for v0.2, consistent with the component boundary that agents own execution logic. External skill pipeline scheduling — where the orchestrator drives each step independently — is a v0.3 concern once distributed routing makes step-level granularity more valuable.

3. **Rollback version audit requirement**: Should all version rollbacks require an explicit security review approval before deployment, not just an audit event? **Proposed default**: audit-and-alert (Phase 3 approach) is sufficient for v0.2's single-operator model. A mandatory approval gate is appropriate for v0.3+ multi-operator deployments and belongs in a future compliance RFC.

4. **Learned skill extraction pipeline**: Phase 1 reserves `source: "learned"` in the data model but defers the extraction mechanism. When should this be designed — as a follow-on RFC, or expanded into a later phase of this RFC? **Proposed default**: follow-on RFC, scoped to v0.3 when multi-run pattern mining across distributed nodes becomes feasible. Keeping it separate avoids expanding RFC 0014's scope before Phase 1 is validated.

5. **Meta-skill defaults for `BaseAgent` vs `PersonaAgent`**: Section K proposes `BaseAgent` defaults to `ESCALATE` for `on_skill_uncertainty`, with `PersonaAgent` overriding to `ASK_CLARIFICATION`. Is `ESCALATE` always the right default for task agents, or should some task agents (e.g., `planner`) also ask for clarification? **Proposed default**: `ESCALATE` for `BaseAgent` (safe for automated pipelines where no user is present). Agents that benefit from clarification (including task-agent roles that run in interactive workflows) override the hook explicitly. The override is lightweight — one method, one return value.

## Decision / Next Steps

This RFC proposes a first-class Skill Registry as the missing capability-management layer between the tool permission model (RFC 0009) and the orchestrator's task routing model. The design extends rather than replaces existing components.

Next steps:

1. Resolve Open Questions 1 (proto scope), 2 (pipeline boundary), and 5 (meta-skill defaults) before Phase 1 implementation begins.
2. Create a PR plan file for RFC 0014 after acceptance.
3. Phase 1 can begin concurrently with RFC 0008 Phase 1 — no hard dependency, and Phase 1 of this RFC touches disjoint files.
4. Phase 3 requires RFC 0009 Phase 1 (audit logging) to be merged first.
5. Phase 4 requires RFC 0006 telemetry pipeline and RFC 0008 Phase 2 (`MemoryFacade`) to be available.

## Related Documentation

- [RFC 0004](0004-python-agent-grpc-server.md) — Tool layer this RFC sits above
- [RFC 0006](0006-efficiency-execution-limits.md) — Telemetry pipeline skill observability integrates with
- [RFC 0008](0008-agent-memory-context-optimization.md) — Procedural memory and context packaging this RFC depends on
- [RFC 0009](0009-security-sandboxing.md) — Capability tokens, audit logging, and HITL gates this RFC extends
- RFC 0010 (Sub-Agent Spawning, not yet written) — Will use `SkillCatalogue` for routing spawned agents
- [Roadmap](../../ROADMAP.md)
- [Extension Spec](../persatrix-extension-spec.md)
