---
id: ISSUE-0054
summary: RFC 0026 facts tier extracts zero facts at interaction close — the close-path summariser/extractor LLM output is stored with its literal ```json markdown fence, breaking both episode summaries and fact extraction
status: open
severity: high
area: agents/persona_runtime
created: 2026-05-17
refs:
  - docs/rfcs/0026-declarative-facts-tier.md
  - docs/manual-tests/MT-MEMORY-005-dementia-test.md
  - docs/manual-tests/v0.3.1-execution-report.md
  - agents/persona_runtime/summarize_close.py
  - agents/persona_runtime/fact_extractor.py
  - agents/persona_runtime/episode_routing.py
---

## Summary

The RFC 0026 declarative facts tier — a headline v0.3.1 deliverable —
extracts **zero facts** in the live integrated stack. Across a full
five-interaction `MT-MEMORY-005` dementia-test run (plus ~20 other chat
turns from the same session), the persona memory `facts` table stayed
empty. The close-path summariser *does* run, but its episode summaries
are stored with a literal ` ```json ` markdown code fence still attached
— strong evidence that the close-path LLM returns markdown-fenced JSON
that is never unwrapped before parsing, so neither a clean summary nor
any extracted facts ever land.

This is a release blocker for v0.3.1: RFC 0026 ships, but non-functional.

## Context

Found during the v0.3.1 manual-test execution
([docs/manual-tests/v0.3.1-execution-report.md](../manual-tests/v0.3.1-execution-report.md)
finding **F-1**), running `MT-MEMORY-005` live on the Docker Compose
stack — persona `ember-owl`, test user `dementia-bob`, five interactions
with two ≥ 11-minute idle gaps so RFC 0020 idle-gap closure fires.

Observed on the live `agent-ember-owl` container's
`/app/data/memory.db`:

| Observation | Detail |
|---|---|
| `facts` table is empty | `SELECT COUNT(*) FROM facts` → `0`, after the full run and ~25 chat turns across 6 users. Schema + migration present (columns `fact_id, agent_id, subject, predicate, object, certainty, source_interaction_id, asserted_at, last_recalled_at, superseded_by, session_id`). |
| The interaction-close path *does* run | `11` `episodes`, `10` `interactions`, `10` `relationships` rows written at idle-gap close. `dementia-bob` shows 2 closed interactions, so RFC 0020 closure fired correctly. |
| Episode summaries are malformed | Stored `episodes.summary` values begin with a literal markdown fence, e.g. ``` ```json\n{\n  "summary": "dementia-bob sent two channel messages and completed associated tasks in the dm:dementia-bob:ember-owl scope..." ``` — the raw, un-parsed LLM response was stored verbatim as the summary. |
| No fact-extraction log lines | `docker compose logs agent-ember-owl` grepped for `fact` / `extract` / `0026` returns nothing — the extractor either never runs or fails silently. |

### Likely root cause (hypothesis — not yet confirmed in code)

The interaction-close path asks the LLM for a structured object (summary
+ extracted facts). LLMs routinely wrap structured output in a markdown
code fence:

````
```json
{ "summary": "...", "facts": [ ... ] }
```
````

The close-path consumer appears to expect a *bare* JSON object and does
not strip the ` ```json `…` ``` ` fence before parsing. One unstripped
fence breaks both outputs at once:

- **Summary** — instead of the parsed `summary` field, the entire raw
  fenced response is stored as `episodes.summary` (matches the observed
  ` ```json `-prefixed summaries).
- **Facts** — the JSON parse fails (or is never attempted), so the
  `facts` array inside the response is never read out and nothing is
  written to the `facts` table.

The investigation should start in
[`agents/persona_runtime/summarize_close.py`](../../agents/persona_runtime/summarize_close.py)
and [`agents/persona_runtime/fact_extractor.py`](../../agents/persona_runtime/fact_extractor.py)
(the RFC 0026 PR 2 extractor wired at interaction close), and the
two-phase close orchestrator `_persist_closed_interaction` /
`finalize_closed_interaction` in
[`agents/persona_runtime/episode_routing.py`](../../agents/persona_runtime/episode_routing.py).

### Relation to ISSUE-0052

