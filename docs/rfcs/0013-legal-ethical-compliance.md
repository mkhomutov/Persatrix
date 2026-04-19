# RFC 0013 — Legal, Ethical & Regulatory Compliance Framework

**Type**: architecture  
**Status**: 📋 Proposed  
**Author**: Maksim Khomutov  
**Date**: 2026-04-16  
**Target**: v0.5.0  
**Depends on**: RFC 0009

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Action Risk Classification Taxonomy](#a-action-risk-classification-taxonomy)
  - [B. Data Privacy & Minimisation](#b-data-privacy--minimisation)
  - [C. Right to Erasure & Memory Compliance](#c-right-to-erasure--memory-compliance)
  - [D. Consent Tracking & Action Authorization](#d-consent-tracking--action-authorization)
  - [E. Ethical Guardrails at the Orchestrator Level](#e-ethical-guardrails-at-the-orchestrator-level)
  - [F. Regulatory Audit Infrastructure](#f-regulatory-audit-infrastructure)
  - [G. Transparency & Explainability](#g-transparency--explainability)
  - [H. Intellectual Property Safeguards](#h-intellectual-property-safeguards)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

This RFC establishes the legal, ethical, and regulatory compliance framework for Persatrix. It defines an action risk classification taxonomy, data privacy controls across agent memory tiers, right-to-erasure support, consent tracking for autonomous agent actions, orchestrator-level ethical guardrails, and regulatory audit infrastructure. This RFC builds on RFC 0009's technical security layer (audit logging, HITL gates, capability tokens) and adds the compliance dimension required before agents interact with real users or process personal data.

## Motivation

Persatrix agents are extensions of the operator's legal and ethical obligations. Every action an agent takes — storing data in memory, sending an HTTP request, executing a shell command, spawning a sub-agent — creates potential legal exposure. This exposure grows with each v0.2 capability:

**Why compliance must be addressed now:**

1. **Agent memory persists personal data.** Episodic memory (`agents/memory/episodic.py`), relationship memory (`agents/memory/relationship.py`), and notes (`agents/memory/notes.py`) all write to SQLite stores that may contain personally identifiable information (PII). Under GDPR Article 17, CCPA §1798.105, and similar regulations, data subjects have the right to erasure — this obligation cascades to every memory tier an agent has written to. Today, there is no mechanism to identify, locate, or delete data by subject identity across memory stores.

2. **Autonomous agents take real-world actions.** Persona agents with `autonomy.level: "semi-autonomous"` or `"autonomous"` act via tick loops without per-action human approval. The `shell_exec`, `http_request`, and `file_write` tools can trigger irreversible external effects. A blanket "I agree to terms" at configuration time does not constitute valid consent for each class of action — regulators and enterprise customers require auditable per-action-class authorization.

3. **No formal risk taxonomy exists.** RFC 0009 introduces HITL gates for actions tagged `irreversible: true`, but there is no systematic classification of which actions carry which risk level. Without a formal taxonomy, operators cannot demonstrate to auditors or regulators that appropriate controls are applied proportionally.

4. **Ethical guardrails are model-dependent.** Current ethical constraints rely entirely on LLM provider guardrails, which can be bypassed through prompt injection or jailbreaking. The orchestrator has no independent ethical refusal layer. This is unacceptable for agents that affect people (hiring assistance, content moderation, customer service).

5. **Enterprise adoption requires compliance posture.** Organizations evaluating agent orchestration systems require demonstrable data governance, audit trails, and consent mechanisms before deployment. Building these retroactively is significantly more expensive and error-prone than designing them in.

**What RFC 0009 provides (that this RFC builds on):**

- `AuditLogger` — immutable, correlation-ID-linked audit trail (RFC 0009 §G)
- Human-in-the-Loop gates — approval mechanism for irreversible actions (RFC 0009 §F)
- `InputSanitizer` — external data provenance tagging (RFC 0009 §C)
- Capability tokens — agent identity and permission scoping (RFC 0009 §B)

**What RFC 0009 does not cover (that this RFC adds):**

- Data classification and PII detection in agent memory
- Right to erasure across memory tiers
- Consent tracking per action class per subject
- Action risk classification taxonomy
- Orchestrator-level ethical refusal rules (independent of LLM)
- Regulatory retention policies and compliance-grade audit retrieval
- Plain-language action summaries for subject access requests
- IP provenance tracking for agent-generated content

## Goals

1. Define a four-tier action risk classification taxonomy (Critical, High, Medium, Low) integrated into the tool system and enforced by the orchestrator.
2. Implement data classification annotations for agent memory operations, distinguishing PII-bearing writes from non-sensitive ones.
3. Add per-subject data erasure capability across all three memory tiers (episodic, relationship, notes) with verification and audit logging.
4. Introduce a consent tracking system that records per-action-class authorizations per subject, enforced at tool invocation time.
5. Implement orchestrator-level ethical guardrails that block prohibited action classes independently of LLM provider constraints.
6. Extend RFC 0009's `AuditLogger` with compliance-specific features: retention policies, per-subject audit retrieval, and plain-language action summaries.
7. Add IP provenance tracking for agent-generated content, recording which model, tools, and data sources contributed to each output.

## Non-Goals

- **Implementing specific regulatory certifications** (SOC2, ISO 27001, HIPAA). This RFC establishes the architectural primitives; certification processes are operational matters.
- **Content filtering or hallucination detection.** Output quality is distinct from compliance. This RFC covers what actions are permitted and how they are tracked, not whether LLM output is accurate.
- **Multi-tenant data isolation.** Persatrix is single-tenant for v0.2. Multi-tenant compliance is a v0.3+ concern.
- **Cross-border data transfer mechanisms.** Standard Contractual Clauses and data localization are deployment-time configuration, not framework architecture. This RFC ensures the primitives exist (data location awareness, transfer logging) but does not implement jurisdictional logic.
- **Automated bias detection in LLM outputs.** This RFC establishes the evaluation framework and hooks; building a production bias detection model is out of scope.
- **Legal review of specific jurisdictions.** This RFC provides the technical architecture; legal review of applicability to specific deployments is an operator responsibility.

---

## Design / Implementation

### A. Action Risk Classification Taxonomy

Every tool action in Persatrix is assigned a risk level that determines the required control gates:

| Risk Level | Definition | Examples | Required Control |
|---|---|---|---|
| **Critical** | Irreversible external effects with financial, legal, or safety implications | Financial transactions, legal document filing, medical advice generation, employment decisions | Human approval gate + structured audit log + consent verification |
| **High** | External communication or modification of user-attributed data | `http_request` (POST/PUT/DELETE), `shell_exec` (network commands), `file_write` (to paths outside workspace), sending messages via bridges | Confirmation step + structured audit log |
| **Medium** | Reading sensitive data, generating persistent artifacts | `file_read` (sensitive paths), `memory_store` with PII flag, report generation | Capability gate + audit log |
| **Low** | Internal reasoning, summarization, planning, workspace-scoped operations | `file_read` (workspace), `file_write` (workspace), internal memory operations, context assembly | Standard trace log |

**Integration with the tool system:**

The `@tool` decorator in `agents/tools/registry.py` gains a `risk_level` parameter:

```python
@tool(
    name="http_request",
    permissions=["network"],
    risk_level="high",  # NEW: determines control gates
)
async def http_request(url: str, method: str = "GET", ...) -> str: ...
```

Risk levels are validated against `config/agents.yaml` at agent load time. Operators can override the default risk level per agent (downgrade only — upgrading risk is not permitted):

```yaml
permissions:
  risk_overrides:
    http_request: "critical"  # operator escalates to require human approval
```

**Orchestrator enforcement:**

The orchestrator's security layer (RFC 0009 `PermissionGate` + this RFC) checks the tool's effective risk level before dispatch:

- **Critical**: Route through HITL gate (RFC 0009 §F). Block if no human approver is configured.
- **High**: Require explicit agent capability token claim (`tool:<name>:high`). Log structured audit entry.
- **Medium**: Standard capability check. Log audit entry with data classification tag.
- **Low**: Proceed with standard trace logging.

### B. Data Privacy & Minimisation

**Data classification annotations:**

Memory operations are annotated with a data classification level:

```python
class DataClassification(str, Enum):
    PUBLIC = "public"           # No restrictions
    INTERNAL = "internal"       # Organization-internal, no PII
    SENSITIVE = "sensitive"     # May contain PII or confidential data
    RESTRICTED = "restricted"   # Confirmed PII, regulated data
```

The memory tools (`memory_store`, `note_create`, `episode_save`) accept an optional `classification` parameter. If omitted, the default is `INTERNAL`. Agents processing external user data must tag writes as `SENSITIVE` or `RESTRICTED`.

**Minimisation enforcement:**

Context assembly (`agents/memory/working.py`) respects classification levels:

1. `RESTRICTED` data is only included in context when the agent's capability token includes `data:restricted`.
2. `SENSITIVE` data is included only for agents with `data:sensitive` or higher.
3. When context exceeds budget, lower-classification items are evicted first (all else being equal).
4. Ephemeral working memory is never persisted beyond the task lifetime unless explicitly promoted to episodic memory.

**PII detection heuristic:**

A lightweight pattern-based PII detector flags potential PII in memory write payloads:

```python
class PIIDetector:
    """Pattern-based PII detection for memory write classification."""

    def scan(self, content: str) -> PIIScanResult: ...

class PIIScanResult:
    contains_pii: bool
    detected_types: list[str]  # e.g. ["email", "phone", "name_pattern"]
    confidence: float          # 0.0–1.0
```

Detection patterns: email addresses, phone numbers, national ID formats, credit card numbers, postal addresses. This is a best-effort heuristic, not a guaranteed filter — it serves as a defense-in-depth layer that auto-escalates classification when PII is detected.

When `PIIDetector.scan()` flags content with `confidence >= 0.7`, and the write's classification is below `SENSITIVE`, the classification is auto-escalated and an audit event is logged.

### C. Right to Erasure & Memory Compliance

**Subject identity tracking:**

Every memory write that involves user data includes a `subject_id` field — the identifier of the data subject whose information is being stored. This enables per-subject queries and deletions.

```python
# Episodic memory
async def save_episode(
    self,
    content: str,
    *,
    subject_id: str | None = None,      # NEW
    classification: DataClassification = DataClassification.INTERNAL,
    ...
) -> int: ...

# Relationship memory
# Already keyed by agent pairs; extend with subject_id for third-party references

# Notes
async def create_note(
    self,
    content: str,
    *,
    subject_id: str | None = None,      # NEW
    classification: DataClassification = DataClassification.INTERNAL,
    ...
) -> int: ...
```

**Erasure cascade:**

A new `DataComplianceManager` orchestrates erasure across all memory tiers:

```python
class DataComplianceManager:
    """Coordinates data subject rights across all memory tiers."""

    async def erase_subject(self, subject_id: str) -> ErasureReport:
        """Delete all data associated with a subject across all memory tiers.

        Returns a structured report suitable for regulatory evidence.
        """

    async def export_subject_data(self, subject_id: str) -> SubjectDataExport:
        """Export all data associated with a subject (GDPR Article 15 / CCPA access request)."""

    async def list_subject_data(self, subject_id: str) -> list[DataLocation]:
        """Enumerate all storage locations containing data for a subject."""
```

**Erasure report:**

```python
@dataclass
class ErasureReport:
    subject_id: str
    requested_at: datetime
    completed_at: datetime
    tiers_processed: list[str]           # ["episodic", "relationship", "notes"]
    records_deleted: dict[str, int]      # {"episodic": 12, "notes": 3, ...}
    verification_hash: str               # SHA-256 of report for tamper evidence
    audit_log_entry_id: str              # Correlation to immutable audit log
```

**Retention policies:**

Memory stores enforce configurable retention periods:

```yaml
# config/agents.yaml — per-agent memory compliance
memory:
  db_path: "data/memory.db"
  retention:
    default_days: 90            # Auto-delete after 90 days
    restricted_days: 30         # Restricted data deleted after 30 days
    audit_log_days: 365         # Audit logs retained for 1 year (regulatory minimum)
  erasure:
    enabled: true               # Enable subject erasure API
    verification: true          # Run post-erasure verification query
```

A background task (`DataRetentionEnforcer`) runs on configurable intervals and purges expired records, logging each deletion to the audit trail.

### D. Consent Tracking & Action Authorization

**Consent model:**

Consent is tracked per subject, per action class. An action class is a combination of tool name and risk level:

```python
@dataclass
class ConsentRecord:
    subject_id: str
    action_class: str           # e.g. "http_request:high", "shell_exec:critical"
    granted: bool
    granted_at: datetime
    expires_at: datetime | None
    scope: str                  # Free-text description of what was consented to
    revoked_at: datetime | None = None
```

**Consent enforcement:**

Before a tool invocation that affects a tracked subject, the tool execution pipeline checks:

1. Does a valid (non-expired, non-revoked) consent record exist for this subject + action class?
2. If not, and the action's risk level is High or Critical, the invocation is blocked and an audit event is logged.
3. If the action's risk level is Medium, a warning is logged but execution proceeds (configurable to block).

**Consent API (REST):**

The orchestrator exposes consent management endpoints:

```
POST   /api/v1/compliance/consent          # Record consent
GET    /api/v1/compliance/consent/{subject} # List consent records for a subject
DELETE /api/v1/compliance/consent/{id}      # Revoke consent
```

**Autonomous agent consent:**

Persona agents with `autonomy.level: "semi-autonomous"` or `"autonomous"` operate on pre-configured consent grants defined in `config/agents.yaml`:

```yaml
autonomy:
  level: "semi-autonomous"
  consent:
    pre_authorized:
      - action_class: "file_write:low"
        scope: "Write files within workspace directory"
      - action_class: "http_request:high"
        scope: "Send HTTP requests to whitelisted domains"
    requires_approval:
      - action_class: "shell_exec:high"
      - action_class: "http_request:critical"
```

### E. Ethical Guardrails at the Orchestrator Level

**Orchestrator-level action blocks:**

The orchestrator maintains an `EthicalPolicy` — a set of rules evaluated independently of LLM provider constraints. These rules operate at the action level, not the content level:

```go
type EthicalPolicy struct {
    BlockedActionPatterns []ActionPattern  // Actions that are always refused
    RequiredDisclosures   []Disclosure     // Mandatory transparency notices
    Logger                *zap.Logger
}

type ActionPattern struct {
    ToolPattern   string   // glob, e.g. "shell_exec"
    ArgPatterns   []string // regex on serialized args
    Reason        string   // human-readable refusal reason
    Severity      string   // "block" or "warn"
}
```

**Default blocked action patterns:**

| Pattern | Reason | Severity |
|---|---|---|
| `shell_exec` with args matching `rm -rf /`, `dd if=`, `mkfs` | Destructive system commands | block |
| `http_request` POST to unresolvable/internal IPs | Potential SSRF / data exfiltration | block |
| `file_write` to system paths outside workspace | System file modification | block |
| Any tool invocation by agent without valid capability token | Unauthorized agent action | block |

Operators extend the policy via a `config/ethical-policy.yaml` file (validated against a JSON schema):

```yaml
blocked_actions:
  - tool: "shell_exec"
    arg_patterns: ["curl.*\\|.*sh", "wget.*\\|.*bash"]
    reason: "Remote code execution via pipe"
    severity: "block"

  - tool: "http_request"
    arg_patterns: ["method.*DELETE"]
    reason: "Destructive HTTP operations require explicit approval"
    severity: "warn"

required_disclosures:
  - context: "user_interaction"
    message: "You are interacting with an AI agent, not a human."
```

**Bias evaluation hooks:**

For agents making decisions that affect people (configured via `decisions_affect_people: true` in agents.yaml), the tool pipeline includes an evaluation hook:

```python
class BiasEvaluationHook:
    """Hook point for bias evaluation on agent decisions.

    This provides the interface; actual evaluation logic is injected
    by operators or loaded from evaluators/.
    """

    async def evaluate(
        self,
        decision: str,
        context: dict[str, Any],
        affected_subjects: list[str],
    ) -> BiasEvalResult: ...
```

The default implementation logs the decision for offline review. Operators can inject custom evaluators via the evaluator registry.

### F. Regulatory Audit Infrastructure

**Extensions to RFC 0009's AuditLogger:**

RFC 0009 defines the `AuditLogger` with immutable, correlation-ID-linked entries. This RFC adds:

1. **Per-subject audit retrieval:**

```go
func (a *AuditLogger) QueryBySubject(subjectID string, from, to time.Time) ([]AuditEntry, error)
```

This enables responding to Subject Access Requests (GDPR Article 15) — "show me everything your system did with my data."

2. **Plain-language action summaries:**

Each audit entry includes a `summary` field — a human-readable, non-technical description of what happened:

```go
type AuditEntry struct {
    // ... existing fields from RFC 0009 ...
    SubjectID      string   // Data subject this action relates to (may be empty)
    Classification string   // Data classification level
    Summary        string   // Plain-language: "Agent ember-owl read file /workspace/report.md"
    ConsentRef     string   // Reference to consent record that authorized this action
}
```

3. **Retention policy enforcement:**

```go
type AuditRetentionPolicy struct {
    DefaultRetentionDays    int  // Minimum retention for all entries
    ComplianceRetentionDays int  // Extended retention for compliance-tagged entries
    AutoPurge               bool // Automatically purge expired entries
}
```

4. **Tamper-evidence verification:**

Periodic integrity checks on the audit log using hash chains:

```go
func (a *AuditLogger) VerifyIntegrity(from, to time.Time) (IntegrityReport, error)
```

### G. Transparency & Explainability

**AI disclosure:**

When agents interact with users (via channels/bridges, RFC 0011), the system must disclose that the interaction is with an AI agent. The `required_disclosures` in ethical policy configuration enforces this.

**Decision trail:**

For actions classified as Medium risk or above, the system records a `DecisionTrail` — a chain of reasoning steps that led to the action:

```python
@dataclass
class DecisionTrail:
    action_id: str
    agent_id: str
    timestamp: datetime
    reasoning_steps: list[str]    # Agent's chain of thought (summarized)
    inputs_used: list[str]        # Which context items influenced the decision
    tools_invoked: list[str]      # Tool calls that preceded this action
    risk_level: str
    subject_ids: list[str]        # Affected data subjects
```

This satisfies explainability requirements under GDPR Article 22 (automated decision-making) and emerging AI regulations.

### H. Intellectual Property Safeguards

**Provenance tracking:**

Agent-generated content is tagged with provenance metadata:

```python
@dataclass
class ContentProvenance:
    generated_by: str              # Agent ID
    model: str                     # LLM model used (e.g., "claude-sonnet-4-20250514")
    timestamp: datetime
    input_sources: list[str]       # Data sources that contributed
    tool_chain: list[str]          # Tools used in generation
    classification: str            # "ai_generated", "ai_assisted", "human"
```

**Output guards:**

The tool output pipeline includes a configurable content check for verbatim reproduction. When enabled, large text outputs from agents are checked against a configurable similarity threshold:

```yaml
ip_safeguards:
  enabled: true
  verbatim_check:
    min_length_chars: 500          # Only check outputs longer than this
    max_verbatim_ratio: 0.8        # Flag if >80% of output matches a single source
  provenance_tracking: true
```

This is a best-effort heuristic. The check compares agent output against the input sources used in the current task context. It does not replace legal review for IP compliance.

---

## Security Considerations

This RFC extends the security posture established by RFC 0009:

- **Data classification adds a new dimension to the attack surface.** If an attacker can manipulate the classification of a memory write (e.g., downgrading `RESTRICTED` to `PUBLIC`), they bypass privacy controls. Classification must be set server-side based on PII detection and cannot be overridden by agent-reported values alone.
- **Consent records are security-critical.** Tampering with consent records could authorize actions a subject never approved. Consent storage must use the same tamper-evidence mechanisms as the audit log.
- **Erasure verification must be genuine.** A post-erasure verification that simply checks "no rows returned" is insufficient if the data was moved rather than deleted. Verification queries must cover all storage locations including FTS5 indexes, WAL files, and any caches.
- **Ethical policy bypass.** The `EthicalPolicy` runs in the orchestrator (Go), which agents cannot bypass because all tool invocations route through the orchestrator's security boundary (RFC 0009 §H). However, the policy configuration file itself must be protected from unauthorized modification.
- **PII detection false negatives.** The `PIIDetector` is pattern-based and will miss novel PII formats. Operators handling regulated data should configure classification defaults conservatively (`SENSITIVE` rather than `INTERNAL`).

---

## Phased Implementation Plan

### Phase 1: Risk Taxonomy & Ethical Policy Configuration (no code dependencies)

**Summary**: Define the action risk classification taxonomy, create the ethical policy configuration schema, and add risk level annotations to existing tools.

**Deliverables**:
1. Add `risk_level` field to the `@tool` decorator in `agents/tools/registry.py`.
2. Annotate all existing built-in tools with appropriate risk levels.
3. Create `schemas/ethical-policy.schema.json` — JSON schema for ethical policy configuration.
4. Create `config/ethical-policy.yaml` — default ethical policy with blocked action patterns.
5. Implement `EthicalPolicy` struct in `internal/security/` — load and evaluate action patterns.
6. Add `risk_overrides` to `schemas/agent.schema.json` and `config/agents.yaml`.
7. Unit tests for policy loading, pattern matching, and risk level validation.

**Dependencies**: None (can start immediately).

### Phase 2: Data Classification & PII Detection

**Summary**: Add data classification annotations to memory operations and implement the PII detection heuristic.

**Deliverables**:
1. Define `DataClassification` enum in `agents/memory/`.
2. Add `classification` and `subject_id` parameters to episodic memory, relationship memory, and note storage interfaces.
3. Add `subject_id` column to SQLite schemas with appropriate indexing.
4. Implement `PIIDetector` — pattern-based PII scanning with auto-escalation.
5. Schema migration for existing memory databases (add columns with `INTERNAL` default).
6. Unit tests for PII detection patterns and classification enforcement.

**Dependencies**: Phase 1 (for data classification definitions).

### Phase 3: Right to Erasure & Retention (depends on RFC 0009 Phase 1 — AuditLogger)

**Summary**: Implement per-subject data erasure, export, and configurable retention policies.

**Deliverables**:
1. Implement `DataComplianceManager` — erasure cascade across all memory tiers.
2. Implement `ErasureReport` generation with verification and audit logging.
3. Implement `SubjectDataExport` for subject access requests.
4. Implement `DataRetentionEnforcer` — background purge of expired records.
5. Add `retention` configuration to `config/agents.yaml` schema.
6. REST endpoints: `POST /api/v1/compliance/erasure/{subject}`, `GET /api/v1/compliance/export/{subject}`.
7. Integration tests: erasure across all tiers, verification, retention enforcement.

**Dependencies**: RFC 0009 Phase 1 (AuditLogger for logging erasure operations).

### Phase 4: Consent Tracking & Orchestrator Enforcement (depends on RFC 0009 Phase 2 — HITL gates)

**Summary**: Implement consent tracking, per-action-class authorization, and orchestrator-level enforcement of risk-gated tool invocations.

**Deliverables**:
1. Implement `ConsentRecord` storage (SQLite, alongside audit log).
2. Implement consent checking in the tool execution pipeline.
3. Add consent pre-authorization config to `agents.yaml` schema.
4. REST endpoints: consent CRUD (`/api/v1/compliance/consent`).
5. Wire risk-level enforcement into orchestrator dispatch — Critical → HITL, High → capability check.
6. Integration tests: consent-gated tool execution, autonomous agent pre-authorization.

**Dependencies**: RFC 0009 Phase 2 (HITL gate mechanism for Critical-risk actions).

### Phase 5: Audit Extensions & Transparency

**Summary**: Extend the audit infrastructure with compliance-specific features and transparency mechanisms.

**Deliverables**:
1. Add `SubjectID`, `Classification`, `Summary`, `ConsentRef` fields to `AuditEntry`.
2. Implement `QueryBySubject` on `AuditLogger`.
3. Implement `DecisionTrail` recording for Medium+ risk actions.
4. Implement `AuditRetentionPolicy` with auto-purge.
5. Implement `VerifyIntegrity` hash-chain verification.
6. Add `ContentProvenance` tracking to agent output pipeline.
7. Unit and integration tests for audit querying, retention, and integrity verification.

**Dependencies**: Phase 3, Phase 4, RFC 0009 Phase 1.

---

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Go orchestrator | `internal/security/security.go` | Add `EthicalPolicy`, risk enforcement |
| Go orchestrator | `internal/security/ethical_policy.go` | New: policy loading, pattern matching |
| Go orchestrator | `internal/security/audit_compliance.go` | New: compliance extensions to AuditLogger |
| Go orchestrator | `internal/server/compliance_handlers.go` | New: REST endpoints for consent, erasure, export |
| Go orchestrator | `internal/server/routes.go` | Register compliance endpoints |
| Python agents | `agents/tools/registry.py` | Add `risk_level` to `@tool` decorator |
| Python agents | `agents/tools/builtin.py` | Annotate all tools with risk levels |
| Python agents | `agents/memory/episodic.py` | Add `subject_id`, `classification` fields |
| Python agents | `agents/memory/relationship.py` | Add `subject_id` field |
| Python agents | `agents/memory/notes.py` | Add `subject_id`, `classification` fields |
| Python agents | `agents/memory/migrations.py` | Schema migration for new columns |
| Python agents | `agents/compliance.py` | New: `DataComplianceManager`, `PIIDetector`, `ConsentRecord` |
| Python agents | `agents/provenance.py` | New: `ContentProvenance` tracking |
| Config | `config/agents.yaml` | Add retention, consent, risk_overrides sections |
| Config | `config/ethical-policy.yaml` | New: default ethical policy |
| Schemas | `schemas/agent.schema.json` | Add compliance-related fields |
| Schemas | `schemas/ethical-policy.schema.json` | New: ethical policy schema |

## Test Strategy

- **Unit tests**: PII detection patterns (positive/negative cases), risk level validation, ethical policy pattern matching, consent record CRUD, data classification enforcement, retention calculation, erasure cascade (per-tier mocks).
- **Integration tests**: End-to-end erasure across real SQLite memory stores with verification. Consent-gated tool execution with mock HITL. Audit log querying by subject. Retention enforcement with time-based purge.
- **E2E / smoke tests**: Submit a workflow involving PII data → verify classification auto-escalation → verify subject appears in audit → invoke erasure → verify data removed → verify audit log records the erasure.
- **Manual tests**: Review ethical policy configuration against common attack patterns. Review plain-language audit summaries for clarity. Verify SQLite WAL and FTS5 indexes are cleared on erasure.

---

## Open Questions

1. **Should PII detection run synchronously in the memory write path or asynchronously?** Synchronous detection adds latency to every memory write but ensures classification is correct before persistence. Asynchronous detection is faster but creates a window where data is stored with incorrect classification.

2. **How should consent interact with sub-agent spawning (RFC 0010)?** When a parent agent spawns a sub-agent to handle a task involving a specific subject, does the parent's consent grant cascade to the child, or must the sub-agent independently verify consent? The capability token model (RFC 0009) provides a mechanism, but the consent policy needs explicit design.

3. **What is the right default retention period?** The current proposal uses 90 days for general data and 30 days for restricted data. These defaults should be validated against common regulatory requirements. GDPR doesn't specify a fixed period — it requires data to be kept "no longer than necessary."

4. **Should the ethical policy be hot-reloadable?** Changing ethical guardrails at runtime has implications for consistency. A policy change mid-workflow could cause inconsistent enforcement across steps.

5. **How does this interact with cross-border data flows in v0.3 (mesh networking)?** When agents run on different nodes in different jurisdictions, data classification and retention requirements may differ. This RFC defers jurisdictional logic but should ensure the data model supports it.

---

## Decision / Next Steps

1. Review this RFC for feasibility and alignment with the v0.2 roadmap.
2. Validate the risk classification taxonomy against known deployment scenarios.
3. Confirm Phase 1–2 can proceed without RFC 0009 dependencies (parallel development).
4. Accept RFC → create PR plan → begin Phase 1 implementation.

---

## Related Documentation

- [RFC 0009 — Agent Identity, Security & Sandboxing](0009-security-sandboxing.md) — Technical security layer this RFC builds on
- [Architecture Spec §6.6–6.9](../ai-agents-orchestration-spec.md) — Audit trail, rate limiting, input sanitization
- [Extension Spec](../persatrix-extension-spec.md) — Persona agent capabilities, memory architecture
- [Spec Audit §31–35](../persatrix-spec-audit.md) — Security and privacy gaps identified in audit
- [RFC 0005 — Persona Agent & Memory System](0005-persona-agent-memory.md) — Memory tier architecture
- [RFC 0008 — Agent Memory & Context Optimization](0008-agent-memory-context-optimization.md) — Shared memory policies
