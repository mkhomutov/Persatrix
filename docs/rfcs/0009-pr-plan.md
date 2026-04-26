# RFC 0009 — PR Implementation Plan (Phases 1–2 — v0.3.0 scope) (scaffold)

**RFC**: [0009-security-sandboxing.md](0009-security-sandboxing.md)
**Created**: 2026-04-25
**Branch prefix**: `feature/v030-rfc0009-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.0-plan.md Phase 1 (combined plans PR)](../v0.3.0-plan.md#phase-1--author-the-six-rfc-pr-plans)

> **Status**: 🔨 Scaffold — PR rows have branch names, scopes, and dependency links pinned, but per-PR key-implementation-detail and tests sections are placeholders. Flesh out before the first implementation PR opens.

---

## Overview

RFC 0009 spans four phases. **Only Phases 1–2 land in v0.3.0**: audit logging + rate limiting + secret redaction (Phase 1), and input sanitization + provenance tagging (Phase 2). Phases 3–4 (tool output validation, agent identity tokens, HITL gates) are deferred to v0.4.0 per the [RFC's Phased Implementation Plan](0009-security-sandboxing.md#phased-implementation-plan).

This plan splits Phases 1–2 into **4 PRs**.

> **Estimate calibration**: 1.7× factor.

**Prerequisite**: none (fills existing `internal/security/security.go` TODO stubs).

**Cross-RFC sequencing**: independent workstream — runs throughout v0.3.0 with no blocking dep on other v0.3.0 RFCs. Two integration points:
- **PR 2 (RateLimiter middleware)** must merge before [RFC 0011 PR plan](0011-pr-plan.md) PR 2 (REST channel publish endpoint) — see [RFC 0011 §"Phase 1 Dependencies"](0011-channels-bridges.md#phase-1-channel-store-and-rest-routing) for the rate-limit-as-DoS-vector rationale.
- **PR 3 (InputSanitizer wired into channel message storage)** integrates at [RFC 0011 PR plan](0011-pr-plan.md) PR 5 (Phase 3).

---

## Dependency Graph

```
PR 1 (Phase 1a — AuditLogger + SecretRedactor)
  ↓
PR 2 (Phase 1b — RateLimiter + middleware integration)
  ↓
PR 3 (Phase 2 — InputSanitizer + ContextItem + provenance tagging)
  ↓
PR 4 (Review follow-ups + RFC partial-close — Phases 1–2 scope only)
```

---

## PR Sequence

### PR 1: `feature/v030-rfc0009-audit-redactor` — Phase 1a: AuditLogger + SecretRedactor

**Depends on**: Nothing.
**Estimated size**: ~400–500 lines.

#### Scope (high-level)

- `internal/security/audit.go` — `AuditLogger`: structured, append-only file sink with checksum chain + correlation ID.
- `internal/security/redactor.go` — `SecretRedactor`: pattern registry, string + struct redaction.
- Wire `SecretRedactor` into `AuditLogger` output.
- Wire `AuditLogger` into orchestrator: agent registration, token events, capability violations.

#### Key implementation details *(TBD)*
#### Tests *(TBD)*

#### PR checklist

- [ ] ROADMAP.md row for RFC 0009 → `🚧 Implementing`
- [ ] Master Progress Overview row 5 → 🔄 In progress

---

### PR 2: `feature/v030-rfc0009-rate-limiter` — Phase 1b: RateLimiter + Middleware

**Depends on**: PR 1.
**Estimated size**: ~350–500 lines.

#### Scope (high-level)

- `internal/security/ratelimit.go` — `RateLimiter`: per-agent sliding-window counter + circuit-breaker flag.
- HTTP middleware adapter for REST endpoint enforcement.
- Wire into executor: check on every tool dispatch call.
- Config field `security.rate_limit_enforced` (default `true`); CLI mirror.
- Startup `WARN` when disabled — per [RFC 0011 §Phase 1 Dependencies](0011-channels-bridges.md#phase-1-channel-store-and-rest-routing).

#### Key implementation details *(TBD)*
#### Tests *(TBD)*

#### PR checklist

- [ ] Middleware ready for [RFC 0011 PR plan](0011-pr-plan.md) PR 2 to consume
- [ ] Startup-warn path covered by integration test

---

### PR 3: `feature/v030-rfc0009-input-sanitizer` — Phase 2: InputSanitizer + Provenance

**Depends on**: PR 2.
**Estimated size**: ~400–500 lines.

#### Scope (high-level)

- `internal/security/sanitize.go` — `InputSanitizer`: pattern registry, `Sanitize()`, audit emission.
- `agents/security.py` — `ContextItem` wrapper: `source`, `sanitized` fields; `<external_data>` prompt delimiters.
- Wire sanitization into `http_request` and `file_read` tool result paths.
- Add provenance metadata to `TaskRequest.context` (no proto change).
- Update agent system prompt templates with external-data handling instructions.

#### Key implementation details *(TBD)*
#### Tests *(TBD)*

#### PR checklist

- [ ] Sanitizer ready for [RFC 0011 PR plan](0011-pr-plan.md) PR 5 to consume
- [ ] No regression on existing tool tests

---

### PR 4: `feature/v030-rfc0009-close` — Review Follow-Ups + Phases 1–2 Close

**Depends on**: PR 3.
**Estimated size**: ~100–250 lines.

| File | Change |
|------|--------|
| `docs/rfcs/0009-security-sandboxing.md` | Phases 1–2 status → `⚠️ Partially Implemented` (Phases 3–4 remain for v0.4.0). |
| `ROADMAP.md` | RFC 0009 row → `⚠️ Partially Implemented (Phases 1–2)`. |
| `docs/v0.3.0-plan.md` | Master Progress Overview row 5 → ✅. |

CHANGELOG.md is **deferred to v0.3.0 release prep** (Phase 4 PR 3).

#### PR checklist

- [ ] All deferred review findings addressed or downgraded
- [ ] `make test` passes; `make lint` clean

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| PR 2 rate-limiter middleware lands after RFC 0011 PR 2 (publish endpoint), leaving DoS vector | Cross-RFC sequencing pin in this plan's Overview; RFC 0011 PR 2 review gate cites this PR's number. If PR 2 slips, RFC 0011 ships startup-WARN path until it merges. |
| Audit log checksum chain corruption on crash | Append-only with `fsync` per write; recovery test in PR 1. |
| InputSanitizer false positives drop legitimate content | Pattern registry is explicit (no LLM-judge); audit events surface every drop for operator review. |
| Phase 1–2 status flip suggests "security is done" | Status flips to `⚠️ Partially Implemented`, not `✅ Implemented`. v0.3.0 release notes call out P3–4 deferral. |

---

## ROADMAP Hygiene

- **PR 1 opens** → ROADMAP RFC 0009 → `🚧 Implementing`; Master Progress Overview row 5 → 🔄.
- **PR 4 merges** → ROADMAP RFC 0009 → `⚠️ Partially Implemented`; row 5 → ✅.

---

## Scaffold TODOs

Before opening PR 1:
- [ ] Fill in "Key implementation details" + "Tests" for each PR.
- [ ] Pin estimated sizes against the RFC's Files Touched table.
