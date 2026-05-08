#!/usr/bin/env python3
"""File size audit — check code and documentation files against review-friendly limits.

Scans code files and documentation files for excessive size. Files that
exceed the configurable thresholds are flagged so they can be reviewed for
splitting opportunities.

**Thresholds (defaults):**

- Code files: 500 lines
- Documentation files: 3 000 words

Usage::

    python scripts/checks/file_size.py [--max-code-lines 500] [--max-doc-words 3000] [--strict] [--verbose]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.checks import ensure_utf8_stdout, walk_files, DEFAULT_EXCLUDES  # noqa: E402

DEFAULT_MAX_CODE_LINES = 500
DEFAULT_MAX_DOC_WORDS = 3000
DEFAULT_MAX_RFC_WORDS = 8000

_RFC_PREFIX = "docs/rfcs/"

CODE_EXTENSIONS = [".go", ".py", ".rs", ".js", ".ts", ".yaml", ".toml"]
DOC_EXTENSIONS = [".md"]

_EXTRA_EXCLUDES = [
    "**/node_modules/**",
    "cli/target/**",
    "agents/generated/**",
    "internal/generated/**",
    # Local venvs (not present in CI but common during local runs).
    ".venv/**",
    "venv/**",
    ".notices-venv/**",
    # Generated dependency manifest; word count scales with the Go/Python/Rust
    # dependency graphs. Review it via `make notices` diff, not size limits.
    "THIRD_PARTY_NOTICES.md",
    # PR review reports are local-only working artifacts and are intentionally
    # not committed; local copies should not block repo-wide size checks.
    "docs/pr-reviews/**",
    # Git worktrees are checked out under .claude/worktrees/ and mirror the
    # full repo tree. Scanning them would double-count every file and report
    # false positives for files that are grandfathered under their normal paths.
    ".claude/**",
]

EXCLUDE_PATTERNS = DEFAULT_EXCLUDES + _EXTRA_EXCLUDES

# Files that already exceeded the size limit when the CI guard was introduced
# (v0.2 release prep, PR 13). New files must stay under the limit. These
# entries are tracked for targeted follow-up splits/trims and should shrink
# rather than grow; remove each once it falls back under its threshold.
GRANDFATHERED_FILES: frozenset[str] = frozenset({
    # Long-form reference docs. The 3000/8000-word limit targets typical
    # prose; these are enumerated planning and specification documents whose
    # length is inherent to their purpose.
    "ROADMAP.md",
    "docs/ai-agents-orchestration-spec.md",
    "docs/persatrix-extension-spec.md",
    "docs/v0.2-release-prep-plan.md",
    # v0.3.0-plan.md is the active release plan; it accumulates MQ rows
    # and Memory Quality follow-ups during the v0.3.x release cycle (same
    # pattern as the PR plans below and as v0.2-release-prep-plan above).
    # PR 238 ratified the Memory Quality Roadmap and added MQ-1..MQ-9; the
    # PR 238 review pass added MQ-10..MQ-13 (SubjectErasure traversal,
    # per-turn provenance, cross-scope identity, within-interaction
    # pressure). These tracking rows are load-bearing for the v0.3.x
    # work and trimming the surrounding narrative would erase release-cycle
    # context. Remove this entry once v0.3.0 ships and the plan is archived.
    "docs/v0.3.0-plan.md",
    "docs/rfcs/0005-persona-agent-memory.md",
    "docs/rfcs/0005-pr-plan.md",
    "docs/rfcs/0006-pr-plan.md",
    # PR plan accumulates per-PR review residuals throughout the multi-PR
    # lifecycle (one review-findings subsection per merged PR). The plan
    # exited PR 3's review window with ~7 984 words; the PR 4 (RFC 0008
    # PR 4) review captures pushed it over the 8 000-word threshold.
    # Same rationale as `docs/rfcs/0019-pr-plan.md` below — trim/split is
    # more disruptive than informative on a plan that is still actively
    # accumulating per-PR follow-ups. Remove this entry once the
    # remaining 6-PR sequence completes and the plan is closed out.
    "docs/rfcs/0008-pr-plan.md",
    # PR plan accumulates per-PR review residuals throughout the multi-PR
    # lifecycle (one review-findings subsection per merged PR). The plan
    # exited PR 4's review window with ~7 980 words; the closeout PR 5
    # (RFC 0019 PR 5) tipped the file over the 8 000-word threshold while
    # appending the standard Disposition / Applied / Deferred sections.
    # Trim/split is more disruptive than informative on an already-merged
    # plan; remove this entry if a future maintenance PR splits the
    # per-PR review captures into a separate document.
    "docs/rfcs/0019-pr-plan.md",
    # docs/observability.md tipped over the 3 000-word prose limit when
    # RFC 0009 PR 1c added the audit-logger metric inventory + SLO alert
    # templates to §13. The new content is already trimmed (a one-line
    # instrument list and three Prometheus alert blocks — code-fenced
    # YAML does not count toward the prose limit). The §13 expansion is
    # required by the PR #234 review Medium-1 finding (capability-fsync
    # amplification monitoring) and PR #233 review Nice-to-have #5;
    # splitting observability.md by topic is a separate maintenance
    # refactor. Grandfather here until that lands.
    "docs/observability.md",
    # CHANGELOG.md grows over a release cycle and is trimmed/archived at
    # each release tag by the git-cliff pipeline (see `cliff.toml` and
    # the release process in `docs/development-workflow.md`), so size
    # temporarily exceeds the prose limit during the active Unreleased
    # window.
    "CHANGELOG.md",
    # docs/ai-glossary.md was at 2 999 words (1 word under the cap) when
    # RFC 0020 PR 4 (PR #229) landed. The PR #229 review Should-Fix #5
    # required adding the canonical PR-4 terminology (closing-state
    # interaction, summary-pending sentinel, summary-unavailable
    # sentinel, interaction janitor) to the glossary per the project's
    # own term-policy in `.github/copilot-instructions.md`. The new
    # section is already trimmed to ~150 words (one definition per
    # term, no aliases/examples sections). Splitting the glossary by
    # topic is a separate maintenance refactor; grandfather here until
    # that lands.
    "docs/ai-glossary.md",
    # agents/memory/facade.py was at 500 lines pre-PR-5. RFC 0020 PR 5
    # added the four-line defense-in-depth recall filter
    # (``SUMMARY_PENDING_TEXT`` skip + import) that pushed it to 504.
    # The PR-262 review M1 follow-up moved the filter into
    # ``EpisodicMemory.recall`` (the recall chokepoint) and replaced
    # the facade-level skip with a longer comment block explaining the
    # lift, settling the file at 507 lines. The facade is the API
    # boundary that RFC 0008 PR 2 froze for downstream consumers
    # (RFC 0011 PR 5, RFC 0020 PR 5); splitting it means rewriting the
    # public-import contract. Grandfather here until a dedicated
    # facade-split lands (queued alongside the episodic split below).
    "agents/memory/facade.py",
    # agents/memory/episodic.py was at ~492 lines pre-PR-262.  The
    # PR-262 review M1 follow-up lifted the ``SUMMARY_PENDING_TEXT``
    # recall filter into this module (added the import, the comment
    # block explaining the lift, and the post-rank filter
    # comprehension), pushing it to 514 lines.  The companion
    # interactions.py split (this PR) extracted ``scopes.py`` and
    # ``interaction_janitor.py`` so that file is now under-cap; the
    # equivalent episodic split — extracting the notes/state/retention
    # delegation methods into mixins along the established
    # ``facade_procedural`` / ``shared_pool_facade`` pattern — is
    # queued as a follow-up.  Grandfather here until that split lands.
    "agents/memory/episodic.py",
    # agents/persona_runtime/memory_context.py was at 498 lines (one
    # line under the cap) pre-RFC-0011 PR 5 follow-up.  The
    # channel-history tier (this PR) added the canonical-priority
    # docstring, the section-clear sweep entry, and the two helper-call
    # sites for ``recall_channel_episodes`` /
    # ``render_channel_history_section``; the tier's recall + admission
    # bodies live in the new ``agents/persona_runtime/channel_history.py``
    # so the bulk does not land here.  Further reduction requires the
    # parallel extraction of the relationship-admission block (~85
    # lines spanning the temporal-rendering + trust + cadence fields)
    # into the same companion-module pattern; that refactor is a
    # logically distinct change and is queued as a follow-up.
    # Grandfather here until that split lands.
    "agents/persona_runtime/memory_context.py",
})


class FileSizeWarning(NamedTuple):
    file: str
    kind: str
    measured: int
    limit: int
    unit: str


def _count_words(text: str) -> int:
    """Count words in *text*, stripping fenced code blocks."""
    in_code_block = False
    prose_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_block = not in_code_block
            continue
        if not in_code_block:
            prose_lines.append(line)
    return len(" ".join(prose_lines).split())


def _scan_files(
    repo_root: Path,
    max_code_lines: int = DEFAULT_MAX_CODE_LINES,
    max_doc_words: int = DEFAULT_MAX_DOC_WORDS,
    max_rfc_words: int = DEFAULT_MAX_RFC_WORDS,
) -> tuple[list[FileSizeWarning], list[tuple[str, int]], list[tuple[str, int]]]:
    """Single-pass scan — returns warnings plus all file measurements."""
    warnings: list[FileSizeWarning] = []
    code_results: list[tuple[str, int]] = []
    doc_results: list[tuple[str, int]] = []

    for fpath in walk_files(repo_root, extensions=CODE_EXTENSIONS, exclude_patterns=EXCLUDE_PATTERNS):
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        line_count = len(text.splitlines())
        rel = fpath.relative_to(repo_root).as_posix()
        code_results.append((rel, line_count))
        if line_count > max_code_lines and rel not in GRANDFATHERED_FILES:
            warnings.append(FileSizeWarning(
                file=rel, kind="code", measured=line_count,
                limit=max_code_lines, unit="lines",
            ))

    for fpath in walk_files(repo_root, extensions=DOC_EXTENSIONS, exclude_patterns=EXCLUDE_PATTERNS):
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        word_count = _count_words(text)
        rel = fpath.relative_to(repo_root).as_posix()
        doc_results.append((rel, word_count))
        effective_limit = max_rfc_words if rel.startswith(_RFC_PREFIX) else max_doc_words
        if word_count > effective_limit and rel not in GRANDFATHERED_FILES:
            warnings.append(FileSizeWarning(
                file=rel, kind="doc", measured=word_count,
                limit=effective_limit, unit="words",
            ))

    return warnings, code_results, doc_results


def get_warnings(
    repo_root: Path | None = None,
    max_code_lines: int = DEFAULT_MAX_CODE_LINES,
    max_doc_words: int = DEFAULT_MAX_DOC_WORDS,
) -> list[FileSizeWarning]:
    """Programmatic API — returns warnings without printing."""
    root = repo_root or REPO_ROOT
    warnings, _, _ = _scan_files(root, max_code_lines, max_doc_words)
    return warnings


def check_file_size(
    repo_root: Path,
    max_code_lines: int = DEFAULT_MAX_CODE_LINES,
    max_doc_words: int = DEFAULT_MAX_DOC_WORDS,
    strict: bool = False,
    verbose: bool = False,
) -> int:
    """Run the file size audit. Returns 0/1 depending on findings and mode."""
    warnings, code_results, doc_results = _scan_files(repo_root, max_code_lines, max_doc_words)

    print(f"[SCAN] Scanned {len(code_results)} code files and {len(doc_results)} doc files")

    if verbose:
        print("\n--- Code files ---")
        for rel, lines in sorted(code_results, key=lambda x: -x[1])[:20]:
            flag = " ⚠" if lines > max_code_lines else ""
            print(f"  {lines:>5} lines  {rel}{flag}")
        print("\n--- Doc files ---")
        for rel, words in sorted(doc_results, key=lambda x: -x[1])[:20]:
            flag = " ⚠" if words > max_doc_words else ""
            print(f"  {words:>5} words  {rel}{flag}")

    if warnings:
        print(f"\n[WARN] {len(warnings)} file(s) exceed size limits:")
        for w in warnings:
            print(f"  {w.file}: {w.measured} {w.unit} (limit: {w.limit})")

        if strict:
            return 1
    else:
        print("[OK] All files within size limits.")

    return 0


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()

    parser = argparse.ArgumentParser(description="Check file sizes against review-friendly limits.")
    parser.add_argument("--max-code-lines", type=int, default=DEFAULT_MAX_CODE_LINES)
    parser.add_argument("--max-doc-words", type=int, default=DEFAULT_MAX_DOC_WORDS)
    parser.add_argument("--strict", action="store_true", help="Exit 1 on warnings")
    parser.add_argument("--verbose", action="store_true", help="Show all scanned files")
    args = parser.parse_args(argv)

    return check_file_size(
        REPO_ROOT,
        max_code_lines=args.max_code_lines,
        max_doc_words=args.max_doc_words,
        strict=args.strict,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    sys.exit(main())
