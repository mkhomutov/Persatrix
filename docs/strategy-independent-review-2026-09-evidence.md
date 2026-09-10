# Independent strategy review — evidence

> **Date**: 2026-09-10, measured at `main` = `f52e7ff6`.
> Companion to the [independent strategy review](strategy-independent-review-2026-09.md). Each figure comes with the command behind it, so anyone can re-run it and see whether it has moved.

## How to re-run these numbers

Run from the repository root. Word counts use `wc -w`, which also counts code blocks and so reads higher than the size checker; they are used for proportions only.

```bash
# lines in tracked files matching a pattern, minus an exclusion pattern
count() { git ls-files | grep -E "$1" | grep -vE "${2:-^$}" | tr '\n' '\0' | xargs -0 cat | wc -l; }
# words in tracked files matching a pattern
words() { git ls-files | grep -E "$1" | tr '\n' '\0' | xargs -0 cat | wc -w; }
```

## Size and shape

| Area | Lines | Files |
|---|---:|---:|
| Go production (tests and generated code excluded) | 44,794 | 227 |
| Go tests | 67,191 | 309 |
| Python production, `agents/` (generated code excluded) | 56,424 | 249 |
| Python tests, `tests/` and `agents/tests/` | 134,426 | 517 |
| Python scripts and evaluators | 10,907 | — |
| Rust CLI (its tests are inline) | 10,909 | 40 |
| Web console, TypeScript (tests included) | 12,328 | 70 |

```bash
count '\.go$' '(_test\.go$|\.pb\.go$)'
count '_test\.go$'
count '^agents/.*\.py$' '(_pb2|/tests/)'
count '^tests/.*\.py$|^agents/tests/.*\.py$'
count '\.rs$'
count '^web/.*\.(ts|tsx|js|jsx)$' 'node_modules'
```

Where the production code sits:

| Area | Lines |
|---|---:|
| `internal/channels/` — channels, governance, autonomy (Go) | 20,126 |
| `internal/server/` — the REST surface (Go) | 7,956 |
| Workflow engine — `planner`, `scheduler`, `executor`, `state` (Go) | 3,680 |
| `agents/memory/` (Python) | 15,785 |
| `agents/persona_runtime/` (Python) | 15,363 |
| Placeholders — `internal/mcp`, `internal/a2a`, `internal/bridges`, `internal/mesh`, `internal/protocols` | 7 / 6 / 10 / 8 / 7 |
| `agents/tools/mcp_bridge.py` — TODO comments only | 12 |

```bash
git log --since=2026-07-01 --oneline -- internal/planner internal/scheduler internal/executor internal/state | wc -l   # 0
grep -niE '"mcp' agents/pyproject.toml   # no match: no MCP client dependency
```

## Words by document kind

| Kind | Words |
|---|---:|
| User guides, `docs/guides/` | 32,667 |
| README | 3,370 |
| RFCs and their PR plans, `docs/rfcs/` | 619,498 |
| Version plans, scope locks and checklists, `docs/v0.*` | 223,914 |
| Manual tests and execution reports | 211,465 |
| Issues | 142,199 |
| Founding specs (two files) | 20,471 |
| Methodology and templates | 18,939 |
| `docs/merged-prs.md` | 17,706 |
| ROADMAP | 14,219 |
| CHANGELOG | 56,581 |
| Other markdown | 55,975 |

Guides plus README come to about 36,000 words. The rows from RFCs down to ROADMAP come to about 1.27 million — roughly 35 words of internal writing per user-facing word. The changelog and the other markdown are left out of both sides.

```bash
words '^docs/guides/.*\.md$'
words '^docs/rfcs/.*\.md$'
words '^docs/v0\..*\.md$'
words '^docs/manual-tests/.*\.md$'
sed -n '4p;5p' ROADMAP.md | wc -w   # 665: the "Current phase" and "Current milestone" lines
```

## Pace of work

| Month (2026) | Merged PRs | feat | fix | docs | chore | Code lines added | Doc lines added |
|---|---:|---:|---:|---:|---:|---:|---:|
| April | 232 | 79 | 23 | 83 | 10 | 46,338 | 50,735 |
| May | 244 | 91 | 32 | 89 | 10 | 38,102 | 46,553 |
| June | 228 | 100 | 29 | 85 | 10 | 50,235 | 28,041 |
| July | 78 | 39 | 15 | 17 | 5 | 23,516 | 9,977 |
| August | 52 | 10 | 8 | 29 | 3 | 8,341 | 8,589 |
| September, to the 10th | 42 | 7 | 3 | 13 | 14 | 9,660 | 8,327 |

Types come from each pull-request title, which becomes the squash commit. Code lines are Go, Python (scripts included), Rust and web, with tests and generated code excluded.

