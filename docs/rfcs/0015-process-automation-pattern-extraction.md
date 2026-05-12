---
id: RFC-0015
title: Process Automation & Pattern Extraction
summary: Detects recurring multi-step agent workflows from execution history and crystallizes them into reusable, parameterized workflow templates.
type: feature
status: proposed
author: Maksim Khomutov
created: 2026-04-19
target: v0.5.0
depends_on:
  - RFC-0006
  - RFC-0008
  - RFC-0009
  - RFC-0014
---

# RFC 0015 — Process Automation & Pattern Extraction

**Type**: feature
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-04-19
**Target**: v0.5.0
**Depends on**: RFC 0006, RFC 0008, RFC 0009, RFC 0014

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Terminology & Position in the Architecture](#a-terminology--position-in-the-architecture)
  - [B. Pattern Detection](#b-pattern-detection)
  - [C. Candidate Data Model](#c-candidate-data-model)
  - [D. Specification Before Code](#d-specification-before-code)
  - [E. Deterministic Skill Implementations](#e-deterministic-skill-implementations)
  - [F. Test Requirements and Registration Gate](#f-test-requirements-and-registration-gate)
  - [G. Registry-First Invocation](#g-registry-first-invocation)
  - [H. Sandboxing and Rollback](#h-sandboxing-and-rollback)
  - [I. Automation Lifecycle](#i-automation-lifecycle)
  - [J. Governance and Audit](#j-governance-and-audit)
  - [K. Relationship to RFC 0014](#k-relationship-to-rfc-0014)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

This RFC defines the pattern extraction and deterministic automation pipeline deferred by [RFC 0014](0014-agent-skill-registry-lifecycle.md) (Open Question 4 — "Learned skill extraction pipeline"). It specifies how the orchestrator identifies repeated reasoning sequences from execution history, how those patterns are promoted to deterministic, code-backed skill implementations that replace LLM reasoning for well-understood task classes, and how the promotion is gated by specifications, tests, sandbox execution, and human review. The design extends RFC 0014's `SkillSpec` data model with a deterministic handler path (`source: "learned"`) rather than introducing a parallel automation registry.

## Motivation

RFC 0014 gives Persatrix a first-class capability registry but intentionally leaves three gaps:

1. **No extraction path**. `SkillSpec.source = "learned"` is reserved in the data model, but the mechanism that produces learned skills from observed runs is out of scope (RFC 0014 §H, Open Question 4).
2. **No deterministic-skill handler contract**. All RFC 0014 skills are implicitly LLM-backed. There is no contract for a skill whose implementation is a tested Python function running in a sandbox with zero token cost.
3. **No pattern telemetry**. RFC 0006 tracks per-task cost and RFC 0014 tracks per-skill health, but neither aggregates structural similarity across runs — which is the signal that tells you a pattern is worth automating.

**Why this matters now (v0.5.0 target):**

- By v0.4.0, the Skill Registry (RFC 0014) is live and the skill catalogue contains real invocation history.
- By v0.4.0, sub-agent spawning (RFC 0010) has multiplied the number of agent runs per user action. Repetition across those runs is a load-bearing cost signal.
- By v0.5.0, external bridges (RFC 0011) pipe in user-driven traffic, which is where high-repetition patterns actually emerge (internal workflows are too heterogeneous to automate productively).

**What happens if we do nothing:**

- Token spend grows linearly with runs even for sequences the system has seen thousands of times.
- Output variance remains high on well-understood tasks, because every run re-reasons from scratch.
- `source: "learned"` rots in the `SkillSpec` schema — present but unreachable. RFC 0014 accumulates documentation debt.
- The registry continues growing without a corresponding path for capability *consolidation* through promotion.

**What this RFC changes:**

- Adds a detection layer that flags repeated structural patterns from telemetry.
- Defines a promotion pipeline: candidate → spec → scaffolded test suite → human-reviewed implementation → sandbox verification → registration.
- Extends RFC 0014's `SkillSpec` with a deterministic handler reference and schema for code-backed skills.
- Establishes governance: ownership, usage-based deprecation, auto-suspension on success-rate decay, version rollback audit.

## Goals

1. Detect repeated structural patterns in agent execution history using telemetry emitted by RFC 0006 and RFC 0014 (no runtime introspection of agent internals).
2. Define `AutomationCandidate` records: a structured description of a repeated pattern with enough context for a specification pass.
3. Define a mandatory **specification-before-code** gate: a candidate becomes a draft `SkillSpec` with input/output schemas, failure modes, and parameterisation before any implementation is written.
4. Extend `SkillSpec` with `implementation: DeterministicHandler` — a code-backed handler contract (module path, callable name, capability requirements) that slots into RFC 0014's existing skill dispatch.
5. Require tests alongside code — `registry.register()` rejects deterministic skills without a passing test suite whose coverage meets a configured floor.
6. Require sandbox execution for every new deterministic skill before first production routing, using RFC 0009's sandbox primitives.
7. Provide a **rollback path** on every deterministic skill that mutates state: either a compensating callable or an explicit `reversible: false` declaration that triggers additional review.
8. Enforce **registry-first invocation** on agents: when a matching healthy deterministic skill exists, agents must route through it rather than re-reasoning, and deviations are logged.
9. Make **human review mandatory** for deterministic skills that touch external systems, mutate persistent state, or handle data classified as sensitive (RFC 0013 integration point).
10. Emit per-candidate and per-skill governance telemetry: detection count, promotion rate, post-promotion success rate, usage-based staleness, cost savings.

## Non-Goals

- **LLM-generated implementation code.** Deterministic handlers are hand-written by humans from the agent-drafted specification. The agent drafts the *spec and tests*, not the implementation. Auto-generated implementations are explicitly deferred to a future RFC — they require a separate trust model (RFC 0014 §H already excluded this).
- **Replacing RFC 0014.** This RFC extends `SkillSpec` and `SkillRegistry`; it does not create a parallel automation registry.
- **Replacing the tool layer (`agents/tools/`).** Deterministic skills compose tools; they do not bypass the tool permission model.
- **Semantic/embedding-based pattern matching.** Phase 1 uses structural similarity over telemetry features. Embedding-based clustering is a v0.6+ concern, after distributed pattern mining becomes feasible.
- **Cross-node pattern aggregation.** Detection is single-orchestrator-scoped. Federated detection is a v0.6 concern tied to the distributed mesh.
- **Auto-promotion without human review.** Every deterministic skill passes a human gate. This RFC does not define an auto-promotion path.
- **Re-implementing RFC 0006 cost telemetry.** Detection consumes the existing telemetry stream; it does not duplicate measurement.

---

## Design / Implementation

### A. Terminology & Position in the Architecture

- **Pattern**: a recurring structural sequence of skill invocations and tool calls observed across multiple runs with similar input shapes and output shapes.
- **AutomationCandidate**: a record emitted by the detection layer that describes one detected pattern and its evidence (invocation count, structural hash, representative inputs/outputs, variance metrics).
- **Deterministic skill**: a `SkillSpec` with `implementation: DeterministicHandler` — a Python callable whose execution does not call an LLM. It may call tools.
- **LLM skill**: the existing RFC 0014 skill shape — the implementation is an agent prompt + tool calls. (RFC 0014 never named these explicitly; this RFC does, to make the contrast visible.)
- **Promotion**: the lifecycle transition from candidate → draft spec → implemented deterministic skill → registered skill.

**Where this lives in the stack:**

```
+-------------------------------------+
|    Orchestrator (Go)                |
|    - SkillCatalogue (RFC 0014)      |
|    - PatternDetector (new)          |
|    - AutomationCandidateStore (new) |
+---------------+---------------------+
                |
                | (candidates emitted as telemetry events)
                |
+---------------v---------------------+
|    Python agents                    |
|    - SkillRegistry (RFC 0014)       |
|      extended for DeterministicHandler
|    - SkillExecutor (new dispatcher) |
|    - SkillSandboxRunner (new)       |
+-------------------------------------+
```

The detection layer lives on the Go side — it reads the same telemetry RFC 0006 and RFC 0014 already emit. The execution layer lives on the Python side, where skills are dispatched today. No new proto contracts are required in Phase 1 (see Open Question 1).

### B. Pattern Detection

Detection runs as a background task in the orchestrator. It is not in the hot path of task execution.

**Signal sources:**

1. **`skill_invocations_total`** (RFC 0014 §I) — counter by skill name, agent ID, outcome. Drives raw repetition count.
2. **`StepExecutionMetadata`** (RFC 0006 §4a) — token counts, LLM call counts, cache hit, wall time. Drives cost-weighted prioritisation.
3. **Step input/output envelopes** from the state store — structural hashing on the schema-stripped request/response.
4. **Agent-initiated flags** — agents may emit a `PatternFlagEvent` mid-task via a new `report_pattern(fingerprint)` method on `BaseAgent`. This captures repetition at the point of highest context (the agent remembers having done the same thing before).

**Detection features per pattern:**

| Feature | Source | Purpose |
|---------|--------|---------|
| `structural_hash` | input/output envelope (schema-stripped) | identity across invocations with varying values |
| `invocation_count` | `skill_invocations_total` | raw frequency |
| `avg_tokens` | `StepExecutionMetadata.tokens` | cost weight |
| `output_variance` | hash distribution over outputs for identical input hashes | automation safety signal |
| `avg_latency_ms` | `skill_latency_p50_ms` | efficiency weight |
| `recency_days` | last-seen timestamp | freshness |
| `flag_count` | `PatternFlagEvent` emissions | agent-raised signal |

**Candidate scoring:**

```
score = ln(invocation_count) * (1 + avg_tokens / 1000) * confidence_factor

where confidence_factor =
    1.0  if output_variance <= low_variance_threshold
    0.5  if output_variance in (low, high)
    0.0  if output_variance >= high_variance_threshold   # rejected
```

Natural log (`ln`) is used so that scores at practical invocation counts (10–1000) are bounded in the range 2.3–6.9, making threshold configuration tractable. Log base 10 or 2 would produce values in different ranges and change the relative weight of the token-cost factor.


High-variance patterns are *not* candidates. The guidance in the source material is explicit: tasks where agents produce low-variance output are the safest to automate. High variance is a signal that judgment is involved — automation would be wrong.

**Thresholds (defaults, configurable in `config/automation.yaml`):**

- `min_invocations`: 5 (below this, never a candidate)
- `min_recency_days`: 30 (patterns not seen in 30 days are excluded)
- `low_variance_threshold`: 0.05 (normalised output-hash entropy)
- `high_variance_threshold`: 0.30
- `scan_interval`: 24h (Phase 1 — daily batch scan)

Detection never mutates runtime state. It emits `AutomationCandidate` records to the `AutomationCandidateStore`. Promotion is always human-initiated.

### C. Candidate Data Model

```go
// internal/automation/candidate.go
type AutomationCandidate struct {
    ID                  string            // UUID
    StructuralHash      string            // schema-stripped I/O hash
    FirstSeenAt         time.Time
    LastSeenAt          time.Time
    InvocationCount     int
    AvgTokens           float64
    OutputVariance      float64
    Score               float64
    RepresentativeInputs  []map[string]any  // up to 5 sampled
    RepresentativeOutputs []map[string]any
    ObservedSkillNames  []string          // which existing skills are involved
    AgentIDs            []string          // which agents produce this pattern
    Status              CandidateStatus
    PromotedToSkill     string            // skill name if promoted; "" otherwise
}

type CandidateStatus string
const (
    CandidateOpen       CandidateStatus = "open"       // newly detected
    CandidateTriaged    CandidateStatus = "triaged"    // marked for spec drafting
    CandidatePromoted   CandidateStatus = "promoted"   // became a deterministic skill
    CandidateRejected   CandidateStatus = "rejected"   // reviewed and declined
    CandidateDeprecated CandidateStatus = "deprecated" // no longer observed
)
```

The `AutomationCandidateStore` is queryable via a new REST endpoint (`GET /api/v1/automation/candidates`) so operators can triage. Phase 1 exposes list, get, and status-transition endpoints only — no write-through automation. Rejected candidates are retained for 90 days as a suppression list (same `structural_hash` is not re-emitted within the window).

### D. Specification Before Code

A candidate does not become a deterministic skill directly. It first becomes a **draft `SkillSpec`** produced by an agent working from the candidate record.

**Draft-spec pipeline:**

1. Operator runs `persatrix automation draft <candidate_id>` via the CLI.
2. The orchestrator invokes a dedicated `skill-spec-drafter` agent (new agent role) with the candidate payload.
3. The drafter emits a `SkillSpec` YAML draft to `drafts/skills/<candidate_id>.draft.yaml` with:
   - `name`, `description`, `layer: procedural`, `source: "learned"`
   - `input_schema` and `output_schema` derived from representative inputs/outputs
   - `fallback_chain: [<original_llm_skill_name>]` — the LLM skill remains the fallback so regressions are safe
   - `high_stakes: true` if any observed tool call in the pattern matches RFC 0009's high-risk action classification
   - `implementation:` left blank for human authoring
   - `tests:` a scaffolded test list (input/output pairs from representative runs; edge cases from observed failures)
4. The draft is committed to a review branch. **The drafter agent never writes implementation code** — that is the non-goal above.
5. A human reviews the draft. Changes are normal PR edits. Approval promotes the spec from `drafts/` to `config/skills.yaml` and unlocks implementation.

The drafter agent has `source: "authored"` and a narrow skill set (read candidate, read RFC 0014 registry, write draft). It cannot register skills itself — that is a config-plane operation.

### E. Deterministic Skill Implementations

Extend `SkillSpec` with a typed implementation reference:

```python
# agents/skills/registry.py (RFC 0014 extension)

@dataclass
class DeterministicHandler:
    module: str           # e.g. "persatrix_agents.automation.impl.markdown_slug"
    callable_name: str    # e.g. "execute"
    reversible: bool      # True if the handler provides a compensating path
    reverse_callable: str | None = None  # name of compensating callable
    timeout_ms: int = 5000
    sandbox_profile: str = "default"      # RFC 0009 sandbox profile name

@dataclass
class SkillSpec:
    # ... existing fields from RFC 0014 ...
    implementation: DeterministicHandler | None = None
    # When set, the skill is deterministic (source="learned" or "authored" with
    # a code-backed handler). When None, the skill is LLM-backed (existing shape).
```

**Handler contract (Python):**

```python
# agents/automation/base.py
from typing import Protocol

class DeterministicSkillHandler(Protocol):
    async def execute(self, inputs: dict, context: SkillContext) -> dict: ...
    # If the skill is reversible, a compensating callable is also required:
    async def reverse(self, inputs: dict, outputs: dict, context: SkillContext) -> None: ...
```

`SkillContext` exposes the narrow capabilities the handler needs: permission-gated tool invocations (RFC 0004 / RFC 0009) and structured logger. It does **not** expose the LLM client, state store, or registry mutation APIs.

**Dispatch integration:**

`SkillExecutor` is the new dispatcher that sits inside each agent's task handler. For each skill invocation:

1. `SkillRegistry.get(name)` returns the `SkillSpec`.
2. If `spec.implementation` is set → route to `DeterministicSkillRunner.run(spec, inputs)`.
3. Else → route to the existing LLM skill path.
4. Either path feeds through `SkillValidator.check_preconditions` and `check_postconditions` (RFC 0014 §E) — schema validation is shared.

Deterministic execution produces the same `SkillFailureMode` values as LLM execution (schema mismatch, timeout, capability gap, etc.), so the orchestrator's fallback logic (RFC 0014 §F) works unchanged — `fallback_chain` for a learned skill typically points to its LLM progenitor.

### F. Test Requirements and Registration Gate

Tests are non-negotiable for deterministic skills. The registry enforces this at load time.

**Required test artefacts per deterministic skill:**

1. **Happy path** — at least one input/output pair drawn from representative runs in the candidate record.
2. **Historical edge cases** — every observed failure mode in the pattern history is exercised.
3. **Schema invariants** — property-based tests over the `input_schema`: malformed inputs must produce `SkillFailureMode.SCHEMA_MISMATCH`, never an uncaught exception.
4. **Reversibility test** — if `reversible: true`, a test demonstrates that `reverse` restores state for every happy-path case.

**Test location:** `tests/automation/<skill_name>/` alongside the handler module.

**Registration gate (enforced by `SkillRegistry.register` at load time):**

- Deterministic skills whose `tests/` directory is missing → rejected with `SkillLoadError("tests_missing")`.
- Deterministic skills whose test suite fails → rejected with `SkillLoadError("tests_failing")`. CI runs these as part of `make test`.
- Deterministic skills with `reversible: true` but no `reverse_callable` declared → rejected with `SkillLoadError("reverse_missing")`.
- Deterministic skills with coverage below `min_coverage` (default 0.80) → rejected with `SkillLoadError("coverage_below_floor")`.

These gates apply at `make validate` time as well — the schema extension in `schemas/skill.schema.json` (from RFC 0014) gains a `tests_present: bool` field populated by the validator tool before the schema check.

### G. Registry-First Invocation

RFC 0014 already routes by skill name. This RFC adds **registry-first discipline** inside agents: before an agent reasons through a task, it checks whether a deterministic skill covers it.

**Agent-side discipline (enforced in `agents/base.py` task handler):**

1. When an agent receives a task, it computes the task's structural fingerprint using the same hashing function the detector uses.
2. `SkillRegistry.find_matching(fingerprint)` returns any deterministic skills whose `input_schema` accepts the task payload.
3. If a match exists and is `HEALTHY`, the agent dispatches through it. No LLM call is made.
4. If no match, the agent falls back to LLM reasoning. A `skill_deterministic_miss` telemetry event is emitted (this is normal — not all tasks automate).
5. If a match exists but the agent's handler code deliberately re-reasons anyway, a `skill_deterministic_bypass` event is emitted with the reason. Repeated bypasses are flagged in governance (Section J).

Registry-first is not mandatory in the sense of aborting LLM paths — some tasks genuinely benefit from reasoning even when a deterministic skill exists (e.g., the user is debugging why automation produced a wrong answer). The telemetry lets operators see *when* and *why* the LLM path is preferred.

### H. Sandboxing and Rollback

All deterministic skill invocations run in a sandbox. Phase 1 leverages RFC 0009's sandbox primitives; Phase 2 adds automation-specific profiles.

**Sandbox profile (`sandbox_profile: "default"`):**

- CPU timeout: `spec.implementation.timeout_ms` (default 5s)
- Memory cap: 512 MB
- Network: denied unless the handler declares required hosts in `spec.implementation.allowed_hosts` (validated against agent's permission config)
- Filesystem: read-only except for `config/agents/state/<agent_id>/scratch/` (ephemeral per-task)
- Tool invocations: all go through RFC 0004's `PermissionGate` — the sandbox does not bypass agent-level permissions

**Rollback:**

- `reversible: true` skills are **required** for any handler that mutates persistent state (writes to the state store, posts to a bridge, calls a writing tool). This is enforced by static inspection at registration time: if the handler imports or calls a known-mutating tool, `reversible: false` triggers `SkillLoadError("mutation_without_reverse")`.
- When a deterministic skill invocation fails postcondition validation, the orchestrator calls `reverse` before surfacing the failure.
- `reversible: false` is permitted only for pure-compute handlers (no side effects). A skill marked `reversible: false` that calls a mutating tool is a registration error.

### I. Automation Lifecycle

Extends RFC 0014's governance lifecycle (§M) with automation-specific states:

```
Detect (candidate) → Triage → DraftSpec → Review →
    Implement → Test → Sandbox → Register (health=UNKNOWN) →
        Monitor → Validate → Deprecate
                     ↑
                Update (version bump)
```

| Stage | Entry gate | Exit gate |
|-------|-----------|-----------|
| Detect | Pattern score crosses threshold | `AutomationCandidate` persisted |
| Triage | Operator reviews candidate list | `CandidateStatus = triaged` |
| DraftSpec | `persatrix automation draft` invoked | Draft YAML committed to `drafts/skills/` |
| Review | PR opened on draft | PR merged; spec moved to `config/skills.yaml` |
| Implement | Human authors handler module + tests | `make test` passes for the skill's tests |
| Sandbox | Skill loaded with `health=UNKNOWN` | ≥ 10 sandbox invocations over synthetic inputs pass |
| Register | Sandbox stage complete | `SkillCatalogue` entry created; routing enabled |
| Monitor | First production invocations | ≥ 50 invocations observed; health transitions out of `UNKNOWN` |
| Validate | 90 days elapsed OR version bump | Health confirmed `HEALTHY`; knowledge cutoff refreshed if applicable |
| Deprecate | Superseded OR success rate decays | `deprecated_at` set; routing removed |

**Auto-suspension (extends RFC 0014 §I health derivation):**

When a deterministic skill transitions to `UNHEALTHY`, it is **automatically suspended** — routing is disabled and operator review is required to re-enable. This is stricter than the RFC 0014 baseline (which uses fallback chains) because a degrading deterministic skill produces bad output silently, without the variance signals an LLM skill naturally emits.

**Usage-based deprecation:**

Skills not invoked in 90 days emit a `skill_unused` event and are flagged for deprecation review. Without a human decision in 30 days, the skill transitions to `deprecated_at = now()` and is unassigned from agent configs via a governance PR.

### J. Governance and Audit

Every state transition emits an audit event via RFC 0009's `AuditLogger`:

| Event | Trigger |
|-------|---------|
| `automation_candidate_detected` | Pattern score crosses threshold |
| `automation_candidate_triaged` | Operator transitions candidate to triaged |
| `automation_spec_drafted` | Drafter agent completes spec |
| `automation_spec_approved` | Draft PR merged |
| `automation_skill_registered` | Implementation passes sandbox stage |
| `automation_skill_bypassed` | Agent deliberately chose LLM path over registered deterministic skill |
| `automation_skill_suspended` | Health transition to `UNHEALTHY` |
| `automation_skill_rolled_back` | Previous version restored |
| `automation_skill_reverse_invoked` | Postcondition failure triggered compensating callable |
| `automation_skill_deprecated` | Skill deprecated |

**Ownership is mandatory.** Every deterministic skill declares:

```yaml
ownership:
  owner_role: <agent role>           # RFC 0014 field, kept
  maintainer_team: <human team>      # new; required for deterministic skills
  review_cadence_days: 90            # lifecycle gate
```

Skills without `maintainer_team` are rejected at load time. This enforces the governance rule from the motivating guidance: "ownerless scripts get deprecated."

### K. Relationship to RFC 0014

This RFC is strictly additive to RFC 0014. Specifically:

- **No replacement of `SkillSpec`** — `implementation` is an optional new field; existing LLM skills continue to work with `implementation = None`.
- **No replacement of `SkillRegistry`** — the registry gains a new validation branch (test/coverage/reverse checks) for specs with `implementation` set.
- **No replacement of `SkillCatalogue`** — routing by skill name is unchanged. Deterministic skills appear in the catalogue alongside LLM skills.
- **No new failure modes** — `SkillFailureMode` values from RFC 0014 §F cover deterministic failures (`SCHEMA_MISMATCH`, `TIMEOUT`, `CAPABILITY_GAP` on sandbox denial, `DEPENDENCY_DOWN` on allowed-host unreachability).
- **No new observability pipeline** — metrics from RFC 0014 §I apply to deterministic skills without changes. Automation-specific telemetry (candidate lifecycle events) is layered on top.
- **Resolves RFC 0014 Open Question 4** — this RFC is the follow-on the open question anticipated. RFC 0014 should be updated to reference RFC 0015 in the resolution.

---

## Security Considerations

1. **Untested-handler poisoning.** A deterministic skill registered without adequate tests could silently produce incorrect outputs at scale. Mitigation: registration gate enforces test presence + coverage + passing suite. CI runs these tests on every PR.

2. **Sandbox escape via handler code.** A malicious or buggy handler could attempt to exceed its sandbox. Mitigation: sandbox enforces CPU/memory/network/filesystem limits through RFC 0009 primitives. Handlers never get raw LLM-client or state-store access — only narrow `SkillContext` capabilities.

3. **Irreversible state mutation.** A deterministic skill that writes persistent state without a `reverse` path can leave the system inconsistent on failure. Mitigation: registration-time static inspection rejects `reversible: false` handlers that call mutating tools; postcondition failure triggers `reverse` before surfacing the failure.

4. **Pattern detection over sensitive inputs.** Representative inputs persisted in `AutomationCandidate` records could contain PII or credentials. Mitigation: inputs are run through RFC 0013's PII detector before persistence; matched fields are hashed or redacted. (This is a hard blocker for Phase 1 — detection cannot ship before RFC 0013 Phase 1.)

5. **Promotion of a coincidentally-similar pattern.** Automating a pattern that is *statistically* repeated but semantically divergent produces silent wrong answers. Mitigation: variance-based confidence factor in scoring; mandatory spec review; fallback chain always points to the LLM progenitor.

6. **Drafter agent scope creep.** The spec-drafter agent could be prompt-injected into writing a wider spec than the candidate warrants. Mitigation: drafter has a narrow, declared skill set; its output is a YAML file reviewed by a human before merging; it cannot register skills itself.

7. **Registry-first bypass as an attack surface.** An agent configured to always bypass deterministic skills (preferring LLM paths) re-introduces the cost and variance the automation was designed to eliminate. Mitigation: `skill_deterministic_bypass` audit events; repeated bypasses per agent flagged in governance telemetry for operator review.

8. **Rollback to vulnerable version.** Rolling a deterministic skill back to a prior version may re-introduce a known bug or vulnerability. Mitigation: extends RFC 0014 §Security #5 — rollback emits `automation_skill_rolled_back`, and the 30-day rollback window from RFC 0014 Phase 3 applies.

9. **Automation of high-risk actions without HITL.** Deterministic skills that invoke high-risk tools (RFC 0009 classification) could bypass HITL gates that exist for LLM paths. Mitigation: `high_stakes: true` on the `SkillSpec` forces an HITL gate on deterministic invocations identically to LLM invocations. The drafter agent auto-sets `high_stakes` based on observed tool classifications.

10. **Sandbox profile downgrade.** An operator loosening `sandbox_profile` to avoid false-positive denials could weaken the trust model. Mitigation: sandbox profile changes emit `automation_sandbox_profile_changed` audit events and require `maintainer_team` sign-off in the governance process.

---

## Phased Implementation Plan

### Phase 1: Pattern Detection & Candidate Store

Summary: Add the detection pipeline, candidate store, and operator-facing triage endpoints. No promotion yet — this phase only generates `AutomationCandidate` records from existing telemetry.

Deliverables:

1. `internal/automation/detector.go` — background task consuming `skill_invocations_total` and `StepExecutionMetadata`; structural hashing; candidate scoring.
2. `internal/automation/candidate.go` — `AutomationCandidate` struct, `AutomationCandidateStore` interface, `InMemoryAutomationCandidateStore`.
3. `internal/server/automation.go` — REST endpoints: `GET /api/v1/automation/candidates`, `GET /api/v1/automation/candidates/{id}`, `POST /api/v1/automation/candidates/{id}/triage`, `POST /api/v1/automation/candidates/{id}/reject`.
4. `BaseAgent.report_pattern(fingerprint)` hook in `agents/base.py`, emitting `PatternFlagEvent` through the gRPC telemetry path.
5. `config/automation.yaml` with detection thresholds; extend `make validate` with a new schema.
6. PII redaction of representative inputs via RFC 0013's PII detector (hard dependency).
7. Unit tests: structural hashing determinism, scoring bounds, variance calculation, threshold transitions, candidate deduplication.

Dependencies: RFC 0006 (telemetry), RFC 0014 Phase 4 (skill observability), RFC 0013 Phase 1 (PII detection).

### Phase 2: Spec Drafter & Registration Gate

Summary: Add the spec-drafter agent and the registration-gate extensions for deterministic skill specs. Phase 2 does not yet dispatch deterministic handlers at runtime — it only validates the shape of their specs.

Deliverables:

1. `agents/automation/drafter.py` — `skill-spec-drafter` agent role (`source: "authored"`, narrow skill set).
2. `config/agents.yaml` — register the drafter role with locked-down skill list.
3. `cli/src/commands/automation.rs` — `persatrix automation draft <candidate_id>`, `persatrix automation list`, `persatrix automation reject`.
4. `DeterministicHandler` dataclass added to `agents/skills/registry.py` (field optional on `SkillSpec`).
5. `schemas/skill.schema.json` extension for `implementation` and `ownership.maintainer_team`.
6. Registration-gate checks in `SkillRegistry.register()`: tests directory present, coverage ≥ floor, `reverse_callable` present when `reversible: true`, mutation static inspection.
7. Integration tests: drafter-produces-draft, gate-rejects-missing-tests, gate-rejects-mutation-without-reverse.

Dependencies: Phase 1, RFC 0014 Phase 1.

### Phase 3: Deterministic Dispatch & Sandboxing

Summary: Runtime dispatch of deterministic skills. This is when deterministic skills are actually executed in production.

Deliverables:

1. `agents/skills/executor.py` — `SkillExecutor` dispatcher; branches on `spec.implementation`.
2. `agents/automation/sandbox_runner.py` — `DeterministicSkillRunner` with CPU/memory/network/filesystem enforcement via RFC 0009 primitives.
3. `agents/automation/context.py` — narrow `SkillContext` surface (tool invocations + logger; no LLM, no state-store write, no registry mutation).
4. Registry-first discipline in `agents/base.py` task handler (fingerprint → `SkillRegistry.find_matching` → dispatch).
5. `skill_deterministic_hit`, `skill_deterministic_miss`, `skill_deterministic_bypass` telemetry events.
6. Failure-mode wiring: sandbox denial → `CAPABILITY_GAP`; timeout → `TIMEOUT`; schema issues → `SCHEMA_MISMATCH`; allowed-host unreachable → `DEPENDENCY_DOWN`.
7. Integration tests: hit/miss/bypass paths; sandbox denial for disallowed network; reverse invoked on postcondition failure.

Dependencies: Phase 2, RFC 0009 Phases 3–4 (sandbox primitives, HITL gates), RFC 0014 Phases 2–3.

### Phase 4: Lifecycle Governance & Audit

Summary: Auto-suspension, usage-based deprecation, ownership enforcement, full audit wiring, and governance telemetry dashboards.

Deliverables:

1. Auto-suspension on `UNHEALTHY` transition in `SkillRegistry`; operator re-enable flow.
2. Usage-based deprecation scan (daily): `skill_unused` emission at 90 days, auto-deprecation PR generation at 120 days.
3. Full audit event emission (all events from §J) via RFC 0009 `AuditLogger`.
4. `ownership.maintainer_team` enforcement at registration; rollback audit via `automation_skill_rolled_back`.
5. `persatrix automation governance` CLI command — surfaces pending triage, suspended skills, rollback history, ownerless entries.
6. Governance telemetry: candidate→promotion funnel rate, cost savings delta (RFC 0006 aggregation), suspension frequency per owner.
7. Integration tests: suspension on threshold breach; usage-based deprecation end-to-end; ownership rejection at load time.

Dependencies: Phase 3, RFC 0009 all phases, RFC 0014 Phase 3.

---

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Go orchestrator | `internal/automation/detector.go` (new) | Pattern detection background task (Phase 1) |
| Go orchestrator | `internal/automation/candidate.go` (new) | Candidate data model + store interface (Phase 1) |
| Go orchestrator | `internal/automation/store_memory.go` (new) | InMemoryAutomationCandidateStore (Phase 1) |
| Go orchestrator | `internal/automation/hash.go` (new) | Structural hashing of skill I/O (Phase 1) |
| Go orchestrator | `internal/automation/governance.go` (new) | Auto-suspension + usage deprecation scan (Phase 4) |
| Go orchestrator | `internal/server/automation.go` (new) | REST endpoints for candidates (Phase 1) |
| Go orchestrator | `internal/server/server.go` | Wire automation routes (Phase 1) |
| Python agents | `agents/automation/__init__.py` (new) | Package root |
| Python agents | `agents/automation/base.py` (new) | `DeterministicSkillHandler` protocol + `SkillContext` (Phase 3) |
| Python agents | `agents/automation/drafter.py` (new) | Spec-drafter agent role (Phase 2) |
| Python agents | `agents/automation/sandbox_runner.py` (new) | Sandboxed dispatcher (Phase 3) |
| Python agents | `agents/automation/context.py` (new) | Narrow SkillContext implementation (Phase 3) |
| Python agents | `agents/automation/impl/` (new dir) | Human-authored deterministic handlers |
| Python agents | `agents/skills/registry.py` | `DeterministicHandler` field + registration gates (Phases 2–3) |
| Python agents | `agents/skills/executor.py` (new) | Dispatcher branching on `implementation` (Phase 3) |
| Python agents | `agents/base.py` | `report_pattern` hook + registry-first discipline (Phases 1, 3) |
| Rust CLI | `cli/src/commands/automation.rs` (new) | `automation draft/list/reject/governance` (Phases 2, 4) |
| Rust CLI | `cli/src/main.rs` | Register automation subcommand |
| Protos | `proto/agent.proto` | Optional `pattern_flag` event (see Open Question 1) |
| Config | `config/automation.yaml` (new) | Detection thresholds, sandbox profile defaults |
| Config | `config/agents.yaml` | `skill-spec-drafter` role registration (Phase 2) |
| Schemas | `schemas/automation.schema.json` (new) | Schema for `config/automation.yaml` |
| Schemas | `schemas/skill.schema.json` | `implementation` field + `ownership.maintainer_team` (Phase 2) |
| Tests | `tests/unit/go/automation_detector_test.go` (new) | Detection/scoring/variance |
| Tests | `tests/unit/python/test_skill_executor.py` (new) | Dispatcher branching |
| Tests | `tests/unit/python/test_sandbox_runner.py` (new) | Sandbox enforcement |
| Tests | `tests/integration/test_automation_pipeline.py` (new) | End-to-end: candidate → draft → handler → sandbox |

## Test Strategy

- **Unit tests (Go)**: structural hashing is deterministic across equivalent inputs; scoring bounds; variance calculation; candidate deduplication; auto-suspension trigger; usage-based deprecation scan.
- **Unit tests (Python)**: `DeterministicHandler` protocol adherence; registration gate rejections (tests missing / failing / coverage below floor / reverse missing / mutation without reverse); drafter produces YAML matching schema; `SkillContext` does not expose forbidden surfaces.
- **Integration tests**: detector emits candidate from seeded telemetry → CLI triages → drafter produces spec → PR merged → handler registered → sandbox-executed → telemetry updated; postcondition failure invokes `reverse`; sandbox denial produces `CAPABILITY_GAP`; health decay auto-suspends routing.
- **Validation tests**: `make validate` accepts well-formed `config/automation.yaml` and extended `config/skills.yaml`; rejects deterministic skills without `maintainer_team`, without tests, or with mutation+`reversible: false`.
- **Manual tests**: operator triage flow via CLI; rollback of a registered deterministic skill to prior version; usage-based deprecation over a seeded 90-day window.
- **Regression**: existing RFC 0014 LLM skill tests unchanged (RFC 0015 is strictly additive).

## Open Questions

1. **Proto change scope for `PatternFlagEvent`**: should pattern flags be a new proto event or piggyback on the existing telemetry envelope as a tagged payload? **Proposed default**: tagged payload in Phase 1 (`metadata["_pattern_flag"]`), consistent with RFC 0008/0014 open-question resolutions. A typed proto field is added alongside RFC 0014's typed-field migration.

2. **Sandbox profile inheritance**: should deterministic skills inherit the invoking agent's permissions (tightest) or have their own declared profile (clearest)? **Proposed default**: `spec.implementation.sandbox_profile` is the floor; intersected with the invoking agent's permissions at dispatch time (never widened). This mirrors RFC 0010's sub-agent capability narrowing.

3. **Registry-first enforcement level**: should registry-first be advisory (telemetry only) or mandatory (LLM path returns a policy error if a healthy deterministic match exists)? **Proposed default**: advisory in Phase 3; mandatory only behind a per-agent config flag in Phase 4 after bypass telemetry has baselined. Premature mandation risks breaking tasks that genuinely need reasoning.

4. **Candidate TTL and suppression window**: rejected candidates are suppressed for 90 days per §C. Is 90 days correct, or should suppression be permanent with explicit un-reject? **Proposed default**: 90 days in Phase 1. Permanent rejection is a stronger claim than the operator has evidence for at triage time; the pattern may become automation-worthy later as inputs evolve.

5. **Drafter agent trust level**: the drafter agent produces YAML that humans review. Should it run in the same sandbox profile as a `persona` agent or a tightened profile given it only reads telemetry? **Proposed default**: tightened profile with no network, no filesystem write outside `drafts/`, no LLM tool-calling (only text generation). The drafter is effectively a templating agent, not an interactive one.

6. **High-stakes auto-detection accuracy**: the drafter auto-sets `high_stakes: true` when observed tool calls match RFC 0009's high-risk classification. What false-negative rate is acceptable before we require human setting? **Proposed default**: ship with auto-detection in Phase 2; review telemetry after 30 days of triage. If any high-risk automation reaches production without `high_stakes`, fail-closed by requiring human sign-off for the field.

7. **Candidate store durability and backend**: `AutomationCandidate` records must survive orchestrator restarts to avoid re-discovering patterns from scratch. Should the store use the existing workflow state store (RFC 0005 — already available), a dedicated time-series backend, or the metrics store from RFC 0006? **Proposed default**: wrap the existing state store interface in Phase 1 (`InMemoryAutomationCandidateStore` for tests, a file-backed or DB-backed implementation for production); promote to a dedicated store only if Phase 3's multi-node scenarios require it. The `AutomationCandidateStore` interface defined in §C already isolates this decision — the backend can be swapped without touching detection or promotion logic.

8. **Static mutation inspection method**: §E requires registration-time rejection of `reversible: false` handlers that call mutating tools, but the inspection mechanism is unspecified. Options: (a) AST-based import scanning (checks `from persatrix_agents.tools import <mutating-tool>`), (b) a whitelist of known-mutating callable names resolved via `ast.walk`, or (c) a runtime dry-run in a sandboxed no-op mode that intercepts tool calls. **Proposed default**: AST-based import scanning in Phase 3 (simplest, no runtime dependency); the whitelist of mutating tools is maintained alongside the tool registry and updated when new tools are added. Option (c) is deferred to a future RFC if false-negative rates from static analysis prove unacceptable.

## Decision / Next Steps

This RFC proposes the pattern-extraction and deterministic-automation pipeline that RFC 0014 Open Question 4 explicitly defers. The design is strictly additive to RFC 0014 and has no impact on v0.2.0 or v0.3.0 scope.

Next steps:

1. Resolve Open Questions 1 (proto scope), 2 (sandbox inheritance), and 3 (registry-first enforcement level) before Phase 2 implementation begins.
2. Create a PR plan file for RFC 0015 after acceptance.
3. Phase 1 can begin once RFC 0006 telemetry + RFC 0013 Phase 1 (PII detection) + RFC 0014 Phase 4 (skill observability) are merged. In practice this means Phase 1 starts during v0.5.0 opening.
4. Phase 2 requires RFC 0014 Phase 1 (registry) and is otherwise independent.
5. Phase 3 requires RFC 0009 Phases 3–4 (sandbox primitives, HITL gates) and RFC 0014 Phase 3 (lifecycle + `SkillGrant`).
6. Update RFC 0014 §H and Open Question 4 to reference this RFC as the resolution (documentation-only follow-up PR).

## Related Documentation

- [RFC 0006](0006-efficiency-execution-limits.md) — Telemetry pipeline consumed by detection
- [RFC 0008](0008-agent-memory-context-optimization.md) — Context packaging referenced by drafter inputs
- [RFC 0009](0009-security-sandboxing.md) — Sandbox primitives, audit logger, HITL gates
- [RFC 0013](0013-legal-ethical-compliance.md) — PII detection required for candidate persistence
- [RFC 0014](0014-agent-skill-registry-lifecycle.md) — Skill registry this RFC extends; resolves Open Question 4
- RFC 0010 (Sub-Agent Spawning, not yet written) — Sandbox narrowing pattern reused here
- [Roadmap](../../ROADMAP.md)
- [Architecture Spec](../ai-agents-orchestration-spec.md)
