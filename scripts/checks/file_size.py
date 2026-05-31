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
    # Auto-generated issue index (scripts/issues.py); its word count scales with
    # the number of tracked issues, not prose authored in the file. Reviewed via
    # `python scripts/issues.py --check` (sync) + per-issue front-matter, not a
    # prose cap — same data-scaling rationale as THIRD_PARTY_NOTICES.md above.
    "docs/issues/INDEX.md",
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
    # docs/v0.3.1-plan.md is the v0.3.1 master plan — same release-cycle
    # accumulator pattern as docs/v0.3.0-plan.md above. It crossed the
    # 3000-word prose cap when the v0.3.1 post-release follow-up flipped the
    # Status / Completed header and rolled the release-prep and post-release
    # PRs into the Master Progress Overview. The RFC 0034 amendment was
    # already split out into docs/v0.3.1-plan-amendment-2026-05-15.md to
    # hold the line earlier in the cycle; trimming the remaining
    # release-cycle narrative would erase context. Remove this entry once
    # v0.3.1 is archived.
    "docs/v0.3.1-plan.md",
    # docs/v0.3.5-plan.md is the active v0.3.5 master plan — same
    # release-cycle accumulator pattern as docs/v0.3.0-plan.md /
    # docs/v0.3.1-plan.md above. It sat just under the 3000-word prose cap
    # through Phases 0–4 and crossed it when the 2026-05-31 scope decision
    # folded the epoch axis (ISSUE-0085) into the release as Phase 3b —
    # adding the phase section, the Master Progress row, and the Acceptance
    # gate. The per-PR detail lives in the dedicated
    # docs/rfcs/0031-epoch-pr-plan.md; the umbrella holds only the
    # release-level framing, and trimming the surrounding narrative would
    # erase release-cycle context. Remove this entry once v0.3.5 ships and
    # the plan is archived.
    "docs/v0.3.5-plan.md",
    # docs/v0.3.x-sequencing.md orchestrates the v0.3.1 / v0.3.2 / v0.3.3
    # patch sequence and accumulates amendments as new v0.3.x-targeted
    # RFCs file (the 2026-05-12 amendment captured the RFC 0030 + RFC
    # 0031 landings and re-shuffled v0.3.1 / v0.3.2 scope). The original
    # 2026-05-10 ratified decision is preserved verbatim above the
    # amendment for context — that "preserve original + dated amendment"
    # framing is the load-bearing shape of the doc and trimming the
    # original body to fit the cap would defeat the comparison the
    # amendment depends on. Same release-cycle-accumulator pattern as
    # docs/v0.3.0-plan.md above. Remove this entry once v0.3.3 ships
    # and the doc is archived.
    "docs/v0.3.x-sequencing.md",
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
    # Same per-PR review residual accumulator pattern as 0008-pr-plan.md
    # and 0019-pr-plan.md above. The plan exited PR 3's review window
    # with ~7 470 words; the PR 4 (RFC 0026 PR 4) deep-review captures
    # — phantom-reinforcement / TICK-cost / soft-slice overage findings,
    # each justified inline so the next reader sees the rationale
    # alongside the contract — pushed the doc over the 8 000-word
    # threshold. Trimming the per-PR review captures would defeat the
    # purpose of co-locating residuals with the plan; remove this
    # entry when the remaining 2-PR sequence (PR 5 / PR 6) closes out
    # and the plan is sealed at v0.3.1 release tag.
    "docs/rfcs/0026-pr-plan.md",
    # Same per-PR review residual accumulator pattern as 0008-pr-plan.md,
    # 0019-pr-plan.md, and 0026-pr-plan.md above. The plan exited PR 1's
    # first review window with ~7 983 words (1 word under the cap); the
    # PR 1 follow-up review captured a fifth deferred item — stop()
    # orphans pending ``InboundEventWake`` handles via the supervisor's
    # ``_stopped`` guard short-circuiting before the queue drains, plus
    # the same TOCTOU shape on ``enqueue`` racing ``stop()``. The item
    # text co-locates the symptom, fix sketch, and the pinning xfail
    # test name so the next reader sees the residual alongside the
    # remaining four. Trimming would defeat the co-location; same
    # disposition as the prior PR plans. Remove this entry once the
    # remaining 4-PR sequence (PR 2 / PR 3a / PR 3b / PR 4) plus the
    # review-follow-ups PR closes out and the plan is sealed at the
    # v0.3.3 release tag.
    "docs/rfcs/0024-pr-plan.md",
    # Same per-PR review residual accumulator pattern as 0008-pr-plan.md,
    # 0019-pr-plan.md, 0026-pr-plan.md, and 0024-pr-plan.md above. The plan
    # was at ~7 854 words after PR 3 merged; the PR #433 deep-review findings
    # table (cost-regression/$0 caps, ISSUE-0070 SystemExit footgun, two
    # in-PR doc fixes, templates info) pushed it to ~8 070 words. The finding
    # rows co-locate rationale with the plan so the next reader sees why each
    # item is deferred or fixed; trimming would erase that context. Remove
    # this entry once the remaining PR 4–7 sequence closes out and the plan
    # is sealed at the v0.3.4 release tag.
    "docs/rfcs/0033-pr-plan.md",
    # Long-form architecture RFC (cf. docs/rfcs/0005-persona-agent-memory.md
    # above) that accumulates implementation amendments inline so each spec
    # section carries its as-built reconciliation: the ISSUE-0081 PR 2/3/4
    # session-model amendments, the scope-axes reframing (§A), and the Phase 3
    # operator-CLI closeout. The RFC exited the Phase 3 PR 4 window at ~7 995
    # words (just under the cap); the Phase 3 PR 5 closeout amendment (§E — all
    # three resolution mechanisms wired, the OQ #6 override-above-auto-binding
    # reconciliation, and the ISSUE-0086 `--all-sessions` carve-out) tipped it
    # to ~8 090. Trimming the closeout would split the operator-surface
    # contract from the spec it amends; reaching into unrelated amendments to
    # offset it trades fidelity for an arbitrary line. Remove this entry at RFC
    # seal (Phase 4 closeout) or if a maintenance PR moves the amendment
    # history into a separate changelog.
    "docs/rfcs/0031-per-session-namespacing-channels.md",
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
    # Per-release manual-test execution reports accumulate evidence across
    # the PR 1 (initial sweep) and PR 4 (final pre-tag verification)
    # release-prep passes — every test row carries inline command + output
    # snippets, per-leg notes, and §"Release-prep regressions fixed"
    # tables that grow the file past the 3 000-word prose cap. Same
    # release-cycle-accumulator pattern as `CHANGELOG.md` above: written
    # against a fixed release, archived once the tag ships. The v0.2.3
    # report (3 395 words at tag time) set the precedent; the v0.3.0 PR 4
    # rerun (this addition) brings the report to ~4 600 words.
    "docs/manual-tests/v0.3.0-execution-report.md",
    # docs/manual-tests/v0.3.1-execution-report.md is the v0.3.1 sibling of
    # v0.3.0-execution-report.md above — identical per-release accumulator
    # pattern. The PR 1 live execution pass (the 26 previously-blocked rows
    # run against the Docker Compose stack) brought the report to ~4 250
    # words: every test row carries an inline outcome + evidence note, plus
    # the MT-MEMORY-005 per-leg acceptance table and the §Follow-ups
    # findings (F-1 facts-tier gap). Written against the v0.3.1 release;
    # archive once the tag ships.
    "docs/manual-tests/v0.3.1-execution-report.md",
    # docs/manual-tests/v0.3.2-execution-report.md is the v0.3.2 sibling of
    # the v0.3.0 / v0.3.1 reports above — identical per-release accumulator
    # pattern. The release-prep PR 1 sweep (32 tests + wallet acquire+settle
    # p99 measurement) brought the report past the 3 000-word prose cap:
    # every test row carries inline outcome + evidence + the F-1 chat-REST
    # surface failure root-cause trace cross-linked to ISSUE-0065 (the only
    # ❌ Fail on this pass). Written against the v0.3.2 release; archive
    # once the tag ships.
    "docs/manual-tests/v0.3.2-execution-report.md",
    # docs/manual-tests/v0.3.3-execution-report.md is the v0.3.3 sibling of
    # the v0.3.0 / v0.3.1 / v0.3.2 reports above — identical per-release
    # accumulator pattern. The PR 1 sweep (34 rows + automated suites) plus
    # the release-prep PR 4 § Re-Execution section (full-suite rerun + the
    # four-behaviour live Docker smoke on the post-version-bump tip) bring
    # the report past the 3 000-word prose cap. Written against the v0.3.3
    # release; archive once the tag ships.
    "docs/manual-tests/v0.3.3-execution-report.md",
    # docs/manual-tests/v0.3.4-execution-report.md is the v0.3.4 sibling of
    # the v0.3.0–v0.3.3 reports above — identical per-release accumulator
    # pattern. The release-prep PR 1 sweep (38 rows = 4 new RFC 0033 MTs +
    # 34 carried-forward, plus the automated suites) carries inline per-step
    # evidence tables for the four new MTs (alias routing, the live one-line
    # provider swap with exact gpt-4o cost math, offline $0, Ollama real
    # tokens) and the §Follow-ups findings (F-5 OPENAI_API_KEY plumbing,
    # F-6 CPU-Ollama latency, F-7 1% tail-sampling), pushing it past the
    # 3 000-word prose cap. Written against the v0.3.4 release; archive once
    # the tag ships.
    "docs/manual-tests/v0.3.4-execution-report.md",
    # docs/v0.3.3-release-checklist.md crossed the 3 000-word prose cap as a
    # release-cycle record: the §3.1 Upgrade Notes table (8 rows — event-driven
    # loop, fire-and-forget channel dispatch, autonomy.timers, scheduled_wakes
    # cache, salience knobs, wake counters, the vestigial §F guard, and the
    # breaking MemoryFacade alias removal) and the §6 Known Gaps inventory are
    # inherently longer for this feature-rich release than the v0.3.2 sibling
    # (which fit at ~2 900 words); the PR 4 gate evidence is already condensed,
    # with full detail deferred to the grandfathered execution report. Written
    # against the v0.3.3 release; archive once the tag ships.
    "docs/v0.3.3-release-checklist.md",
    # docs/v0.3.4-release-checklist.md is the v0.3.4 sibling of the v0.3.3
    # checklist above — identical release-cycle-record pattern. It crossed the
    # 3 000-word prose cap in release-prep PR 4 when the §1 gate checkboxes were
    # filled with the post-bump re-certification evidence (per-gate results +
    # the carried-forward Docker-smoke leg values) on top of the §3.1 Upgrade
    # Notes table (7 rows — provider-agnostic aliases, no-default-provider,
    # config-driven selection, provider-neutral onboarding, missing-price guard,
    # alias-derived pricing + model_alias span, $0-local vs the wallet cap) and
    # the §6 Known Gaps inventory, both inherently long for this provider-parity
    # release. The PR 4 gate evidence is already condensed, with full detail
    # deferred to the grandfathered execution report. Written against the v0.3.4
    # release; archive once the tag ships.
    "docs/v0.3.4-release-checklist.md",
    # docs/v0.3.4-release-prep-plan.md is the v0.3.4 release-prep sequencer —
    # same release-cycle-accumulator pattern as the v0.3.0 / v0.3.1 plans and
    # the v0.3.3 checklist above. It crossed the 3 000-word prose cap when PR 1
    # made the provider-neutral onboarding scope explicit (the F-5 per-agent
    # OPENAI_API_KEY plumbing requirement) on top of the four PR scope +
    # acceptance blocks and the §Current state / §Known follow-up inventories;
    # it will keep accumulating as PRs 2–4 land their status + acceptance
    # residuals. Trimming the per-PR scope/acceptance detail would erase the
    # contract the sequence depends on. Remove this entry once v0.3.4 ships and
    # the plan is archived.
    "docs/v0.3.4-release-prep-plan.md",
    # docs/guides/persona-agents.md was at 2 867 words on the v0.3.0
    # release-candidate tip; release-prep PR 2 added three §2 callouts
    # (interactions-not-messages per RFC 0020, now-anchor per RFC 0021,
    # and a new §6 listing the externally inspectable persona prompt
    # sections per RFC 0022). The new content is already trimmed (each
    # callout is one paragraph; §6 is a one-row-per-section table). A
    # future maintenance PR can split the chat (§4) and observability
    # (§5) subsections into the chat-specific guide once it exists, but
    # that is a separate refactor. Grandfather here until that lands.
    "docs/guides/persona-agents.md",
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
    # docs/storage-architecture-roadmap.md is the long-form planning doc the
    # SA-1..SA-10 storage items live in. It sat right at the 3000-word prose
    # cap and crossed it when RFC 0029 Phase 1 PR 5 (the Phase 1 closeout)
    # flipped the SA-1 row — vehicle "new RFC" → RFC 0029, target + status
    # updated to record the v0.3.2 `MemoryStore` facade landing. Same
    # status-flip-tips-a-tracking-doc pattern as docs/v0.3.1-plan.md above;
    # trimming the SA-1..SA-10 narrative would erase planning context and a
    # topic split is a separate docs refactor. Remove this entry once that
    # split lands.
    "docs/storage-architecture-roadmap.md",
    # agents/memory/facade.py, agents/memory/episodic.py, and
    # agents/persona_runtime/memory_context.py were grandfathered above
    # the 500-line cap; their splits landed in this PR.  facade.py was
    # already under-cap once the procedural mixin (``facade_procedural``)
    # absorbed RFC 0008 PR 5 follow-ups; episodic.py dropped below the
    # cap when the notes-tier delegates moved into
    # ``episodic_notes_api._EpisodicNotesAPIMixin``; memory_context.py
    # dropped below the cap when the relationship-tier admission block
    # moved into ``relationship_section`` (parallel to the
    # ``channel_history`` extraction precedent).
})


class FileSizeWarning(NamedTuple):
    file: str
    kind: str
    measured: int
    limit: int
    unit: str


def _count_words(text: str) -> int:
    """Count words in *text*, stripping fenced code blocks and YAML front-matter.

    YAML front-matter (the ``---``-delimited block at the very top of a doc)
    is structured metadata, not prose — counting it against the word cap
    would punish RFC/issue files for adding required machine-readable
    metadata.
    """
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break
    in_code_block = False
    prose_lines: list[str] = []
    for line in lines[start:]:
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
