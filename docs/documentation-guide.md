# Documentation Guide

## Context

You are updating documentation for **Persatrix** — a polyglot AI agent orchestration framework built with Go (orchestrator), Python (agents), and Rust (CLI). The project maintains documentation across architecture, specs, and configuration.

## Writing Style

**Applies to every doc and every code comment — new or changed.**

Persatrix is read by people who are not all programmers: developers at any
level, founders sizing it up for a business, researchers using it for a study,
and brand-new teammates on day one. Write so any of them can follow along.

- **Plain English.** Short sentences, everyday words. Spell out a technical
  term the first time you use it, or link the [glossary](ai-glossary.md).
- **Lead with the point.** Say what the thing does and why it matters in the
  real world before explaining how it works inside.
- **Cut everything optional.** If a sentence still makes sense without a word,
  delete the word. Drop filler like "simply", "just", "of course".
- **Show it.** One real example or command beats a paragraph describing it.

One test before you commit: *could someone who has never seen this code tell
what it's for and why it's useful?* If not, rewrite.

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
| 🚀 **Stable** | Proven in production (RFC lifecycle) |
| 🚧 **In Progress** | Currently being worked on (phase-level) |
| 🚧 **Implementing** | Implementation in progress (RFC lifecycle) |
| ⚠️ **Partial** | Partially implemented |
| ⚠️ **Partially Implemented** | Some phases complete, others remain (RFC lifecycle) |
| 📋 **Planned** | Designed but not yet started |
| 📋 **Proposed** | Complete and open for review (RFC lifecycle) |
| 🔮 **Future** | Post-current-phase roadmap item |