[ISSUE-0052](ISSUE-0052-persona-conversational-working-memory-gap.md)
adjacent finding #1 anticipated that fact extraction is single-message
and that *referential* follow-ups would not extract — but it assumed
**self-contained** statements still extract turn-locally ("Session 1's
hiking / dog / married / child *did* persist"). This issue shows that
assumption no longer holds in the v0.3.1 build: even a self-contained
statement (`"I'm picking up my daughter Mira"`) produces **no fact at
all**. The facts tier is not partially working — it is fully inert.
Adjacent finding #2 (episode close / `summarize_close.py`) points at the
same code area.

## Impact

**Severity: high.** RFC 0026 (declarative facts tier) is a headline
v0.3.1 deliverable and the basis of the v0.3.1 "memory works" story.
With this defect:

- The persona has **no long-term structured memory** of people. Every
  cross-interaction fact (`has_child_named`, `dislikes`, `prefers`,
  `committed_to`, the `self`-subject preferences) is silently dropped.
- `MT-MEMORY-005`'s legs still pass *behaviourally* — but only because
  the short (~8-turn) dementia-test transcript fits inside the RFC 0034
  conversation window, so recall is carried by the live transcript, not
  the facts tier. In any conversation long enough for early turns to
  scroll out of the window, the persona has nothing to fall back on —
  the exact dementia-test failure mode RFC 0026 was built to fix.
- The malformed ` ```json `-fenced episode summaries also degrade
  episodic recall: the recall path indexes/ranks garbled summary text.
- The v0.3.1 release-prep counted `MT-MEMORY-005` Legs 1/2/5 as passing
  *"via the facts tier"*; that mechanism does not exist in the shipped
  build.

## Proposed fix / investigation path

1. Reproduce with `PERSATRIX_MEMORY_PROVENANCE=1` set on the agent
   container (`MT-MEMORY-005` §Telemetry) to confirm the facts slice is
   empty at trigger turns.
2. In `summarize_close.py` / `fact_extractor.py`, locate where the
   close-path LLM response is parsed. Strip a leading/trailing markdown
   code fence (` ```json `…` ``` ` or bare ` ``` `) before
   `json.loads`, or constrain the prompt to forbid the fence. A
   defensive unwrap (regex or a `.strip("`")`-style helper) is the
   minimal fix; a tool-use / structured-output call is the robust one.
3. After the fix, confirm on a fresh `MT-MEMORY-005` run that
   `episodes.summary` holds clean prose (no fence) **and** the `facts`
   table is populated with `(subject, predicate, object)` tuples after
   an interaction closes — e.g. `(dementia-bob, has_child_named,
   "Mira")`, `(dementia-bob, dislikes, "phone calls")`,
   `(self, has_preference, ...)`.
4. Re-run `MT-MEMORY-005` and update
   [v0.3.1-execution-report.md](../manual-tests/v0.3.1-execution-report.md)
   F-1.

## Notes

> 2026-05-17 — initial capture during the v0.3.1 manual-test execution
> (release-prep PR 1). Logged as finding F-1 in the v0.3.1 execution
> report. The dementia-test acceptance behaviour holds via the RFC 0034
> conversation window, so this did not block the report's literal
> release gate — but RFC 0026 ships non-functional, which the release
> owner must resolve before tagging v0.3.1.

> 2026-05-17 — **re-run after the fence fix ([commit a6c3332](https://github.com/mkhomutov/Persatrix/commit/a6c3332),
> `_strip_code_fence` in `fact_envelope.py`). Issue stays OPEN — the
> facts tier is still inert.**
>
> *Automated.* The 6 fix-commit tests are green:
> `test_facts_extractor_close.py::TestExtractorFencedEnvelope`
> (full close path, fenced envelope → clean summary + facts persisted),
> plus the 5 unit tests in `test_extractor.py` /
> `test_envelope_parse_observability.py`.
>
> *Live.* Docker stack rebuilt with the fix; drove a four-turn
> establish scenario at `ember-owl` (user `issue54-bob`: daughter Mira /
> dislikes phone calls / budget-spreadsheet commitment / rate-card
> topic) and triggered an RFC 0020 idle-gap close. Result on
> `memory.db`:
>
> - **Summary half — fixed.** The new `episodes.summary` is clean prose
>   with **no ` ```json ` fence** (*"Issue54-bob sent four channel
>   messages, each triggering task completion actions. … no substantive
>   discussion or decisions recorded."*). The 8 pre-fix episodes in the
>   same DB still carry the literal fence — the fix changed the
>   behaviour. The fence-unwrap fix is confirmed working.
> - **Facts half — still 0.** `SELECT COUNT(*) FROM facts` → `0` after a
>   full establish + close.
>
> *Root cause re-diagnosed.* The markdown fence was a real but
> **secondary** bug. The facts tier extracts nothing because the
> close-path summariser/extractor is fed **per-turn action-envelope
> strings, not message content**. Each closed turn's payload carries
> only `summary = "Event: channel_message → Actions: [...]"` plus
> structural fields — `episode_routing.py:350-381` deliberately drops
> the message body, citing RFC 0020 §D *"per-turn message text is not
> stored in episodes"*, and `summarize_close.py::_interaction_to_entries`
> reads only `payload["summary"]`. The closed episode's `context_json`
> confirms it: all four turns store the identical action-envelope
> string and the words "Mira" / "phone calls" / "rate card" appear
> nowhere. So the RFC 0026 extractor's LLM input has no facts to
> extract — it correctly returns `facts: []`. The ` ```json `-fence
> unwrap is necessary but **not sufficient**; the headline symptom
> (zero facts at interaction close) persists.
>
> *Next fix.* The close-path extractor needs access to the actual
> message content — e.g. extract from the RFC 0034 conversation window /
> channel store rather than from interaction-turn payloads, or amend
> RFC 0020 §D. Until then the `facts` table stays empty regardless of
> the fence.
>
> *Adjacent.* During the re-run the startup channel catch-up replayed
> ~68 stale events and the concurrent idle-closes raised
> `sqlite3.OperationalError: cannot commit transaction - SQL statements
> in progress` in `_persist_closed_interaction` (two old scopes failed
> to close; the janitor backfilled them to
> `[interaction summary unavailable]`). A single uncontended close (the
> `issue54-bob` interaction) succeeded cleanly, so this is a close-path
> concurrency bug under catch-up-storm load — separate from this issue,
> worth its own ticket.
