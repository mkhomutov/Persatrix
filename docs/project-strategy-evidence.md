# Project Strategy — Evidence

> Companion to the [standing strategy review](project-strategy.md). Every
> number there is here with the command that produced it, so each review
> re-runs the commands and sees what moved. Measured at `main` = `cbeea7bc`
> on 2026-09-11 unless a row says otherwise.
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

| Measure | Value | Command |
|---------|-------|---------|
| First commit | 2026-04-08 | `git log --reverse --format=%ad --date=short \| head -1` |
| Commits | 926 | `git rev-list --count HEAD` |
| Release tags | 21 (v0.1.0 → v0.3.15) | `git tag \| wc -l` |
| Contributors | 1 human + Dependabot | `git shortlog -sn` |
| Go production | 44 926 lines | `count '\.go$' '(_test\.go$\|\.pb\.go$)'` |
| Go tests | 67 192 | `count '_test\.go$'` |
| Python production (`agents/`) | 56 452 | `count '^agents/.*\.py$' '(_pb2\|/tests/)'` |
| Python tests | 135 199 | `count '^tests/.*\.py$\|^agents/tests/.*\.py$'` |
| Rust CLI | 10 909 | `count '\.rs$'` |
| Web console (Svelte + JS) | 12 329 | `count '\.(svelte\|js)$' '(node_modules\|assets/)'` |
| Channel engine, `internal/channels/` | 20 126 | `count '^internal/channels/.*\.go$' '_test\.go$'` |
| Manual tests | 73 | `ls docs/manual-tests/MT-*.md \| wc -l` |
| Eval sets | 6 (+ goldens) | `ls evaluators/eval_sets/` |
| CI jobs / required checks | 11 / 12 | `ci.yml`; `docs/methodology/branch-protection.json` |
| Markdown, `wc -w` | 1.45 M words; guides 34 053 | proportions only — the checker strips code blocks |

Tests are 1.7 lines per line of production code. Guides plus README are
about 2.5 % of the prose.

## 2. Outside use

| Signal | Value | Command |
|--------|-------|---------|
| Stars / forks / watchers | 4 / 0 / 0 | `gh repo view --json stargazerCount,forkCount,watchers` |
| Views, 14 days | 254 from 23 unique visitors | `gh api repos/mkhomutov/Persatrix/traffic/views` |
| Referrers | github.com 129 views from 3 people; Google 1 | `…/traffic/popular/referrers` |
| Clones, 14 days | 3 869 from 328 cloners — CI checkouts | `…/traffic/clones` |
| Release assets, v0.3.15 | 0 | `gh release view v0.3.15 --json assets` |
| Image or binary publishing in CI | none | `grep -nE 'ghcr\|docker push\|upload-release' .github/workflows/*.yml` |
| Open PRs from anyone else | 0 | `gh pr list --state open` |

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

Since the reviews' baseline (`git log f52e7ff6..HEAD --format=%s`): 35
commits — 21 `docs`, 12 `fix`, 2 `ci`, 0 `feat`.

The `agents/memory/` tree, `internal/mcp/`, `internal/a2a/` and the workflow
engine (`internal/planner`, `scheduler`, `executor`, `state`) took no
functional commit between 2026-07-01 and this measurement; the two commits
touching the workflow engine are comment fixes
(`git log --since=2026-07-01 --oneline -- internal/planner internal/scheduler internal/executor internal/state`).

## 4. Size-cap pressure

`scripts/checks/file_size.py --near-cap`: **24** files exactly at a limit,
**100** within 3 % of one (the 24 included). #898 measured 24 and 101 at `f52e7ff6`, before
v0.3.16 PRs D1 and D2 split two at-cap files; the at-cap count did not move.

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
(inbound agent endpoint) is 🔨 Draft, target v0.4.x.

## 6. Code facts the decision rests on