```bash
gh pr list --state merged --limit 1000 --json title,mergedAt              # classify by the title's type
git log --since=2026-08-01 --until=2026-09-01 --numstat --format=''       # lines added per path
gh run list --branch main --workflow ci.yml --limit 60 --json conclusion  # 55 success, 5 failure
```

All five failures were in the "Docs hygiene" job, on 2026-09-08 and 2026-09-09.

## The decision log

| Date | Amendment | What it decided |
|---|---|---|
| 2026-05-10 to 05-23 | Original decision and two amendments | Order of v0.3.1–v0.3.5: memory quality, cost gate, idle-is-free, any provider, sessions |
| 2026-06-04 | Users first | Attracting users becomes the goal; confidentiality and identity deferred as invisible to users |
| 2026-06-23 | v0.3.10 | Reasoning before posting, on the same axis |
| 2026-06-28 | v0.3.11 | The autonomous channel, called the best adoption demo |
| 2026-07-25 | v0.3.12 | Axis changes to memory realism; confidentiality pulled forward; accounts bundled |
| 2026-08-02 | v0.3.13–v0.3.14 | Deferred items; per-person memory isolation |
| 2026-08-19 | v0.3.15–v0.3.16 | Speaker attribution; the audience check; scope from a sweep of the project's own topologies |

Ten tags have shipped since the users-first amendment: v0.3.6 (2026-06-04) to v0.3.15 (2026-09-06). Source: [v0.3.x sequencing](v0.3.x-sequencing.md).

## Signs of outside use

