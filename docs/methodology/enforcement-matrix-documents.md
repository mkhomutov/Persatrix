# Enforcement Matrix: Size, Documents and Process

> **Last updated**: 2026-11-12
> The second half of the [enforcement matrix](enforcement-matrix.md): the
> rules about file size and shape, about documents, and about how work is
> planned and merged. Split out on 2026-11-12, when the matrix reached its
> word cap; the rules for code and builds stay there, and so does the
> meaning of each level in the **Enforcement** column
> ([Enforcement levels](enforcement-matrix.md#enforcement-levels)).

## Size and shape

| Rule | Stated in | Check | Enforcement |
|------|-----------|-------|-------------|
| Code files ≤ 800 lines | documentation-guide §Size Limits | `file_size.py --strict` | Required (`File size check`) + Pre-commit |
| A code file over 500 lines is warned, and split at a real seam when a change edits it for another reason — never trimmed to fit, never in a sweep (sequencing Amendment 2026-09-12, ruling (e)) | documentation-guide §Size Limits; release-cycle §The debt sweep (retired) | `file_size.py` lists it under `[WARN]` on every run; the split is review's call | Advisory output + Convention |
| Docs ≤ 3 000 words; RFCs ≤ 8 000 words | documentation-guide | same | Required (`File size check`) + Pre-commit |
| Grandfathered files carry a reason and an exit condition | `file_size_allowlist.py` docstring | review; `test_allowlist_has_no_dead_entries`, `test_allowlist_entries_are_not_already_excluded` | Convention + unit tests |
| Released version-cycle docs are frozen evidence, exempt from the cap | documentation-guide §Where Documents Live | `file_size.py` excludes them once `CHANGELOG.md` has the version's dated heading (ISSUE-0139; read from the tree, not `git tag`, so a depth-1 checkout agrees with a full clone); a still-allowlisted released doc prints `[STALE-ALLOWLIST]` (advisory; the tag PR drops an open plan's last-resort entry) | Required (`Python` unit tests pin it) |
| Near-cap notice at 3 % of each limit | `file_size.py` | `--near-cap` output on every run | Advisory output |
| PRs under 500 changed lines | CONTRIBUTING; BRANCHING; copilot-instructions | — | **Guidance, stated as such** since the BRANCHING rewrite — a third of merges exceed it, almost all docs-heavy; code PRs split |
| Squash merge; linear history | BRANCHING | branch protection | Required |
| Branch naming (`feature/vNNN-…`, `docs/`, `fix/`, `ci/`) | BRANCHING; each plan's header | — | Convention |

## Documentation

| Rule | Stated in | Check | Enforcement |
|------|-----------|-------|-------------|
| No broken relative links or anchors in tracked markdown | documentation-guide; consistency checklist | `doc_links.py` | Required (`Docs hygiene`) + Pre-commit |
| Only the standard status markers | documentation-guide §Status Markers | `doc_status_markers.py` | Required (`Docs hygiene`) + Pre-commit |
| No leaked tool-call markup in docs | — | `doc_leaked_markup.py` | Required (`Docs hygiene`) + Pre-commit |
| `FILEMAP.md` matches `git ls-files` | `generate_filemap.py` | `--check` (date-insensitive; on a PR it compares against the merge tree, so a PR behind a file-adding merge fails until updated) | Required (`Docs hygiene`) + Pre-commit regenerates — closed [ISSUE-0133](../issues/ISSUE-0133-no-ci-gate-on-filemap-freshness.md) |
| Merged-PR history (`docs/merged-prs.md`) matches the squash log, allowing only the newest merges to be missing | automation-catalogue | `scripts/merged_prs.py --check` | Required (`Docs hygiene`) + Pre-commit regenerates (a shallow clone may skip) |
| No plan row says "PR open" for a PR `docs/merged-prs.md` records, or "not started" for a merged PR | release-cycle §Phase 1 | `scripts/checks/plan_status.py` (`make plan-status-check`) | Required (`Docs hygiene`) + Pre-commit flips it, check skipped when shallow — first run found ten stale rows; judging an unlinked 🔀 row by the commit that wrote it found eight more |
| A patch release keeps one document: no `docs/vX.Y.Z-*.md` file beside its plan but a test-findings PR plan | release-cycle §Phase 0 (sequencing Amendment 2026-09-12, ruling (e)) | `scripts/checks/plan_status.py` (`make plan-status-check`) fails one for any `X.Y.Z` after 0.3.16 with `Z` above 0, dated or not; v0.3.16 and earlier keep their files, a minor release is not judged | Required (`Docs hygiene`) + Pre-commit |
| Every artifact the methodology names exists (documents, tools, make targets, Docs-hygiene steps) | [conformance.json](conformance.json) | `make conformance-check` | Required (`Docs hygiene`) + Pre-commit |
| No ROADMAP Component Status row says less than the RFC it names | ROADMAP §How to Update | `scripts/checks/roadmap_status.py` (`make roadmap-status-check`) | Required (`Validate configs`) + Pre-commit — its first run found `internal/security/` still "In progress" four months after RFC 0009 closed part-way |
| The Go, Python and Rust minimums the setup docs state are the ones the build requires: the `go` line in `go.mod`, `requires-python` in `agents/pyproject.toml`, `rust-version` in `cli/Cargo.toml` | README; CONTRIBUTING; `cli/Cargo.toml` | `test_toolchain_minimums.py` reads every "Go 1.26+"-style minimum in README (badges and quick start), CONTRIBUTING, the guides, the manual-test template and the manual tests, the bug form's example, and the CLI's Python message, and fails when a main setup doc stops stating one it can read. Records of their time (execution reports, the CHANGELOG, RFCs, issues) are left alone | Required (`Python`) — since 2026-09-26; README had said Go 1.24+ and Rust 1.80+ while the build needed 1.26 and 1.86 |
| Unified doc audit (links + markers + sizes) | `doc_audit.py` | — | Local convenience wrapper; its three checks run individually in CI |
| Local-only files never referenced from committed files | CLAUDE.md; copilot-instructions; review-process | review | Convention |
| Glossary terms mandatory; new terms added in the same change | CLAUDE.md §Terminology | review | Convention |
| Plain English; lead with the point | documentation-guide §Writing Style | review | Convention |
| Status hygiene before and after every task | ROADMAP §How to Update; CLAUDE.md | review; ROADMAP's Component Status rows also by `roadmap_status.py` (above) | Convention; Required for the Component Status rows |
| Every RFC has front-matter, required sections, ToC | rfcs/README checklist | `rfcs-check` (front-matter and its `**Status**` header line only) | Required for front-matter; Convention for sections |

## Process

| Rule | Stated in | Check | Enforcement |
|------|-----------|-------|-------------|
| Every PR reviewed; findings dispositioned | review-process | — | Convention (0 GitHub approvals required) |
| Migrations land ahead of their consumer, one store per PR | release-cycle | review | Convention |
| Scope locks change only by amendment | decisions | review | Convention |
| Every sequencing amendment since 2026-09-12 records its external evidence | decisions rule 6 | `scripts/checks/amendment_evidence.py` (`make amendment-evidence-check`): the section and its five rows are present, filled and given once, and no heading from that date names an amendment in another form (a unit test pins the CI step and the make target, which the conformance manifest no longer does); what the amendment concludes from them is review's call | Required (`Docs hygiene`) + Pre-commit |
| Live arc runs once, live, before the tag | release-cycle | the plan's release checklist | Convention, evidenced in the report |
| Version strings aligned across five files | version-bump guide | `make bump-version` | Manual in the tag PR |
| TDD for new unit-level code | CLAUDE.md §TDD | review | Convention |
| Version-train gate | release-cycle | review | Convention |
| PR body follows What / Why / How / Not in this PR / Gates / Review | BRANCHING §Pull requests | `.github/PULL_REQUEST_TEMPLATE.md` pre-fills it | Template (GitHub applies it to every new PR) |

## Related documentation

- [enforcement-matrix.md](enforcement-matrix.md) — the rules for code and builds, and the levels
- [enforcement-gaps.md](enforcement-gaps.md) — what the matrix says the project should change
- [documentation-guide.md §Size Limits](../documentation-guide.md#size-limits)
