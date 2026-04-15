# Documentation Guide

## Context

You are updating documentation for **Persatrix** — a polyglot AI agent orchestration framework built with Go (orchestrator), Python (agents), and Rust (CLI). The project maintains documentation across architecture, specs, and configuration.

## Core Principles

When updating documentation:

1. **Accuracy First**: Documentation must accurately reflect the current codebase state
2. **Security Focus**: Security-related docs require extra scrutiny (agent permissions, network boundaries)
3. **Cross-Reference Integrity**: Updates may require changes to multiple related documents
4. **Consistency**: Use standardized terminology
5. **Completeness**: Update all affected docs in the same change, not just one file

## Pre-Update Checklist

Before updating any documentation, verify:

- [ ] **Current Code State**: Read the actual implementation files referenced in the doc
- [ ] **Related Docs**: Identify all docs that reference the same feature/component
- [ ] **Cross-References**: Note all internal doc links that may need updating

## Documentation Categories

### Specification Documents
**Files**: `docs/ai-agents-orchestration-spec.md`, `docs/persatrix-extension-spec.md`

When updating:
- These are the canonical design specs — update when behavior changes
- Keep security model descriptions accurate
- Cross-reference with audit doc for known gaps

### Architecture Documentation
**Files**: `docs/ai-agents-orchestration-spec.md` (architecture sections)

When updating:
- Verify component boundaries are accurately described
- Update data flow descriptions if gRPC/REST contracts change
- Check that module organization matches actual directory structure

### Configuration Documentation
**Files**: `config/agents.yaml`, `config/optimization.yaml`, `config/mcp-servers.yaml`

When updating:
- Validate against JSON schemas in `schemas/`
- Run `make validate` after changes
- Document permission changes carefully (deny-by-default model)

## Status Markers

Use these standardized markers consistently:

| Marker | Meaning |
|--------|---------|
| ✅ **Implemented** | Feature is complete and tested |
| 🚧 **In Progress** | Currently being worked on |
| ⚠️ **Partial** | Partially implemented |
| 📋 **Planned** | Designed but not yet started |
| 🔮 **Future** | Post-current-phase roadmap item |

## Ownership Map

| Topic | Canonical Document |
|-------|-------------------|
| Core architecture & API | `docs/ai-agents-orchestration-spec.md` |
| Extension features (personas, memory, channels) | `docs/persatrix-extension-spec.md` |
| Spec gaps & audit | `docs/persatrix-spec-audit.md` |
| Branching strategy | `docs/BRANCHING.md` |
| Agent configuration | `config/agents.yaml` |
| Workflow definitions | `workflows/*.yaml` |
| Protobuf contracts | `proto/*.proto` |
| CI workflow details | `CONTRIBUTING.md` |
| Security policy | `SECURITY.md` |
| RFC design decisions | `docs/rfcs/` |

## Historical Artifacts Policy

- ❌ **Do not** keep deprecated CI workflows, scripts, or config files — delete once replacements are active
- ❌ **Do not** leave stale status markers — update when work is done
- ❌ **Do not** keep empty placeholder directories — either populate or remove them
- ✅ **Do** consolidate any unique information into the canonical document before deleting
- ✅ **Do** update all cross-references when removing an artifact

## Size Limits

| Scope | Limit | Rationale |
|-------|-------|-----------|
| **Code files** (`.go`, `.py`, `.rs`, `.toml`, `.yaml`) | **≤ 500 lines** | Effective code review |
| **Documentation files** (`.md`) | **≤ 3 000 words** | Thorough doc review |

When a file approaches or exceeds its limit, **split it** into focused, single-responsibility modules.
