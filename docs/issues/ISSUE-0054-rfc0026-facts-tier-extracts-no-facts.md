---
id: ISSUE-0054
summary: RFC 0026 facts tier extracts zero facts at interaction close. Three chained close-path bugs — markdown code fence (fixed a6c3332), action-envelope-only input with no message body (fixed 71a9045), and a max_tokens=256 cap on the combined summarise+extract call that truncated any multi-fact envelope mid-JSON so it never parsed (fixed 90c0d5b). All three fixed and verified live — facts tier extracts and persists facts at interaction close.
status: resolved
severity: high
area: agents/persona_runtime
created: 2026-05-17
closed: 2026-05-17
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

> 2026-05-17 — **root-cause fix landed (this branch).** The re-diagnosed
> root cause is fixed: the close-path summariser/extractor now receives
> the inbound message content.
>
> *Fix.* Three coordinated changes (TDD):
>
> - `episode_routing.py::_handle_multi_turn_event` — stash the inbound
>   message body (`event.payload["content"]`) on the turn payload under
>   a new `text` key, alongside the existing structural envelope.
> - `summarize_close.py::_interaction_to_entries` — project that `text`
>   into the `MemoryEntry` content fed to `MemoryFacade.compress`, so
>   the combined summarise + extract LLM call finally sees the message
>   body rather than just the `"Event: … → Actions: […]"` envelope.
> - `episode_routing.py::_persist_closed_interaction` — strip `text`
>   from each turn payload before it lands in `episodes.context_json`.
>   The body is carried only transiently on the in-memory interaction
>   for the Phase-2 summariser; RFC 0020 §D's "the episodic store does
>   not double as a message log" property is preserved, so the existing
>   `test_closed_interaction_context_does_not_embed_message_body`
>   contract test stays green. RFC 0020 §D + the `Turn` docstring are
>   amended to document the transient in-memory carry.
>
> *Automated verification — green.* New TDD tests, red before the fix:
>
> - `test_summarize_close_helpers.py::TestInteractionToEntriesCarriesMessageText`
>   — `_interaction_to_entries` projects `payload["text"]` into the
>   entry content; legacy payloads with no `text` key still project.
> - `test_facts_extractor_message_content.py::TestExtractorReceivesMessageContent`
>   — full close path: distinctive message bodies reach the summariser
>   prompt, a content-aware extractor consequently populates the
>   `facts` table, and the body does **not** leak into `context_json`.
>   The second test reproduces the exact ISSUE-0054 symptom (empty
>   `facts` table) pre-fix and pins it fixed.
>
> The full close-path / facts / interaction-lifecycle regression suites
> pass; `ruff` + `mypy` clean; `scripts/checks/file_size.py --strict`
> passes (the new test class was split into its own file).
>
> *Outstanding before this can move to `resolved`.* A fresh live
> `MT-MEMORY-005` run on the Docker Compose stack to confirm the
> `facts` table populates with real `(subject, predicate, object)`
> tuples after an idle-gap close, plus the
> [v0.3.1-execution-report.md](../manual-tests/v0.3.1-execution-report.md)
> F-1 update. The fence fix and this content fix together close the
> code-level defect; the live re-run is the release-gate confirmation.

