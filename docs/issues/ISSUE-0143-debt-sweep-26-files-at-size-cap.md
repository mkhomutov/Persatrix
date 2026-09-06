---
id: ISSUE-0143
summary: "Twenty-six files sit at exactly their size cap at the v0.3.15 post-release follow-up, over the release-cycle threshold of twenty — the next edit to any of them is a split or a trim rather than a one-liner, and one of them is the file ISSUE-0137 needs to grow a test"
status: open
severity: low
area: repo
created: 2026-09-06
refs:
  - docs/methodology/release-cycle.md
  - docs/methodology/enforcement-matrix.md
  - docs/issues/ISSUE-0137-episode-write-boundary-cannot-express-the-records-principal.md
---

## Summary

`python scripts/checks/file_size.py --near-cap`, read at the v0.3.15
post-release follow-up, reports **26 files at exactly their cap** (25 code files
at 500/500 lines, one document at 3 000/3 000 words) and 102 within 3% of it.
The [release-cycle rule](../methodology/release-cycle.md#the-debt-sweep) files
this issue at twenty or more.

## Context

The caps are a cliff, not a gradient: a file at 500/500 cannot take a one-line
fix. Every edit to one becomes either a split or a trim, and trimming is what
produced the shape a sweep on 2026-08-29 first identified — 29 code files at
exactly 500 lines against a background of ~3.5 per line-count bucket. That
distribution does not occur naturally; it is the fingerprint of repeatedly
cutting rationale to fit.

The 26, by area:

```
agents/                     10   base.py, memory/shared_pool.py,
                                 observability/metrics.py,
                                 persona_runtime/{action_loop,close_path,memory_context}.py,
                                 response_gate.py, salience_bid.py,
                                 server_persona.py, server_servicers.py
internal/ + cmd/             7   channels/{config,autonomous_acceptance_test,synthesis_close_test}.go,
                                 observability/zapenc/encoder.go,
                                 server/{server,chat_handler_test}.go,
                                 cmd/orchestrator/main.go
tests/                       4   integration/test_conversational_continuity.py,
                                 unit/python/{test_end_interaction_vote_action,
                                 test_fact_store_reinforcement,
                                 test_session_recall_default_path}.py
web/ + cli/                  4   web/src/lib/api.test.js,
                                 web/src/panels/{ChannelSettings.test.js,ChannelTimeline.svelte},
                                 cli/src/commands/channel_config_tests.rs
docs/                        1   memory-quality-roadmap.md
```

## Impact

Ordinary maintenance in these files costs a refactor it did not budget for, and
the cost lands on whoever happens to touch the file next rather than on whoever
filled it.

**One case is already blocking named work.** `agents/persona_runtime/close_path.py`
is at 500/500, and it is the file
[ISSUE-0137](ISSUE-0137-episode-write-boundary-cannot-express-the-records-principal.md)
concerns: the v0.3.15 tenant partition rests on a single
`with principal_scope(...)` there, unpinned by any test. Adding that guard means
splitting the file first. A cap that turns "add a regression test" into "refactor
a load-bearing module" is a cap actively deterring the safer change.

Six more sit within ten lines of the cliff, `close_path.py`'s own neighbours
among them.

## Proposed fix / investigation path

Per the release-cycle rule, the next master plan carries a **cuttable Workstream
D — debt sweep**:

1. **Split the at-cap files first** — pure structural PRs, no behaviour change,
   each under 500 lines. Split at a real seam and name the new file for what it
   holds; do not shave comments to buy headroom, which is the move that created
   this.
2. **Prioritise by blocked work, not by count.** `close_path.py` goes first
   because ISSUE-0137 is waiting on it. `agents/base.py` and
   `internal/server/server.go` are next by blast radius.
3. **Test files split as readily as source** — 8 of the 26 are tests, and a test
   file at its cap silently discourages adding cases, which is the worst possible
   place to apply that pressure.
4. **Re-read the count at the next follow-up.** If a sweep ran and the number has
   not moved, the cap itself is the thing to revisit, not the files.

## Notes

Filed at the v0.3.15 post-release follow-up, the first application of the
[debt-sweep rule](../methodology/release-cycle.md#the-debt-sweep) since
[#861](https://github.com/mkhomutov/Persatrix/pull/861) recorded it. Count at
filing: **26 at cap, 102 within 3%**.
