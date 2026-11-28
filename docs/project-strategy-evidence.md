# Project Strategy — Evidence

> Companion to the [standing strategy review](project-strategy.md). Most
> numbers there and in its
> [four-reviews companion](project-strategy-four-reviews.md) are here with the
> command that produced them; the rest are the four reviews' own. Each review
> re-runs the commands and sees what moved. Measured at `main` = `cbeea7bc`
> on 2026-09-11, and again where a column or row says so: at `04bc24d0` on
> 2026-10-20 for the first review, and at `9ec80213` on 2026-11-28 for the
> review held after EXP-001's result.
>
> The four reviews this file preserves —
> [#895](https://github.com/mkhomutov/Persatrix/pull/895),
> [#896](https://github.com/mkhomutov/Persatrix/pull/896),
> [#897](https://github.com/mkhomutov/Persatrix/pull/897),
> [#898](https://github.com/mkhomutov/Persatrix/pull/898) — measured
> `f52e7ff6` (2026-09-09). Where a number below differs from theirs, the
> tree moved; where it matches, it was re-derived, not copied.

## How to re-run

From the repository root. `count` sums lines in tracked files matching a
pattern minus an exclusion; the size checker, not `wc -w`, is the word count
that the cap enforces.

```bash
count() { git ls-files | grep -E "$1" | grep -vE "${2:-^$}" | tr '\n' '\0' | xargs -0 cat | wc -l; }
.venv/bin/python scripts/checks/file_size.py --near-cap
```

## 1. Scale

| Measure | Value | 2026-10-20 | 2026-11-28 | Command |
|---------|-------|-----------:|-----------:|---------|
| First commit | 2026-04-08 | 2026-04-08 | 2026-04-08 | `git log --reverse --format=%ad --date=short \| head -1` |
| Commits | 926 | 1 033 | 1 134 | `git rev-list --count HEAD` |
| Release tags | 21 (v0.1.0 → v0.3.15) | 22 (→ v0.3.16) | 22 | `git tag \| wc -l` |
| Contributors | 1 human + Dependabot | the same, plus one Copilot-agent fix ([#994](https://github.com/mkhomutov/Persatrix/pull/994)) | the same | `git shortlog -sn` |
| Go production | 44 926 lines | 45 144 | 49 986 | `count '\.go$' '(_test\.go$\|\.pb\.go$)'` |
| Go tests | 67 192 | 67 705 | 81 019 | `count '_test\.go$'` |
| Python production (`agents/`) | 56 452 | 59 224 | 64 897 | `count '^agents/.*\.py$' '(_pb2\|/tests/)'` |
| Python tests | 135 199 | 160 722 | 185 026 | `count '^tests/.*\.py$\|^agents/tests/.*\.py$'` |
| Rust CLI | 10 909 | 10 909 | 10 909 | `count '\.rs$'` |
| Web console (Svelte + JS) | 12 329 | 15 591 | 18 852 | `count '\.(svelte\|js)$' '(node_modules\|assets/)'` |
| Channel engine, `internal/channels/` | 20 126 | 20 241 | 21 127 | `count '^internal/channels/.*\.go$' '_test\.go$'` |
| Manual tests | 73 | 74 | 74 | `ls docs/manual-tests/MT-*.md \| wc -l` |
| Eval sets | 6 (+ goldens) | 6 (+ goldens) | 6 (+ goldens) | `ls evaluators/eval_sets/` |
| CI jobs / required checks | 11 / 12 | 11 / 12 | 12 / 12; the new `Python 3.12 (unit tests)` job is not required | `ci.yml`; `docs/methodology/branch-protection.json` |
| Markdown, `wc -w` | 1.45 M words; guides 34 053 | — | — | proportions only — the checker strips code blocks |

Tests are 1.7 lines per line of production code. Guides plus README are
about 2.5 % of the prose. At 2026-11-28 the two test rows sum to 266 045
lines against 144 644 in the four production rows, 1.8 to one.

## 2. Outside use

| Signal | Value | 2026-10-20 | 2026-11-28 | Command |
|--------|-------|------------|------------|---------|
| Stars / forks / watchers | 4 / 0 / 0 | 4 / 0 / 0 | 4 / 0 / 0 | `gh repo view --json stargazerCount,forkCount,watchers` |
| Views, 14 days | 254 from 23 unique visitors | 86 from 4 unique visitors | 136 from 14 unique visitors | `gh api repos/mkhomutov/Persatrix/traffic/views` |
| Referrers | github.com 129 views from 3 people; Google 1 | github.com 11 views from 1 person | github.com 13 views from 1 person; Google 1 | `…/traffic/popular/referrers` |
| Clones, 14 days | 3 869 from 328 cloners — CI checkouts | 765 from 164 cloners | 3 498 from 438 cloners | `…/traffic/clones` |
| Release assets, latest release | 0 (v0.3.15) | 0 (v0.3.16) | 0 (v0.3.16) | `gh release view --json assets,tagName` |
| Image or binary publishing in CI | none | none | none | `grep -nE 'ghcr\|docker push\|upload-release' .github/workflows/*.yml` |
| Open PRs from anyone else | 0 | 0 (the author's two drafts) | 0 (the same two drafts) | `gh pr list --state open` |

The traffic endpoints need push access.

## 3. Pace

Merged PRs by month, typed from the squash title
(`gh pr list --state merged --limit 1000 --json title,mergedAt`):

| Month 2026 | Merged | feat | fix | docs | chore | other |
|------------|-------:|-----:|----:|-----:|------:|------:|
| April | 232 | 79 | 23 | 83 | 10 | 37 |
| May | 244 | 91 | 32 | 89 | 10 | 22 |
| June | 228 | 100 | 29 | 85 | 10 | 4 |
| July | 78 | 39 | 15 | 17 | 5 | 2 |
| August | 52 | 10 | 8 | 29 | 3 | 2 |
| September, to the 11th | 77 | 7 | 15 | 34 | 14 | 7 |
| September, whole month | 163 | 22 | 42 | 59 | 19 | 21 |
| October, to the 20th | 21 | 4 | 5 | 7 | 4 | 1 |
| October, whole month | 49 | 13 | 8 | 19 | 8 | 1 |
| November, to the 28th | 73 | 10 | 32 | 11 | 14 | 6 |

Since the reviews' baseline (`git log f52e7ff6..HEAD --format=%s`): 35
commits — 21 `docs`, 12 `fix`, 2 `ci`, 0 `feat`. From `cbeea7bc` to
`04bc24d0`: 107 — 32 `fix`, 32 `docs`, 19 `feat`, 10 `ci`, 9 `chore`, 4
`test`, 1 `revert`. From `04bc24d0` to `9ec80213`: 101 — 35 `fix`, 23 `docs`,
19 `feat`, 18 `chore`, 5 `ci`, 1 `test`. Of November's 32 fixes, 27 close
findings of the quality review of 2026-11-10, and 17 of the pull requests
merged since 2026-10-21 are Dependabot's.

`agents/tools/mcp_bridge.py`, `internal/mcp/`, `internal/a2a/` and the workflow
engine (`internal/planner`, `scheduler`, `executor`, `state`) took no
functional commit between 2026-07-01 and this measurement; the two commits
touching the workflow engine are comment fixes
(`git log --since=2026-07-01 --oneline -- internal/planner internal/scheduler internal/executor internal/state`).
(Corrected 2026-10-20: this sentence named `agents/memory/`, which took
functional commits throughout.) At 2026-10-20 it still holds: one commit since,
a lint fix in four executor test files (#945); the command now prints three
lines. At 2026-11-28 it prints four, and the fourth is the first functional
change since June: the scheduler now seals a finished run's log ring
([#1061](https://github.com/mkhomutov/Persatrix/pull/1061)). The MCP and A2A
paths took no commit.

## 4. Size-cap pressure

`scripts/checks/file_size.py --near-cap`: **24** files exactly at a limit,
**100** within 3 % of one (the 24 included). #898 measured 24 and 101 at `f52e7ff6`, before
v0.3.16 PRs D1 and D2 split two at-cap files; the at-cap count did not move.
At 2026-10-20, under ruling (e)'s code caps (a warning at 500 lines, a failure
at 800): **1** file at a limit, a doc, and **14** within 3 %. Against 500, like
for like (`--max-code-lines 500 --near-cap`): **18** at a limit, **97** within
3 %. At 2026-11-28 the warning is at 700 lines, by the maintainer's decision
of 2026-11-21
([ISSUE-0231](issues/ISSUE-0231-size-check-counts-comment-lines.md)), and the
failure still at 800: **1** file at a limit, a doc, and **20** within 3 %, all
of them docs. No code file is over 700 lines. Against 500, like for like:
**18** at a limit, **106** within 3 %.

## 5. Interop and stubs

| Path | Lines | Content |
|------|------:|---------|
| `internal/mcp/mcp.go` | 9 | package comment + TODO |
| `agents/tools/mcp_bridge.py` | 15 | TODO comments |
| `internal/a2a/a2a.go` | 11 | TODO |
| `internal/protocols/`, `internal/mesh/`, `internal/bridges/` | 15 / 16 / 15 | TODO |
| `internal/resilience/resilience.go` | 38 | package doc + TODO |

No MCP client dependency (`grep -i mcp agents/pyproject.toml`). The glossary
now marks the MCP bridge 📋 Planned, so the inaccuracy #898 reported in
passing is already fixed. [RFC 0043](rfcs/0043-inbound-agent-interop-endpoint.md)
(inbound agent endpoint) is 🔨 Draft, target v0.4.x. All of it is unchanged
at 2026-10-20 and at 2026-11-28.

## 6. Code facts the decision rests on

| Claim | Evidence |
|-------|----------|
| The memory budget was a hardcoded constant | `agents/persona_runtime/memory_budget.py:107` — `MEMORY_BUDGET_TOKENS: int = 1500`; no key in `config/optimization.yaml`. Since v0.3.16 K1 ([#944](https://github.com/mkhomutov/Persatrix/pull/944)), `memory_budget.tokens` there sets it (the constant is now at :111) |
| `agents/memory/` leans on sibling modules | Besides the standard library, `aiosqlite` and `opentelemetry`, it imports `..llm_client`, `..principal_id`, `..session_id`, `..epoch_id`, `..prompt_loader` and `..model_aliases`, and since 2026-09-11 `..clock` and `..llm_types` (corrected 2026-10-20; the row said it stood alone), and since 2026-10-20 `..observability`, `..security` and `..sender_type`: `git ls-files 'agents/memory/*.py' \| xargs grep -hoE '^(from\|import) [a-zA-Z_.]+' \| sort \| uniq -c` |
| No ablation or baseline result exists | `git grep -niE 'ablat' -- docs/ evaluators/ tests/` → 0; `git grep -niE 'baseline\|single[- ]call' -- evaluators/` → 0. At 2026-10-20 the greps return 3 (the strategy docs' own text) and 1 (a comment in `evaluators/exp001/costs.py`); EXP-001's harness builds the baseline, arm A (`evaluators/exp001/arm_a.py`), but no result exists. At 2026-11-28 one does: [EXP-001's result](experiments/EXP-001-result.md), published 2026-11-27, compares four arms with arm A. The greps return 3 and 66: the comment, and 65 lines in EXP-001's published run records |
| Trust reaches the model as one prompt line | `agents/persona_runtime/relationship_section.py:224-226` (now :281-284) injects the score only when it differs from 0.5; `EVAL-MEMORY-001.yaml:58-62` settles for `> 0.0` and notes the increase form "needs live". No EXP-001 arm varies trust, so its result says nothing about this row |
| The audience gate was shadow-only | [RFC 0037 audience amendment](rfcs/0037-amendment-audience-egress.md) status 🔄 Shadow; flip is verdict-gated (v0.3.16 PR A3). Live since A3 ([#950](https://github.com/mkhomutov/Persatrix/pull/950)) |
| Live arcs cost cents | v0.3.11 autonomous arc $0.17, 99 s, about 47 000 tokens; v0.3.15 ten-leg arc $0.13 ([execution reports](manual-tests/)). EXP-001's scored run: 150 meetings, $44.09 for the arms and $4.44 for the judge |
| Extraction RFCs are parked | RFC 0046: not standalone, rides the governance-core extraction if a strategy review orders one; the first ordered none, and [ruling (t)](v0.4.x-sequencing-2026-10-21.md#rulings) passes it to the next. The next ([2026-11-28](project-strategy-review-2026-11-28.md#3-the-maintainers-rulings)) ordered none either, and it reopens only on an outside ask recorded in an amendment's External evidence section ([ruling aa](v0.4.x-sequencing-2026-11-28.md#rulings)). RFC 0047: 📋 Proposed, target v0.4.0+, gated on RFC 0045 and the MIT↛BUSL CI gate |
| Licence grant | Non-commercial use only, deployments under 10 users ([LICENSE](../LICENSE) Additional Use Grant) |
| Interop drafts waiting | [#754](https://github.com/mkhomutov/Persatrix/pull/754) and [#755](https://github.com/mkhomutov/Persatrix/pull/755) (RFC 0041 Phase 1) open since 2026-07-18; still drafts at 2026-10-20, now v0.4.0's cuttable D4; still drafts at 2026-11-28, waiting with the pause; since the Amendment 2026-11-28 they wait with D4 for EXP-002's pre-registration |

## 7. The decision log, condensed

From [v0.3.x sequencing](v0.3.x-sequencing.md); each row is one amendment.

| Date | Axis | What it decided |
|------|------|-----------------|
| 2026-05-10 → 05-23 | memory quality, cost | v0.3.1–v0.3.5 |
| 2026-06-04 | **adoption pull** | "to attract users, the multi-persona conversation must read as realistic and be useful"; confidentiality deferred as invisible to a prospective user |
| 2026-06-23, 06-28 | adoption pull | v0.3.10 reasoning before posting; v0.3.11 autonomous channel, "the single best adoption demo the project can ship" |
| 2026-07-25 | **memory realism** | RFC 0037 pulled forward; reasons: an internal review, substrate readiness, a twice-slipped RFC |
| 2026-08-02 | substrate | v0.3.13 deferred calls, v0.3.14 per-person isolation |
| 2026-08-19 | substrate | v0.3.15 attribution, v0.3.16 audience; scope from a sweep of the project's own topologies |
| 2026-09-12 | **measure first** | close v0.3.16 small; EXP-001 before any train opens |
| 2026-10-18 | **the goal** | a society that pursues long goals; EXP-001 a measurement; v0.4.0 opens |
| 2026-10-18 | substrate | v0.4.0's shape, in [v0.4.x sequencing](v0.4.x-sequencing.md): the phases' own prerequisites board, nothing else |
| 2026-10-21 | **the reading** | the first review acted on, in [a file of its own](v0.4.x-sequencing-2026-10-21.md): the goal replaces the product reading; rule 6 stays, read for the goal; the README may be rewritten after the strategy rewrite; Apache-2.0 outside the reserved seams by a licence PR |
| 2026-11-28 | **the order** | the review after EXP-001's result acted on, in [a file of its own](v0.4.x-sequencing-2026-11-28.md): the society work goes on, with EXP-002's pre-registration ahead of every v0.4.0 row not started; the pause of 2026-11-07 ends; no extraction ordered, no component retired; the warning at 700 lines |

No amendment after 2026-06-28 cites anything from outside the repository.
Since 2026-09-12 every amendment carries an External evidence section, and
all four to 2026-10-21 are empty of positives. The fifth, of 2026-11-28,
records the first positive entry by the rule's letter: an experiment result
published, EXP-001's, on 2026-11-28. That amendment does not lean on it.
Nothing records the v0.3.11 demo being shown to anyone.

## 8. Market claims — perishable

Carried from #896 and #898, gathered 2026-09-10 from public reporting, not
verified, and to be re-checked at every review. The lesson of #896 §4.1 is
that a market claim written in May was false by September.

- **Agent frameworks**: LangGraph, CrewAI, Microsoft Agent Framework, OpenAI
  Agents SDK, Google ADK, Claude Agent SDK are the common choices.
- **Agent memory**: Mem0 (reported $24M raised, tens of thousands of stars),
  Letta, Zep, Cognee, Supermemory; Mem0 ships as an MCP server usable from
  Claude Desktop, Cursor and VS Code. Persistent memory is called table
  stakes.
- **Pre-call budget gates**: Tollgate (MIT, reserves budget before the call),
  LiteLLM hard caps, Portkey gateway Apache-2.0 since March 2026, Bifrost
  hierarchical budgets. RFC 0045 §M-1's "the market is after-the-fact
  dashboards" no longer holds.
- **Simulated societies**: Simile (reported $2B valuation), Concordia
  (Apache-2.0 builder), OASIS.
- **Shared channels**: Slack's "agent-first workspace" and Slack Code
  (2026-08-20); Teams positions the same way.
- **Interop**: A2A joined MCP in the Linux Foundation's Agentic AI Foundation
  in August 2026, with 150+ supporting organisations.

Sources are listed in #898's evidence file (fourteen links) and are not
copied here; a review that relies on one re-fetches it. The first review
(2026-10-20) did not re-check them, since none of its rulings relies on them.
Nor did the review of 2026-11-28, for the same reason: it ordered no
extraction.

## Related documentation

- [Project strategy — standing review](project-strategy.md) — the argument
  these numbers support, with the four reviews' findings and their
  recommendation ledger in its
  [four-reviews companion](project-strategy-four-reviews.md).
- [The review of 2026-11-28](project-strategy-review-2026-11-28.md) — what
  the review that measured the 2026-11-28 figures recorded and ruled.
- [v0.3.x sequencing](v0.3.x-sequencing.md) — the decision log in §7.
- [Documentation guide § Size limits](documentation-guide.md#size-limits) —
  the caps §4 measures against.