> 2026-05-17 — **live re-run after the content fix ([commit 71a9045](https://github.com/mkhomutov/Persatrix/commit/71a9045)).
> Issue stays OPEN — a third, distinct bug is now the headline cause:
> the combined summarise+extract LLM call truncates.**
>
> *Automated.* All 54 fix-commit tests green — the 8 integration tests
> in `test_facts_extractor_close.py` + `test_facts_extractor_message_content.py`
> (incl. `TestExtractorReceivesMessageContent`, the content-fix
> root-cause test) and the 46 unit tests in
> `test_summarize_close_helpers.py` / `test_extractor.py` /
> `test_envelope_parse_observability.py`.
>
> *Live.* Docker stack rebuilt from this branch; drove a four-turn
> establish scenario at `ember-owl` (user `issue54-carol`: seven-year-old
> daughter Mira / dislikes phone calls, prefers async / budget-spreadsheet
> commitment / rate-card renegotiation) and triggered an RFC 0020
> idle-gap close.
>
> *Content fix — confirmed working.* The closed `issue54-carol` episode
> summary is rich prose naming Mira, the rate card, the budget
> spreadsheet, and the async preference — proof the inbound message
> bodies now reach the summariser/extractor prompt. The LLM response
> even **contains a populated `facts` array** with five real tuples
> (`Carol has_child_named Mira`, `Mira has_age 7`, `Carol prefers
> "text or async communication"`, `Carol dislikes "phone calls"`, …).
> Pre-fix this array was always `[]`. Commit 71a9045 does what it
> claims.
>
> *Facts half — still 0 for `issue54-carol`.* `SELECT COUNT(*) FROM
> facts` finds no `carol` rows. Root cause re-diagnosed (third bug in
> the chain): the combined summarise+extract call in
> `summarize_close.py` caps `max_tokens=256`. That literal is unchanged
> from the RFC 0020 PR 4 *summary-only* era; RFC 0026 PR 2 appended the
> `facts`-array output to the **same** call without raising the cap.
> A conversation rich enough to yield several facts produces a
> two-output envelope larger than 256 output tokens, so the LLM
> response is **truncated mid-JSON**. The persisted `episodes.summary`
> for `carol` is the raw 828-char fenced envelope, cut off at the
> fifth fact's `"certainty": 0.9` — no closing brackets. Truncated
> JSON fails `json.loads`, so `split_combined_response` raises
> `FactsParseError(reason="truncated")`, the backward-compat branch
> commits the raw text as the summary, and `finalize_closed_interaction`
> skips the facts dispatch because `facts_raw is None`.
>
> *Corroboration.* During the same run the startup catch-up replayed
> stale events; one thin replayed conversation (`mt-chat-004-user`)
> closed with a **clean, fence-free** prose summary **and** extracted
> one fact — `(mt-chat-004-user, plans_to, "start a new project",
> 0.9)`. So the end-to-end facts path *does* populate the table when
> the envelope fits inside 256 tokens. The defect is purely the token
> cap vs. envelope size; the fence-unwrap fix (a6c3332) and the
> content fix (71a9045) are both still correct and still needed. The
> ` ```json `-fenced malformed summary is now a *secondary* symptom of
> truncation — truncated JSON cannot be parsed regardless of the fence.
>
> *Next fix.* Raise `max_tokens` on the combined summarise+extract
> call in `summarize_close.py` (256 → enough headroom for the prose
> summary plus a multi-fact array — on the order of 1024), ideally as
> a named constant beside `SUMMARIZATION_TARGET_TOKENS`. Until then the
> `facts` table stays empty for any non-trivial interaction.
>
> *Adjacent (unchanged from the prior re-run).* The catch-up storm
> again raised `sqlite3.OperationalError: cannot commit transaction -
> SQL statements in progress` in `_persist_closed_interaction` for one
> replayed scope (`dm:ember-owl:my-custom-user`, backfilled to
> `[summary pending]`). Still a separate close-path concurrency bug
> under catch-up-storm load, worth its own ticket.

> 2026-05-17 — **RESOLVED. Live re-run after the token-cap fix
> ([commit 90c0d5b](https://github.com/mkhomutov/Persatrix/commit/90c0d5b),
> `SUMMARIZATION_MAX_OUTPUT_TOKENS` 256 → 1024). The RFC 0026 facts
> tier now extracts and persists facts at interaction close.**
>
> *Automated.* The targeted close-path suites are green — 85 tests:
> 38 integration (`test_facts_extractor_close.py`,
> `test_facts_extractor_message_content.py`,
> `test_summarize_on_close_phases.py`, `test_interaction_multi_turn.py`)
> + 47 unit (`test_summarize_close_helpers.py`, `test_extractor.py`,
> `test_envelope_parse_observability.py`). Includes the new
> `test_truncated_envelope_returns_summary_failure_not_raw_text`, which
> pins that an envelope-shaped parse failure routes to the
> unavailable-summary fallback rather than committing raw broken JSON
> as the episode summary.
>
> *Live.* Docker stack rebuilt from this branch (`agent-ember-owl`
> image rebuilt; orchestrator + ember-owl brought up). Drove a
> five-turn establish scenario at `ember-owl` over the REST chat
> endpoint (`POST /api/v1/agents/ember-owl/chat`, user `issue54-dave`:
> daughter Mira age 7 / dislikes phone calls, prefers async /
> budget-spreadsheet commitment / rate-card renegotiation), then sent
> a later message past the idle window to trigger an RFC 0020 idle-gap
> close. The agent's `memory.interaction_idle_timeout_sec` was
> shortened to 60s for the run so the idle window fires in seconds
> rather than the 600s default — a test-only `config/agents.yaml`
> change, since reverted; the close-path code under test is unaffected
> by the timeout value.
>
> *Result — both halves fixed.*
>
> - **Facts half — fixed.** `SELECT COUNT(*) FROM facts` → **5** after
>   the close, every row carrying the `source_interaction_id` of the
>   closed `issue54-dave` interaction:
>   `(dave, has_child_named, "Mira", 0.95)`,
>   `(mira, has_age, "7", 0.95)`,
>   `(dave, dislikes, "phone calls", 0.95)`,
>   `(dave, prefers, "asynchronous communication", 0.95)`,
>   `(dave, committed_to, "sending budget spreadsheet tomorrow morning", 0.9)`.
>   This is precisely the multi-fact envelope (5 tuples) that overran
>   the old 256-token cap and truncated mid-JSON in the `issue54-carol`
>   re-run; with the cap at 1024 the envelope fits, parses, and
>   persists. The agent emitted 5 `fact.store` log lines under one
>   trace — the "no fact-extraction log lines" symptom is gone.
> - **Summary half — fixed.** The closed episode's `summary` is clean
>   448-char prose with **no ` ```json ` fence**, naming Mira, the
>   async preference, the budget spreadsheet, and the rate-card
>   renegotiation.
>
> No `WARNING` / `ERROR` / `Traceback` lines in the agent's close-path
> logs — the close ran clean, with no truncation, no envelope-parse
> failure, and no summary failure.
>
> All three bugs in the ISSUE-0054 chain are now fixed and verified
> live: the markdown fence ([a6c3332](https://github.com/mkhomutov/Persatrix/commit/a6c3332)),
> the action-envelope-only input
> ([71a9045](https://github.com/mkhomutov/Persatrix/commit/71a9045)),
> and the 256-token truncation cap
> ([90c0d5b](https://github.com/mkhomutov/Persatrix/commit/90c0d5b)).
> Status → `resolved`. The full five-leg `MT-MEMORY-005` F-1 re-check
> on the release-candidate tip remains scheduled for the v0.3.1
> release-prep Track B final pre-tag verification.
>
> *Adjacent (unchanged).* This run used a minimal stack (orchestrator
> + one agent) with no startup catch-up storm, so the separate
> close-path `sqlite3.OperationalError` concurrency bug flagged in the
> prior two re-runs did not surface; it is untouched by this fix and
> still warrants its own ticket.