| Signal | Value |
|---|---|
| Stars, forks, watchers | 4, 0, 0 |
| Visitors, last 14 days | 224 views from 24 unique visitors |
| Where visitors came from | GitHub itself (126 views, 2 people) and Google (1 view) |
| Clones, last 14 days | 3,445 from 343 cloners — likely dominated by CI checkouts, so not evidence of users |
| Downloadable files on releases | 0 on v0.3.11, v0.3.14 and v0.3.15 |
| Image or binary publishing in CI | none |
| Web search for "Persatrix" | returns only this repository |
| Launch, outreach or user research in the repository | nothing after an April "pre-v0.2.1 announcement polish" pull request (#117) |

```bash
gh repo view --json stargazerCount,forkCount,watchers
gh api repos/mkhomutov/Persatrix/traffic/views
gh api repos/mkhomutov/Persatrix/traffic/popular/referrers
gh api repos/mkhomutov/Persatrix/traffic/clones
gh release view v0.3.15 --json assets
grep -nE 'ghcr|docker push|build-push-action|upload-release' .github/workflows/*.yml   # no match
```

The traffic endpoints need push access to the repository.

## Architecture and process facts

- **Toolchains.** Building everything needs Go, Python, Rust and Node; the quickstart lists Docker Desktop, Go 1.24+, Python 3.11+ and Rust 1.80+ ([README](../README.md)).
- **Configuration surface.** Almost 40 distinct `PERSATRIX_*` names in code and config; 71 make targets; 11 jobs in `ci.yml`, plus three single-job workflows.
- **Size caps.** 24 files sit exactly at their cap — 23 at 500 lines and one document at 3,000 words — and 101 are within 3% of it, by the size checker's own count. v0.3.16 opened with two uncuttable file splits because both files it had to edit were at 500 of 500 ([v0.3.16 plan](v0.3.16-plan.md)).
- **Dependency pins.** The Anthropic SDK is held below 1.x ([ISSUE-0144](issues/ISSUE-0144-anthropic-sdk-pinned-below-1x.md)); protobuf is held at 5.x ([ISSUE-0145](issues/ISSUE-0145-proto-toolchain-pinned-to-protobuf-5x.md)).
- **Interoperability.** No MCP client and no A2A endpoint; inbound agent interop is a draft RFC targeted at v0.4.x ([RFC 0043](rfcs/0043-inbound-agent-interop-endpoint.md)). The [glossary](ai-glossary.md#mcp-bridge) nevertheless describes the MCP bridge as an existing component.
- **Evaluation.** Six eval sets (`EVAL-MEMORY-001` to `005`, `EVAL-WORKING-001`), all golden-trace regression recipes ([RFC 0044](rfcs/0044-eval-set-golden-traces.md)). None compares against a baseline without the society machinery.
- **Cost of live acceptance runs.** v0.3.11 autonomous arc: $0.17, 99 seconds, 47,474 tokens; four-vendor arc: $0.25 ([v0.3.11 execution report](manual-tests/v0.3.11-execution-report.md)). v0.3.15 ten-leg arc: $0.13.
- **Waiting work.** Pull requests #754 and #755 (RFC 0041 Phase 1) have been open since 2026-07-18.
- **License grant.** Non-commercial use — evaluation, research, personal projects — in deployments serving fewer than 10 users and not offered commercially ([LICENSE](../LICENSE)).

```bash
git grep -ohE 'PERSATRIX_[A-Z0-9_]+' -- '*.go' '*.py' '*.rs' '*.yaml' '*.yml' | sort -u | wc -l   # 38, a few of them prefixes
grep -E '^[a-zA-Z0-9][a-zA-Z0-9_.-]*:([^=]|$)' Makefile | grep -vE '^\.' | cut -d: -f1 | sort -u | wc -l   # 71
.venv/bin/python -c "import yaml; print(len(yaml.safe_load(open('.github/workflows/ci.yml'))['jobs']))"  # 11
.venv/bin/python scripts/checks/file_size.py --near-cap    # 24 exactly at a limit, 101 within 3%
git grep -niE 'baseline|single[- ]call|ablat' -- evaluators/   # no match
```

## The market in September 2026

Public reporting, gathered 2026-09-10. Figures are as reported, not verified.

- **Agent frameworks.** LangGraph, CrewAI (role-based agents with personas), Microsoft Agent Framework (AutoGen and Semantic Kernel merged), OpenAI Agents SDK (sessions, hand-offs, guardrails), Google ADK and the Claude Agent SDK are the common production choices [1][2].
- **Agent memory.** Mem0, Letta, Zep, Cognee and Supermemory are funded; Mem0 reports $24.5M raised and tens of thousands of GitHub stars, and commentators now call persistent memory table stakes [3][4].
- **Simulated societies.** Simile, from the Stanford generative-agents research, raised a $100M Series A in February 2026 and a Series B at a reported $2B valuation; customers use its simulated populations to test products, messages and policies [5]. Open-source research frameworks include Concordia, now with an Apache-2.0 no-code builder [6], and OASIS, which scales to a million agents [7].
- **Shared channels.** Slack describes an "agent-first workspace" [8] and on 2026-08-20 announced Slack Code, which puts coding agents in project channels beside people; Microsoft positions Teams the same way [9].
- **Interoperability.** A2A joined MCP in the Linux Foundation's Agentic AI Foundation in August 2026 [10]; A2A reported more than 150 supporting organisations at its first anniversary [11].
- **Budget control.** LLM gateways such as LiteLLM, Portkey and agentgateway enforce spend limits per key or per user [12][13].
- **Consumer personas.** Character.AI, Replika and others compete on long-term memory — a separate, consumer market for "a persona that remembers you" [14].

## Sources

1. [Best multi-agent frameworks in 2026 — GuruSup](https://gurusup.com/blog/best-multi-agent-frameworks-2026)
2. [OpenAI Agents SDK guide, 2026 — StackNotice](https://stacknotice.com/blog/openai-agents-sdk-complete-guide-2026)
3. [The AI memory problem — Value Add VC](https://valueaddvc.com/blog/the-ai-memory-problem-how-startups-are-solving-for-persistent-context)
4. [AI memory solutions, Q3 2026 — Mnemoverse](https://mnemoverse.com/docs/library/ai-memory-solutions-2026-q3)
5. [Inside Simile's $2B bet — Turing Post](https://www.turingpost.com/p/simile-ai)
6. [The Concordia simulation builder is now open source — UNU C3](https://c3.unu.edu/blog/concordia-simulation-builder-open-source)
7. [OASIS: Open Agent Social Interaction Simulations — arXiv](https://arxiv.org/abs/2411.11581)
8. [Why the future of work is an agent-first workspace — Slack](https://slack.com/blog/news/agent-first-workspace-slack)
9. [Slack brings AI agents to workspaces — Forbes](https://www.forbes.com/sites/timkeary/2026/08/20/slack-brings-ai-agents-to-workspaces-but-can-it-take-on-teams/)
10. [Google's A2A protocol gets a new home — Axios](https://www.axios.com/2026/08/17/a2a-agentic-ai-foundation-open-ai-standards)
11. [A2A surpasses 150 organizations — Linux Foundation](https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year)
12. [Budget and spend limits — agentgateway](https://agentgateway.dev/docs/standalone/latest/llm/cost-controls/budget-limits/)
13. [AI gateway setup 2026: LiteLLM, Portkey and Kong — Spheron](https://www.spheron.network/blog/ai-gateway-litellm-portkey-kong-gpu-cloud/)
14. [AI relationships 2026: Replika, Character.AI — explainx.ai](https://www.explainx.ai/blog/ai-relationships-companionship-replika-character-ai-2026)

## Related documentation

- [Independent strategy review](strategy-independent-review-2026-09.md) — the assessment these numbers support.
- [v0.3.x sequencing](v0.3.x-sequencing.md) — the decision log summarised above.
- [Documentation guide § Size limits](documentation-guide.md#size-limits) — the caps referred to here.