This is the full set `scripts/checks/doc_status_markers.py` accepts; the RFC
lifecycle markers are defined in [rfcs/README.md](rfcs/README.md#rfc-lifecycle).

## Ownership Map

One canonical document per topic. Link to it; do not copy from it.

| Topic | Canonical Document |
|-------|-------------------|
| Core architecture & API | `docs/ai-agents-orchestration-spec.md` |
| Extension features (personas, memory, channels) | `docs/persatrix-extension-spec.md` |
| Spec gaps & audit | `docs/persatrix-spec-audit.md` |
| Architecture diagrams | `docs/diagrams/` (one file per diagram; conventions in [development-workflow.md §Phase 7](development-workflow.md#phase-7--documentation--diagrams)) |
| Terminology — product | `docs/ai-glossary.md` |
| Terminology — process | `docs/methodology/process-glossary.md` |
| How a release is made | `docs/methodology/release-cycle.md` |
| Scope locks, cuttable items, amendments | `docs/methodology/decisions.md` |
| PR review | `docs/methodology/review-process.md` |
| Testing layers and rules | `docs/methodology/testing-strategy.md` |
| Which rules are enforced, and how | `docs/methodology/enforcement-matrix.md` |
| Make targets, scripts, hooks, workflows | `docs/methodology/automation-catalogue.md` |
| RFC-level lifecycle | `docs/development-workflow.md` |
| RFC format and lifecycle; live RFC table | `docs/rfcs/README.md`; `docs/rfcs/INDEX.md` (generated) |
| Internal issue tracker; live issue table | `docs/issues/README.md`; `docs/issues/INDEX.md` (generated) |
| Manual tests index and conventions | `docs/manual-tests/README.md` |
| Version status, RFC master index, merged PRs | `ROADMAP.md` |
| Version scope decisions | `docs/v0.3.x-sequencing.md` (active amendment at the top) |
| One version's plan / release-prep / checklist / evidence | `docs/vX.Y.Z-*.md` and `docs/manual-tests/vX.Y.Z-execution-report.md` |
| Operator and user guides | `docs/guides/` (auth, channels, epochs, model providers, persona agents, sessions, web console, version bump, demo) |
| Log schema and observability | `docs/observability.md` |
| Golden-trace evals | `docs/evaluators-guide.md`; `evaluators/eval_sets/README.md` |
| Prompt assets | `docs/prompt-organization.md` |
| Open-core licence tiers | `docs/open-core-reserved-seams.md` (policy in RFC 0045) |
| Companion discussion documents (spawn RFCs, own no code) | `docs/memory-quality-roadmap.md`, `docs/storage-architecture-roadmap.md`, `docs/agent-runtime-vocabulary-roadmap.md`, `docs/memory-scope-axes.md` |
| Branching, commits, PR titles | `docs/BRANCHING.md` |
| Agent configuration | `config/agents.yaml` |
| Workflow definitions | `workflows/*.yaml` |
| Protobuf contracts | `proto/*.proto` |
| Contributor setup and quality gates | `CONTRIBUTING.md` |
| Security policy | `SECURITY.md` |
| Agent working rules | `.github/CLAUDE.md`, `.github/copilot-instructions.md`, `.github/instructions/*.md` |

## Where Documents Live, and When They Freeze

| Kind | Path | Lifecycle |
|------|------|-----------|
| Standing reference (specs, guides, methodology, glossaries) | `docs/`, `docs/guides/`, `docs/methodology/`, `docs/diagrams/` | Edited whenever the thing they describe changes; `Last updated` bumped |
| RFCs and their PR plans | `docs/rfcs/NNNN-*.md`, `docs/rfcs/NNNN-pr-plan.md` | Live until `✅ Implemented`; then edited only for divergence notes and status |
| Issues | `docs/issues/ISSUE-NNNN-*.md` | Live until `resolved`; resolved files stay for `grep` |
| Manual tests | `docs/manual-tests/MT-*.md` | Live; versioned (`v1.1`, `v1.2`) when a leg changes |
| Version-cycle documents | `docs/vX.Y.Z-plan.md`, `-scope-locks.md`, `-plan-amendment-*.md`, `-release-prep-plan.md`, `-release-baseline.md`, `-release-checklist.md` | Edited during the cycle; **frozen at the post-release follow-up** except for the Released stamp |
| Release evidence | `docs/manual-tests/vX.Y.Z-execution-report.md` | Written once against the tag; never edited after |

**Archival rule.** A version-cycle document is *archived* when its version's
tag exists and the post-release follow-up has stamped it Released. Archived
documents are not moved (every plan is linked from ROADMAP, issues, and later
plans, and moving them would break those links); they are frozen in place.
Frozen release documents are **release evidence**, so they are exempt from the
word cap: execution reports and checklists are already excluded by pattern in
`scripts/checks/file_size.py`, and plans / release-prep plans of **released**
versions should be too — today they are grandfathered one by one with an exit
condition nothing can execute ([ISSUE-0139](issues/ISSUE-0139-released-plans-have-no-archival-mechanism.md)).
Only the **open** cycle's plan needs an allowlist entry.

## Historical Artifacts Policy

- ❌ **Do not** keep deprecated CI workflows, scripts, or config files — delete once replacements are active
- ❌ **Do not** leave stale status markers — update when work is done
- ❌ **Do not** keep empty placeholder directories — either populate or remove them
- ✅ **Do** consolidate any unique information into the canonical document before deleting
- ✅ **Do** update all cross-references when removing an artifact
- ✅ **Do** keep release evidence (execution reports, checklists, released plans) — these are records, not deprecated artifacts; freeze them, never trim them (see [Where Documents Live](#where-documents-live-and-when-they-freeze))

## Size Limits

| Scope | Limit | Rationale |
|-------|-------|-----------|
| **Code files** (`.go`, `.py`, `.rs`, `.toml`, `.yaml`) | **≤ 500 lines** | Effective code review |
| **Documentation files** (`.md`) | **≤ 3 000 words** | Thorough doc review |
| **RFCs** (`docs/rfcs/*.md`) | **≤ 8 000 words** | Design documents carry required sections the prose cap would punish |

When a file approaches or exceeds its limit, **split it** into focused,
single-responsibility modules — move a stable half to its own file rather
than deleting rationale to make room ("split, don't trim"). Split at about
485 lines or 2 900 words, before the cap arrives: a file *at* the cap turns
every later fix into deleted context.

**How words are counted.** `scripts/checks/file_size.py` strips YAML
front-matter and fenced code blocks before counting, so `wc -w` over-reports.
Measure with the tool:

```bash
python -c "from scripts.checks.file_size import _count_words; print(_count_words(open('docs/some-file.md').read()))"
```

The checker also prints a **near-cap** notice for files within 3 % of their
limit. Files that already exceeded a cap when the gate was introduced are
listed in `scripts/checks/file_size_allowlist.py`, each with a reason and an
exit condition; execution reports, checklists, generated indexes, and the
notices file are excluded by pattern because their length is data, not prose.
Which of these rules are enforced, and where, is in the
[enforcement matrix](methodology/enforcement-matrix.md).