| Claim | Evidence |
|-------|----------|
| The memory budget is a hardcoded constant | `agents/persona_runtime/memory_budget.py:107` — `MEMORY_BUDGET_TOKENS: int = 1500`; no key in `config/optimization.yaml` |
| `agents/memory/` stands alone | Its imports are the standard library, `aiosqlite` and `opentelemetry` only: `git ls-files 'agents/memory/*.py' \| xargs grep -hoE '^(from\|import) [a-zA-Z_.]+' \| sort \| uniq -c` |
| No ablation or baseline exists | `git grep -niE 'ablat' -- docs/ evaluators/ tests/` → 0; `git grep -niE 'baseline\|single[- ]call' -- evaluators/` → 0 |
| Trust reaches the model as one prompt line | `agents/persona_runtime/relationship_section.py:224-226` injects the score only when it differs from 0.5; `EVAL-MEMORY-001.yaml:58-62` settles for `> 0.0` and notes the increase form "needs live" |
| The audience gate is shadow-only | [RFC 0037 audience amendment](rfcs/0037-amendment-audience-egress.md) status 🔄 Shadow; flip is verdict-gated (v0.3.16 PR A3) |
| Live arcs cost cents | v0.3.11 autonomous arc $0.17, 99 s, about 47 000 tokens; v0.3.15 ten-leg arc $0.13 ([execution reports](manual-tests/)) |
| Extraction RFCs are parked | RFC 0046 and 0047: 📋 Proposed, target v0.4.0+, gated on RFC 0045 and the MIT↛BUSL CI gate |
| Licence grant | Non-commercial use only, deployments under 10 users ([LICENSE](../LICENSE) Additional Use Grant) |
| Interop drafts waiting | [#754](https://github.com/mkhomutov/Persatrix/pull/754) and [#755](https://github.com/mkhomutov/Persatrix/pull/755) (RFC 0041 Phase 1) open since 2026-07-18 |

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

No amendment after 2026-06-28 cites anything from outside the repository.
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
copied here; a review that relies on one re-fetches it.

## 9. Recommendation ledger

Every recommendation the four reviews made, with its disposition in the
[standing review](project-strategy.md). *Adopted* means carried as written;
*changed* means carried with the stated modification; *rejected* names the
reason; *deferred* means decided at the first review.

| Source | Recommendation | Disposition |
|--------|----------------|-------------|
| #895 R1 | Unblock RFC 0046 now, breaking the version train | Rejected — #896's market finding; the lease travels with the governance core (§2) |
| #895 R2 | Implement MCP in both directions | Changed — one interop slice, chosen by the experiment (§6.7) |
| #895 R3 | Stop the tenancy work after the shadow gate | Changed — stop after v0.3.16, but keep the gate as the confidentiality story (§2) |
| #895 R4 | Choose one wedge; rewrite the README | Changed — wedge stated as a hypothesis; README changes on the result (§5) |
| #895 R5 | Cut the cycle to plan → implement → tag | Adopted as §7.3 |
| #895 R6 | Raise the file cap to ~800 or retire it | Adopted as §7.4 (warning at 500, gate at 800) |
| #895 R7 | Retire the Rust CLI | Changed — freeze now, delete on the wedge decision (§2) |
| #895 R8 | Reconsider BUSL | Deferred — default Apache-2.0 at the first review (§4) |
| #896 S1 | External-evidence input to Before Phase 0 | Adopted as §7.1 |
| #896 S2 | Ship MCP, memory side first | Changed — the MCP server is one of the two interop slices (§6.7) |
| #896 S3 | Reposition on the memory boundary | Changed — one of two candidate wedges; the experiment decides (§4) |
| #896 S4 | Do not extract `persatrix-budget` | Adopted; RFC 0046 re-targeted rather than retired (§8) |
| #896 S5 | Cap the v0.3.x train; name what you remove | Adopted as §7.2 |
| #896 S6 | Permissive licence at the reachable boundary | Adopted — MIT for anything built to be reached (§2) |
| #896 S7 | File cap to ~800 | Adopted as §7.4 |
| #896 S8 | Retire the Rust CLI | Changed — freeze (§2) |
| #897 S1 | Run the ablation before repositioning | Adopted; widened to the five-arm experiment (§4) |
| #897 S2 | Make `MEMORY_BUDGET_TOKENS` configurable first | Adopted as §6.2 |
| #897 S3 | Reposition on the gate, not on memory | Changed — the gate is the fallback wedge if C does not beat A (§4) |
| #897 S4 | Decide the fate of the other 86 % | Changed — decided per component at the first review, not by an amendment now (§8) |
| #897 S5 | Retire the trust score or evidence it | Adopted as §8 last row |
| #897 S6 | Adopt #896's external-evidence gate | Adopted as §7.1 |
| #898 R1 | Pause v0.4.0; finish v0.3.16 small | Adopted as §6.1 |
| #898 R2 | One published experiment, four setups | Adopted; a fifth arm added for the allocator (§4) |
| #898 R3 | Name one first user and job in the README | Changed — named as a hypothesis (§5); README after the result |
| #898 R4 | First contact in five minutes: images, binaries, demo, transcript | Adopted as §6.5 |
| #898 R5 | MCP client and an A2A endpoint | Changed — one slice, RFC 0043 preferred (§3.4, §6.7) |
| #898 R6 | Release the governance core and budget lease permissively now | Changed — lease not alone; the core's extraction is decided at the first review (§8) |
| #898 R7 | Process diet; cap as a warning | Adopted as §7.3–§7.4 |
| #898 R8 | Review on 2026-11-30 against outcomes | Adopted as the first review date (§10) |

## Related documentation

- [Project strategy — standing review](project-strategy.md) — the argument
  these numbers support.
- [v0.3.x sequencing](v0.3.x-sequencing.md) — the decision log in §7.
- [Documentation guide § Size limits](documentation-guide.md#size-limits) —
  the caps §4 measures against.
