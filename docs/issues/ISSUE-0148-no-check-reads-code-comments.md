---
id: ISSUE-0148
summary: "No check reads code comments, so a placeholder package whose doc says it works, or a code comment with a long-past version tag, passes CI — the same defect took three PRs in one day (#899, #900, #903) and hand searches still missed cases; proposes a check that a Go package or Python module with no code says it is a placeholder, and later a rule for stale version tags"
status: open
severity: low
area: ci
created: 2026-09-10
refs:
  - scripts/checks/doc_status_markers.py
  - scripts/checks/doc_links.py
  - scripts/checks/released.py
  - scripts/pre_commit.py
  - .github/workflows/ci.yml
  - docs/methodology/automation-catalogue.md
  - docs/methodology/enforcement-matrix.md
  - internal/scheduler/stage_runner.go
  - cli/src/main.rs
  - evaluators/conversation_scorer.py
---

## Summary

Every documentation check reads Markdown only. A Go package or Python module
that holds nothing but comments can describe itself as working, and a code
comment can carry a version tag that went out of date long ago, without
failing any check. Found in the review of PR #903 (finding F-12).

## Context

- **What the checks read.** The documentation checks that run in CI and in
  the pre-commit hook look only at Markdown: `scripts/checks/doc_status_markers.py`
  collects `*.md` files and `scripts/checks/doc_links.py` lists `*.md` files.
  Ruff selects no TODO rules, and the golangci-lint set CI runs since
  v0.3.16 PR C1 ([ISSUE-0142](ISSUE-0142-ci-never-runs-golangci-lint.md))
  reads no comments either. Nothing reads Go, Python or Rust comments for
  what they claim.
- **The cost so far.** The same defect took three PRs on 2026-09-10: #899
  (the glossary described the MCP bridge as if it worked), #900 (the MCP
  placeholders' comments) and #903 (five Go packages, three Python modules
  and the CLI's help text). Each was found by hand, and the review of #903
  still found two cases its own search had missed: the `--help` text for the
  CLI's mesh commands and `evaluators/conversation_scorer.py`, which is
  outside `agents/`.
- **Stale version tags.** Go and Python code still holds 29 comments of the
  form `TODO(v0.x)` or `(v0.x+)` that name a version already released — for
  example `internal/scheduler/stage_runner.go`, "TODO(v0.2): evaluate step
  conditions", and the `agents/persona.py` docstring, "Persatrix Persona
  Agent Interface (v0.2+)".

## Impact

Readers of `go doc`, module docstrings and `--help` output keep being told
that planned code works, or that it is due in a release that has already
shipped. Each fix is a manual search that misses cases, so the problem comes
back.

## Proposed fix

Add one check to the existing documentation-check machinery — the `Docs
hygiene` CI job and `_CHECKS` in `scripts/pre_commit.py` — and list it in
`docs/methodology/automation-catalogue.md` and
`docs/methodology/enforcement-matrix.md`:

1. **Placeholder rule.** A Go package whose non-test files hold only comments
   and the package clause must have a package doc that says "placeholder". A
   Python module, other than an `__init__.py`, with nothing after its
   docstring must have a docstring that says "placeholder". Once #903 merges,
   this passes across the tree: six Go packages and three Python modules.
2. **Reverse rule.** A package or module whose doc opens by calling it a
   placeholder but which has code fails, so the doc changes when code lands.
   Match only the opening ("Package x is a placeholder", "Placeholder for"):
   a plain word match also hits unrelated uses, such as `internal/ui/ui.go`.
3. **Stale-tag rule, later.** Flag `TODO(v0.x)` and `(v0.x+)` in code once
   that version appears in `released_versions()` (`scripts/checks/released.py`).
   It needs a cleanup pass or an exemption list first, because of the 29
   existing hits.

## Notes

> 2026-09-10 — filed from the PR #903 review (F-12). That PR fixed the
> comments by hand; this issue is for the check that keeps them fixed.
>
> 2026-09-10 — the same stale tags also sit in design docs, which no check
> reads for them either: `docs/ai-agents-orchestration-spec.md` still heads
> its API lists "Distributed Mesh (v0.3)", "A2A Interop (v0.3)" and
> "Bridges (v0.2+)". A stale-tag rule could cover Markdown too; PR #903 left
> these headings alone.
>
> 2026-09-11 — PR #905 rewrote the `internal/state/state.go` TODO that the
> Context section quoted, so the example, the count and the refs now point
> at `internal/scheduler/stage_runner.go`. The 32 was counted before the
> #903 review removed the CLI's two `(v0.3+)` help lines, the only Rust
> hits. `git grep -nE 'TODO\(v0\.[0-9]+\)|\(v0\.[0-9]+\+\)' -- '*.go' '*.py' '*.rs'`
> now finds 30; one of them, the `(v0.5+)` in
> `agents/persona_runtime/action_loop.py`, names a release not yet shipped.
