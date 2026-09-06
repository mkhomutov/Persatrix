"""Pin ``scripts/merged_prs.py`` — the generated merged-PR history.

ROADMAP.md carried a hand-maintained "Merged PR History" table that stopped
at #708 in June while ~150 PRs merged after it: the table is derivable from
the squash-merge subjects on ``main``, and derivable data that is typed by
hand goes stale. The generator reads ``git log`` and writes
``docs/merged-prs.md`` between auto markers, the same shape as the RFC and
issue indexes, so a ``--check`` can fail CI when it is stale.
"""

from __future__ import annotations

import pytest

from scripts.merged_prs import MergedPR, classify, parse_log, render

LOG = "\n".join(
    [
        "7403cd28|2026-09-06|docs(branching,claude): rewrite BRANCHING.md (#860)",
        "9d3d2878|2026-09-06|ci(checks): promote every local-only check into CI "
        "(ISSUE-0133, ISSUE-0134) (#858)",
        "89367eb2|2026-09-06|test(manual-tests,docs): v0.3.15 release-prep PR 1 — "
        "the ten-leg live arc (#855)",
        "9003032e|2026-09-02|fix(channels,wallet): ISSUE-0123 + ISSUE-0131 — "
        "the close reserve (v0.3.15 PR A4b) (#852)",
        "a6aeab86|2026-08-03|feat(v0313): RFC 0049 Phase 0 — metadata → DispatchContext (#809)",
        "deadbeef|2026-08-01|chore(deps): bump google.golang.org/grpc from 1.82.1 to 1.83.1 (#853)",
        "1f522469|2026-04-08|Initial commit",
        "cafef00d|2026-05-01|feat: a direct commit with no PR number",
    ]
)


def test_parse_log_keeps_only_squash_merged_prs_newest_first() -> None:
    prs = parse_log(LOG)
    assert [p.number for p in prs] == [860, 858, 855, 852, 809, 853]
    assert prs[0] == MergedPR(
        number=860, date="2026-09-06", title="docs(branching,claude): rewrite BRANCHING.md",
    )


def test_parse_log_strips_the_pr_suffix_from_the_title() -> None:
    assert all("(#" not in p.title for p in parse_log(LOG))


@pytest.mark.parametrize(
    "title, area",
    [
        ("feat(v0313): RFC 0049 Phase 0 — metadata → DispatchContext", "RFC 0049 · v0.3.13"),
        (
            "fix(channels,wallet): ISSUE-0123 + ISSUE-0131 — the close reserve (v0.3.15 PR A4b)",
            "ISSUE-0123, ISSUE-0131 · v0.3.15",
        ),
        (
            "test(manual-tests,docs): v0.3.15 release-prep PR 1 — the ten-leg live arc",
            "v0.3.15 release prep",
        ),
        ("docs(release): v0.3.14 post-release follow-up — Phase-4 backfills", "v0.3.14 post-release"),
        ("chore(deps): bump google.golang.org/grpc from 1.82.1 to 1.83.1", "deps"),
        ("docs(branching,claude): rewrite BRANCHING.md", "branching, claude"),
        ("feat: scaffold initial project structure", "—"),
    ],
)
def test_classify_derives_the_area_column_from_the_title(title: str, area: str) -> None:
    assert classify(title) == area


def test_render_writes_the_table_between_auto_markers() -> None:
    content = render(parse_log(LOG))
    assert "<!-- BEGIN merged-prs:auto -->" in content
    assert "<!-- END merged-prs:auto -->" in content
    assert "| [#860](https://github.com/mkhomutov/Persatrix/pull/860) |" in content
    assert "6 merged" in content
    # Newest first, so #860 appears before #809.
    assert content.index("[#860]") < content.index("[#809]")


def test_render_escapes_pipes_in_titles() -> None:
    prs = [MergedPR(number=1, date="2026-01-01", title="feat: a | b")]
    assert "a \\| b" in render(prs)
