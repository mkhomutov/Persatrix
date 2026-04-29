# RFC 0020 — Interaction Lifecycle: Dialogue Boundaries and Episode Granularity

**Type**: architecture
**Status**: 🚧 Implementing
**Author**: Maksim Khomutov
**Date**: 2026-04-25
**Target**: v0.3.0
**Depends on**: RFC 0005 (episode model, relationship memory); RFC 0017 (per-event memory budget — composes, no API change required)
**Consumed by**: RFC 0008 (§D Context Packaging and Compression Pipeline); RFC 0011 (§E Memory Integration)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Vocabulary](#a-vocabulary)
  - [B. Boundary Detection](#b-boundary-detection)
  - [C. Interaction Lifecycle States](#c-interaction-lifecycle-states)
  - [D. Storage Model](#d-storage-model)
  - [E. Memory Injection Contract](#e-memory-injection-contract)
  - [F. Relationship Memory Updates](#f-relationship-memory-updates)
  - [G. Per-Channel Scoping](#g-per-channel-scoping)
  - [H. Reflection Nudges and Counters](#h-reflection-nudges-and-counters)
  - [I. Backfill and Migration](#i-backfill-and-migration)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Today, every event-handler invocation produces one episode. A twelve-message back-and-forth becomes twelve noisy episodes instead of one coherent memory. This RFC introduces **Interaction** as a first-class concept — a bounded sequence of turns between two or more participants — and makes the *interaction*, not the message, the unit of summarization, episodic storage, and relationship-memory updates.

The design adds a boundary detector (idle-gap + structural triggers, with topic-shift deferred), four lifecycle states (`open` → `closing` → `closed` → `summarized`), and a storage model where the existing `episodes` table records one row per closed interaction rather than one row per turn. Working memory continues to hold the live turn buffer for the open interaction; episodic recall returns only closed interactions.

## Motivation

Persatrix's persona memory stack ([RFC 0005](0005-persona-agent-memory.md)) was built around a synchronous request/response shape: an event arrives, the agent reasons, an episode is written. This works for ticks and one-shot tool calls, but it does not match the texture of a real exchange. Three concrete problems result:

1. **Episodic memory becomes shredded.** A negotiation between two agents that takes ten turns produces ten near-duplicate episode summaries (`"Event: CHANNEL_MESSAGE → Actions: [SEND_CHANNEL_MESSAGE]"`). Recall surfaces ten partial fragments instead of one coherent "we negotiated X with Bob" memory. This degrades both the value of recall and the relevance scoring built in RFC 0017.

2. **Relationship memory updates per message instead of per outcome.** [RFC 0005 §`record_interaction`](0005-persona-agent-memory.md#L835) increments `interaction_count` and adjusts trust on each turn. This is wrong by definition: a friendly back-and-forth followed by a betrayal produces nine "positive interactions" and one "negative" instead of one negative interaction. Trust dynamics are blurred by message-rate noise.

3. **Channels (RFC 0011) multiply both problems by N.** A six-participant `#planning` channel with a normal exchange produces dozens of episodes per real conversation across all members' isolated memory stores. The compression pipeline ([RFC 0008 §D](0008-agent-memory-context-optimization.md#L141)) inherits this granularity and either compresses too aggressively (losing the negotiation arc) or too late (paying for many tiny compressions).

The user-facing v0.3.0 promise — "watch agents talk, negotiate, and form opinions over time" — fails quietly without this. Agents will appear to forget what they just discussed, because the recall layer returns ten low-relevance fragments instead of one summary that says what was actually agreed.

## Goals

1. **Define Interaction** as a first-class boundary alongside Message and Turn, with a precise lifecycle.
2. **Detect interaction boundaries** using a hybrid policy (structural + idle-gap), with topic-shift detection scaffolded but deferred.
3. **Summarize at interaction granularity**: one persisted episode per closed interaction, not per turn.
4. **Update relationship memory at interaction granularity**: trust deltas computed from the interaction outcome, not per-message.
5. **Compose with existing memory budget** ([RFC 0017](0017-persona-memory-injection-budget.md)) without API churn — the budget allocator stays per-event; what changes is what episodic recall returns.
6. **Define semantics for channels** (RFC 0011) so threads, DMs, and group conversations each have a defensible interaction-boundary policy.
7. **Provide a non-disruptive migration path** for existing per-event episodes already in production agent databases.

## Non-Goals

- **Real-time topic-shift detection** during an open interaction (LLM-judged or embedding-based). Scaffolded but not implemented in v0.3.0; deferred to a later phase.
- **Cross-agent shared interaction state.** Each agent records its own view of the interaction in its own episodic memory. Reconciling participants' views into a canonical interaction transcript is a v0.4.0+ concern.
- **Replaying or rewriting historical episodes** in agent databases. Existing rows stay as-is; the new model takes effect for newly-created interactions only.
- **Changing the working memory token budget contract** ([RFC 0017](0017-persona-memory-injection-budget.md)). The `MemoryBudget` greedy-fill loop is reused unchanged; only the inputs (recall results) shift from per-turn to per-interaction.
- **Persisting open-interaction state across orchestrator restarts as a hard guarantee.** Restart behavior is specified in §C but treated as best-effort, not durable transaction state.

---

## Design / Implementation

### A. Vocabulary

The four terms below are the canonical vocabulary across this RFC, the SQL schema, and any code that touches the episode pipeline. Existing code that uses "interaction" loosely (e.g., `record_interaction` in relationship memory) is reinterpreted in terms of these definitions.

| Term | Definition | Storage |
|------|------------|---------|
| **Message** | A single utterance produced by one participant — a string with a sender, timestamp, and (optionally) a channel/thread. The atomic unit of communication. | `messages` table per channel ([RFC 0011 §B](0011-channels-bridges.md)) for channel content; transient for human chat (RFC 0016). |
| **Turn** | One event → action cycle of one agent. May produce zero, one, or several outbound messages. Already implicit in the dispatch loop ([agents/dispatch.py](../../agents/dispatch.py)); not separately persisted. | Not persisted as such; observable via traces (RFC 0019). |
| **Interaction** | A bounded sequence of turns between two or more participants, sharing a topic and time window. Has exactly one start, one end, and one summary. The unit of episodic memory and relationship updates. | `episodes` table, one row per closed interaction (§D). |
| **Episode** | The persisted record of a closed interaction: summary, participants, outcome, importance, timestamps. Replaces the current "one episode per event-handler call" model. | `episodes` table (existing — schema additions in §D). |

**Naming clarification.** "Episode" is retained from RFC 0005 to avoid rippling rename churn through `EpisodicMemory`, `store_episode`, and the existing schema. Conceptually, an episode is now the artifact of a closed interaction. The two terms are not synonyms: an interaction has lifecycle states, an episode does not (it is the final, immutable record).

### B. Boundary Detection

An interaction's **start** is unambiguous: the first turn that arrives without an open interaction in the relevant scope (see §G for how scope is defined per channel type) starts a new interaction.

An interaction's **end** is the hard problem. v0.3.0 ships a hybrid detector with three triggers, evaluated in priority order:

1. **Structural close (highest priority, deterministic)**:
   - Channel thread is archived or explicitly closed.
   - Participant leaves the channel (DM or group).
   - Agent emits an explicit `END_INTERACTION` action (new action type — optional; see Open Question 3).
   - Process is shutting down cleanly — open interactions are flushed to `closing` state with their current turns.

2. **Idle-gap timer (default trigger)**:
   - No turn has arrived in the interaction's scope for `idle_timeout` seconds. Default: **600s (10 minutes)**, configurable per channel via `channel.idle_timeout_sec` and globally via `optimization.yaml`'s `interaction.idle_timeout_sec`.
   - Channel-level override takes precedence over global. DMs default to the global value; threads inherit from their parent channel.
   - The timer resets on every new turn within the interaction's scope.

3. **Topic-shift detection (scaffolded; deferred)**:
   - A pluggable interface `TopicShiftDetector` is defined in Phase 1 but the default implementation returns `False` in v0.3.0. Phase 4 (post-v0.3.0) wires an embedding-similarity or LLM-judge implementation behind a config flag.

The mental model: structural triggers are immediate and free; idle-gap is the workhorse and pays only a timer cost; topic-shift is the future improvement that pays an LLM/embedding cost per turn.

### C. Interaction Lifecycle States

```
   first turn arrives          boundary tripped         summary persisted
   ───────────────────►  open  ──────────────►  closing  ──────────────► closed/summarized
                                                   │
                                                   │ a new turn during closing
                                                   │ does not reopen — it starts
                                                   │ a fresh `open` interaction
                                                   ▼ (see "reopen window" below)
```

| State | Meaning | Storage |
|-------|---------|---------|
| `open` | Active interaction; turns are accumulating in working memory. Idle timer is armed. | In-memory `InteractionTracker` per agent, keyed by scope (§G). Not persisted as `open` rows. |
| `closing` | Boundary tripped; summary call is enqueued or in flight. Working-memory turns are frozen for this interaction. | A `closing` row is written to `episodes` with `closed_at` set but `summary` empty, so a restart can reconcile. |
| `closed` / `summarized` | Summary persisted; row is now eligible for episodic recall. | Final `episodes` row with non-empty `summary`. |

**Reopen window.** If a new turn arrives in the same scope while the interaction is `closing` (e.g., during the seconds between the timer firing and the summary call returning), the reopen rule is: **do not reopen**. The just-closing interaction summarizes as-is, and the new turn starts a fresh interaction. This sacrifices some semantic fidelity (a participant who was just slow to reply gets split across two episodes) for a much simpler concurrency model — no race between summary completion and turn arrival, no rollback paths.

**Restart behavior.** On orchestrator restart with an `open` interaction in memory: the in-memory tracker is lost, the stored channel/conversation history is intact, and the *next* arriving turn starts a new interaction. Pre-restart turns are recoverable from message storage but are not auto-summarized as a closed interaction. This is acceptable because (a) restarts are expected to be rare relative to interaction frequency, and (b) the alternative — durable open-interaction state — adds a transactional contract that's not worth its complexity for v0.3.0.

Composed with RFC 0011's catch-up history fetch ([RFC 0011 OQ #8](0011-channels-bridges.md#open-questions)), this leaves a small sliver of episodic gap on long-lived channels that survive a restart: pre-restart turns are visible to the agent for one turn (via the on-startup history fetch) but are not attached to any interaction and therefore do not produce an episodic summary. Both RFCs accept the gap independently; the gap is bounded by the on-startup fetch cap (50 messages per subscribed channel in v0.3.0) and is observable via the `interactions.opened` counter showing no interaction-open event for the recovered turns.

**Closing rows.** A row that's been in `closing` state for more than `closing_grace_sec` (default 300s) without a successful summary is treated as failed. A janitor (Phase 2) writes a fallback summary `"[interaction summary unavailable]"` and transitions the row to `closed` so it doesn't block recall. Failed-summary count is exported as a metric.

**Summary text by phase.** The placeholder shape differs by lifecycle stage to keep `"[interaction summary unavailable]"` as the unique signal for "summarization broke":

| Stage | Closure shape | `summary` value |
|-------|---------------|-----------------|
| Phase 1 | Single-turn (`turn_count=1`) — TICK or tool-only events routed through the tracker | The same per-event summary text the pre-RFC episode would have carried (behavioral parity per Phase 1 deliverable 4). No LLM call in this phase. |
| Phase 2 onward | Multi-turn (`turn_count>1`), summary call succeeds | LLM-generated summary (`context_management.summarization.model`). |
| Phase 2 onward | Multi-turn, summary call times out or errors | `"[interaction summary unavailable]"` written by the janitor — exclusive marker for the failure path. |

Recall queries treat the Phase 1 single-turn rows as full-fidelity episodes (they carry the same content the pre-RFC per-event store did) and the failure placeholder as low-fidelity (the row is preserved for shape, not for content). The `turn_count > 1` boost in §I applies regardless of whether the summary is LLM-generated or the failure placeholder; the placeholder is the loss-of-content signal, not a recall-eligibility signal.

### D. Storage Model

The `episodes` table from [RFC 0005 §`episodes`](0005-persona-agent-memory.md#L736) is extended additively. No existing column is changed in semantics; four new columns are added:

```sql
ALTER TABLE episodes ADD COLUMN interaction_id TEXT;        -- ULID, unique per interaction
ALTER TABLE episodes ADD COLUMN started_at REAL;            -- first turn timestamp
ALTER TABLE episodes ADD COLUMN closed_at REAL;             -- boundary-trip timestamp
ALTER TABLE episodes ADD COLUMN turn_count INTEGER;         -- number of turns aggregated
ALTER TABLE episodes ADD COLUMN scope TEXT;                 -- 'group:<name>', 'dm:<a>:<b>', 'thread:<id>', 'tick' — prefix vocabulary matches RFC 0011 §A canonical addressing (`group | dm | thread`); a `channel:` prefix would silently miss every WHERE-clause join against channel addresses.
CREATE INDEX IF NOT EXISTS idx_episodes_scope ON episodes(scope, closed_at);
```

The asymmetry between `ADD COLUMN` (no `IF NOT EXISTS`) and `CREATE INDEX IF NOT EXISTS` is intentional. SQLite gained `ADD COLUMN IF NOT EXISTS` in 3.35 (March 2021); we don't yet require that minimum so the column DDL is non-idempotent. Migration safety is provided by the versioned migration runner in [agents/memory/migrations.py](../../agents/memory/migrations.py), which records applied migrations and skips re-runs. The `IF NOT EXISTS` on the index is belt-and-suspenders for any out-of-band re-execution; it stays because the index is the cheaper of the two to verify defensively.

Existing per-event rows (pre-RFC) have NULL in all four new columns. Recall queries treat NULL `interaction_id` as "single-turn legacy episode" and surface them at lower priority than multi-turn interactions of similar age (see §I).

**Lifecycle state is encoded by `(closed_at, summary)`, not by a separate column.** §C defines four states (`open`, `closing`, `closed`, `summarized`) but the migration deliberately omits a `state` TEXT column. The rule a reader (or a static analyzer) needs to know:

| Lifecycle state | Row predicate |
|-----------------|---------------|
| `open` | not persisted as a row — lives only in the in-memory `InteractionTracker` |
| `closing` | `closed_at IS NOT NULL AND (summary IS NULL OR summary = '')` |
| `closed` / `summarized` | `closed_at IS NOT NULL AND summary IS NOT NULL AND summary != ''` |

Recall queries that mean "only summarized episodes" therefore filter `WHERE summary IS NOT NULL AND summary != ''` (equivalent SQLite-friendly form: `WHERE COALESCE(summary, '') != ''`). The `closing`-state janitor (§C "Closing rows") is the only writer that mutates a row from `closing` to `closed`/`summarized`, by populating `summary` (either with the LLM-generated text or with the `"[interaction summary unavailable]"` fallback).

A separate `state` column was considered and rejected because it would (a) double-encode information already implicit in `closed_at`/`summary`, (b) require a `CHECK` constraint that has to stay in sync with §C's vocabulary across migrations, and (c) introduce a third failure mode where the column's value disagrees with the `summary` column. The implicit encoding is load-bearing — pinning it here in the storage model is what makes it safe.

Per-turn message text is **not** stored in `episodes`. Channel messages live in [RFC 0011's `messages` table](0011-channels-bridges.md). Human-chat messages are transient (RFC 0016) and only the resulting interaction summary persists. This keeps the episodic store from doubling as a message log.

### E. Memory Injection Contract

The persona-runtime memory-injection caller ([agents/persona_runtime/memory_context.py](../../agents/persona_runtime/memory_context.py)) is updated as follows:

- **Episodic recall** (`MemoryFacade.retrieve_relevant`) returns **only `closed` episodes** — concretely, the recall query filters `WHERE summary IS NOT NULL AND summary != ''` per the storage-model encoding pinned in §D. Open-interaction context is supplied separately by working memory (next bullet).
- **Working memory** carries the open-interaction turns up to the existing token budget. This is unchanged from today's behavior except that the buffer is now scoped to the current open interaction in the relevant scope, rather than an unstructured rolling window.
- **Memory budget** ([RFC 0017](0017-persona-memory-injection-budget.md)) is unchanged. The greedy-fill loop still admits items in priority order against `_MEMORY_BUDGET_TOKENS`. What changes is the input distribution: fewer, denser episodic summaries instead of many shallow ones.

The deferred-summary tradeoff is mitigated by working memory: while an interaction is `open`, the agent has full access to its turns through the live conversation buffer. Only after close does the interaction transition into recallable episodic memory. This means the cost of "deferred recall" is bounded to the gap between participants in *different* scopes (e.g., agent in `#planning` cannot recall the still-open `#design` discussion as an episodic summary, but can if it's also a participant in `#design` because that interaction's turns are in its working memory).

### F. Relationship Memory Updates

[RFC 0005's `record_interaction`](0005-persona-agent-memory.md#L835) is reinterpreted to fire **once per closed interaction**, not per message. The signature is preserved; the call site moves from the per-event handler into the interaction-close path:

- `interaction_count` increments by 1 per closed interaction (not per turn).
- Trust delta is computed from the interaction's outcome — a single value derived from the summary or from explicit outcome tags emitted during the interaction (e.g., `outcome=cooperative`, `outcome=defected`).
- `last_interaction_at` is set to `closed_at`, not the last message timestamp.

This is a breaking semantic change to relationship memory but the interface is identical, so callers in pre-existing code paths require no edits beyond moving the call site. Configuration that depends on `interaction_count` thresholds (e.g., trust-bootstrap rules) needs recalibration; this is called out in the Phase 2 deliverables.

### G. Per-Channel Scoping

The "scope" of an interaction — what set of turns belong to the same interaction — varies by channel type. For [RFC 0011](0011-channels-bridges.md)'s three canonical channel types:

| Channel type | Interaction scope | Boundary policy |
|--------------|-------------------|-----------------|
| `dm` | The two participants. One open interaction at a time per DM pair, per agent. | Idle-gap (default 10min). Structural close on either participant leaving. |
| `thread` | The thread itself. One open interaction per thread per agent. | Structural close on thread archive; idle-gap as fallback. Threads are the cleanest case — a thread *is* an interaction. |
| `group` | One rolling interaction per channel per agent. | Idle-gap only. Topic-shift would be the natural improvement here (a `#planning` channel may host several distinct conversations per day) — explicitly deferred. |

For non-channel events:

| Source | Interaction scope | Boundary policy |
|--------|-------------------|-----------------|
| Human chat (RFC 0016) | One open interaction per `(agent, human_participant)` pair. | Idle-gap; structural close on session end. |
| `TICK` events | Each tick is its own single-turn interaction (legacy behavior preserved). | No boundary detection; `closes` immediately on action emission. |
| Tool-only invocations (no inbound message) | Single-turn interaction. | Same as TICK. |

The unifying rule: the interaction scope is the smallest natural conversational unit for the source, and `tick`/single-turn events degenerate gracefully into single-turn interactions whose summary is the existing per-event summary text. **No event type loses its current behavior**; multi-turn behavior is added on top.

### H. Reflection Nudges and Counters

[RFC 0005's `auto_reflect_after`](0005-persona-agent-memory.md#L1050) nudge ("after N interactions, inject 'consider what's worth noting'") shifts to fire after N **closed interactions**, not N events. The counter persistence already exists in the `agent_state` table per RFC 0005 line 1058; the only change is what increments it.

This is a behavioral improvement: today, an agent in a busy channel hits the reflection threshold from raw message volume rather than from genuinely new contexts. After this RFC, the threshold tracks distinct conversational arcs.

### I. Backfill and Migration

**Existing rows.** Episodes already in production agent databases stay untouched. The four new columns are NULL for them. They are recallable via FTS5 and vector search (RFC 0008) at the same priority they have today.

**Recall priority for mixed legacy + new rows.** When a query returns both legacy single-turn rows and new interaction rows, the recall layer applies a small boost to interaction rows with `turn_count > 1` to reflect their higher information density. The boost is configurable (default: +10% on the relevance score). The +10% default is a placeholder pending dogfood data — see Open Question 7 for calibration.

**Boost fallback mechanism.** "Disabled if it produces empty results" is pinned to a concrete rule: if the boosted query returns fewer rows than `min(limit, baseline_count // 2)` — where `baseline_count` is the row count of the same query without the boost — the recall layer reissues the query without the boost and surfaces those rows instead. This avoids two failure shapes: (a) zero rows when the candidate set has no `turn_count > 1` rows (the boost makes them disappear under threshold), and (b) under-filling `limit` when only a tiny number of multi-turn rows clear the boosted threshold. The fallback path is exercised in the test suite alongside the boost path so both branches are kept honest.

**No bulk re-clustering.** Attempting to retroactively cluster old single-turn episodes into multi-turn interactions is explicitly out of scope. The cost (an LLM clustering pass over the entire history per agent) is high, the benefit decays with episode age, and the existing rows continue to function.

**Default importance policy.** [RFC 0011 §E](0011-channels-bridges.md#e-memory-integration) shows the close pipeline calling `self._compute_interaction_importance(interaction)` when writing the episode at interaction close. RFC 0011 calls into the close path that this RFC owns, so the default formula is pinned here rather than in RFC 0011:

```python
def _compute_interaction_importance(interaction) -> float:
    # Linear in turn count, capped — a 14-turn negotiation is materially more
    # important than a 2-turn exchange, but we don't want one mega-thread to
    # saturate the importance distribution.
    return min(1.0, 0.3 + 0.05 * interaction.turn_count)
```

A 1-turn interaction (TICK / tool-only) yields `importance = 0.35`, which is intentionally close to the existing `EpisodicMemory.store_episode` default of 0.5 from [RFC 0008 §C](0008-agent-memory-context-optimization.md) — single-turn rows must remain comparable to legacy per-event episodes that lacked any importance signal. A 14-turn closed interaction yields `importance = 1.0`, the cap. The choice of 0.3 baseline + 0.05 per turn is a placeholder pending dogfood data alongside the multi-turn recall boost (Open Question 7) — both knobs feed the same recall scorer. Outcome-tag inputs (e.g., `outcome=defected`) are *not* mixed into the importance signal in v0.3.0; outcome tags drive trust deltas in §F, which is a separate downstream signal. Mixing them would couple two recall behaviors that should be independently calibratable.

Phase 1 implementations may hard-code the constant `0.5` if interaction-aware close paths are not yet wired (single-turn legacy rows behave identically); Phase 3 (joint with RFC 0011 P3) is the milestone at which the formula above takes effect.

**Counter migration.** The `auto_reflect_after` counter is preserved as-is at the cutover. Once the new code path is live, increments come from closed interactions; pre-existing counter values are treated as legacy interaction counts. No reset.

---

## Security Considerations

- **Summary injection via interaction content.** An interaction's summary is generated by an LLM call over participant-supplied content. Adversarial messages can attempt to influence summary text, which then becomes a stored memory injected into future contexts. Mitigation: summary generation runs against the same input-sanitization pass as other LLM calls (RFC 0009 Phase 1), and the summary is stored verbatim — it is never executed or interpreted as instructions. Summary size is capped at a new default of 2000 chars — a deliberate 10× increase over the existing per-event cap (`_MAX_EPISODE_SUMMARY_CHARS = 200`, defined in [agents/persona_runtime/memory_context.py:76](../../agents/persona_runtime/memory_context.py#L76) and documented in RFC 0017 §C). The increase is justified because per-interaction summaries aggregate multiple turns into a single coherent narrative, where the per-event 200-char ceiling — sized for a single message-and-action pair — would clip a 10-turn negotiation summary mid-sentence and erase the outcome signal that makes the new granularity useful. The 2000-char value is introduced by this RFC, not inherited from RFC 0017; the constant lives in `memory_context.py` and may be promoted to a config knob if dogfood data shows different distributions per scope (e.g., DMs vs. group channels).
- **Resource amplification via long interactions.** A pathological case where an interaction stays `open` for hours accumulates turns in working memory. Mitigation: working memory's existing token bound (RFC 0017) caps in-memory growth regardless of interaction duration. A second cap (`max_interaction_turns`, default 200) forces a structural close to prevent indefinite open intervals.
- **Cross-agent interaction-id leakage.** Each agent generates its own `interaction_id` for its own view of an interaction; ids are not shared across agents and are not exposed in protobuf payloads. This means even logs or traces correlating "the same" interaction across agents must reconstruct from `(scope, started_at)` rather than relying on a canonical id — accepted as a non-goal of this RFC.
- **Summary failure as a covert channel.** A janitor that auto-fills `"[interaction summary unavailable]"` could in theory be triggered intentionally (e.g., by causing summary calls to time out) to scrub interaction content. The `closed_at` and `turn_count` columns remain populated even in failure, so the existence and shape of the interaction is non-erasable; only the natural-language summary is lost.

## Phased Implementation Plan

### Phase 1: Vocabulary, Tracker, and Schema

**Summary**: Add the in-memory `InteractionTracker`, extend the `episodes` schema, and wire single-turn ticks/tools through the new path so the existing behavior is unchanged but the lifecycle is observable.

**Deliverables**:

1. New module `agents/memory/interactions.py`: `InteractionTracker` class keyed by scope, with `start`, `add_turn`, `close`, and `idle_check` methods. No LLM calls in this phase — closing produces a placeholder summary.
2. Schema migration adding `interaction_id`, `started_at`, `closed_at`, `turn_count`, `scope` columns plus the `idx_episodes_scope` index.
3. `BoundaryDetector` interface with two implementations: `StructuralCloseDetector` and `IdleGapDetector`. `TopicShiftDetector` interface defined; default no-op implementation registered.
4. Single-turn event paths (TICK, tool-only) routed through the tracker so each emits one closed interaction with `turn_count=1`. Behavioral parity with current per-event episodes verified by integration test.
5. New telemetry counters: `interactions.opened`, `interactions.closed`, `interactions.closed.by_idle_gap`, `interactions.closed.by_structural`, `interactions.summary.failed`.

**Dependencies**: None. This phase is self-contained and ships even if RFC 0011 slips.

### Phase 2: Multi-Turn Interactions and Summarization

**Summary**: Wire multi-turn interaction support for human chat (RFC 0016) and DMs, including deferred summarization on close and the `closing`-state janitor.

**Deliverables**:

1. Multi-turn aggregation for human-chat sessions: turns accumulate in the open interaction; close on session end or idle.
2. Summarization-on-close LLM call. Summary generation uses the same model selection as RFC 0005's episode summarization (`optimization.yaml` → `context_management.summarization.model`).
3. `closing`-state janitor with `closing_grace_sec` enforcement and fallback summary text.
4. `record_interaction` call site moved from per-event into the close path. Trust-bootstrap thresholds that assumed per-message increments are recalibrated as part of this phase: a recalibration checklist is produced and lands in **this RFC's "Migration Notes" appendix** (added at Phase 2 PR-plan time), listing every config knob that was scaled against per-message `interaction_count` and its post-RFC equivalent. The earlier "or v0.3.0 release-prep doc" alternative is dropped — pinning a single owner avoids the deliverable splitting between two locations depending on doc state at PR time.
5. `auto_reflect_after` counter switched to increment on close.
6. Integration test: ten-turn human-chat session produces one episode, not ten, with a coherent summary.

**Dependencies**: Phase 1.

### Phase 3: Channel Integration (RFC 0011)

**Summary**: Wire the interaction model into RFC 0011's channel routing so DMs, threads, and groups produce one episode per interaction per agent.

**Deliverables**:

1. `CHANNEL_MESSAGE` event handler in persona runtime calls `InteractionTracker.add_turn` with the scope derived per §G.
2. Thread archive and channel-leave events trigger `StructuralCloseDetector`.
3. Per-channel `idle_timeout_sec` config field added to channel schema.
4. `MemoryFacade.retrieve_relevant` filters out non-`closed` rows (defense in depth — Phase 1 ensures only `closed` rows exist as completed summaries, but the filter makes this explicit).
5. Integration test: six-participant `#planning` channel with a 15-message exchange produces one episode per agent, each summarizing the agent's view of the exchange.

**Dependencies**: Phase 1, Phase 2, RFC 0011 Phase 2.

### Phase 4 (deferred, post-v0.3.0): Topic-Shift Detection

**Summary**: Replace the `TopicShiftDetector` no-op with a real implementation (embedding-similarity or LLM-judge), behind a config flag, off by default.

Not committed to v0.3.0 scope. Listed here only so Phase 1's interface choice is justified.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | `agents/memory/interactions.py` | (new) `InteractionTracker`, `BoundaryDetector` interface, `StructuralCloseDetector`, `IdleGapDetector`, `TopicShiftDetector` no-op |
| Python agents | `agents/memory/episodic.py` | Schema migration; `store_episode` accepts `interaction_id`, `started_at`, `closed_at`, `turn_count`, `scope`; recall priority boost for multi-turn rows |
| Python agents | `agents/memory/relationship_mutations.py` (`record_interaction` at line 166); façade `agents/memory/relationship.py` (`RelationshipMemory.record_interaction` at line 184) re-exports unchanged | Move call site of `record_interaction` from per-event handler to the interaction-close path; no signature change. The module is split: the function lives in `relationship_mutations.py`; callers using the `RelationshipMemory` façade keep using the façade. |
| Python agents | `agents/persona_runtime/__init__.py` | Wire dispatch loop to `InteractionTracker` for tick / human-chat / channel events. (`persona_runtime` is a package; the dispatch entry point is in `__init__.py`. Per-feature wiring may also touch `persona_runtime/action_loop.py` and `persona_runtime/memory_context.py`.) |
| Python agents | `agents/persona_runtime/memory_context.py` | Recall path filters to closed-only; working-memory buffer scoped to open interaction |
| Python agents | `agents/dispatch.py` | Emit interaction lifecycle events for telemetry; respect `END_INTERACTION` action if added |
| Config | `config/optimization.yaml` | `interaction.idle_timeout_sec`, `interaction.closing_grace_sec`, `interaction.max_turns` defaults |
| Schemas | `schemas/channel.schema.json` | Optional per-channel `idle_timeout_sec` field (Phase 3) |
| Tests | `tests/unit/`, `tests/integration/` | Tracker, boundary detection, lifecycle states, multi-turn aggregation, restart behavior |

No proto changes. No Go orchestrator changes (interaction lifecycle is agent-local). No CLI changes.

## Test Strategy

- **Unit tests**:
  - `InteractionTracker` lifecycle: open → close on idle, open → close on structural trigger, reopen-during-closing rejected.
  - `IdleGapDetector` timer arming/reset/firing.
  - Schema migration round-trip on a database with pre-existing per-event rows.
  - Recall priority boost computed correctly for mixed legacy + new rows.
- **Integration tests**:
  - Ten-turn human-chat session → one episode with coherent summary; relationship memory `interaction_count` increments by 1.
  - Six-participant channel with a 15-message exchange (Phase 3) → one episode per agent.
  - Restart with open interaction → next turn starts new interaction; pre-restart turns remain recoverable from channel store but are not auto-summarized.
  - `closing`-state janitor: synthetic summary failure leaves `closed_at` and `turn_count` populated, summary set to fallback string.
  - Single-turn TICK and tool-only paths produce `turn_count=1` episodes with behavior identical to pre-RFC.
- **Manual tests**:
  - End-to-end persona conversation in `persatrix chat` over a multi-turn dialogue, verifying episodic recall surfaces the *interaction* summary in a follow-up session, not per-message fragments.

## Open Questions

1. **Default `idle_timeout_sec` value.** Proposed 600s (10 min). Too short and a slow-reply human gets split across episodes; too long and the agent waits forever to "remember" what just happened. Calibrate against early dogfood usage.
2. **Group-channel scoping (single rolling interaction vs. per-sender-pair).** §G commits to one rolling interaction per group channel per agent for v0.3.0. The alternative — per-sender-pair sub-interactions — is more accurate but materially harder. Revisit if Phase 3 dogfood produces noisy summaries.
3. **Explicit `END_INTERACTION` action.** Should agents be able to deliberately close an interaction (e.g., a planner that says "we've decided X, closing")? Pro: cleaner semantics, agent-driven. Con: adds an action type and a new failure mode (agent forgets to close). Lean toward yes in Phase 2.
4. **Surfacing `interaction_id` in the agent's context.** Should the agent see "this is interaction X, your fifth turn"? Could enable richer in-prompt reasoning ("we've been at this for a while"). Could also be noise. Default off; revisit after Phase 2.
5. **Reopen window on race conditions.** §C commits to "do not reopen during `closing`". This is the simpler choice but may produce visible artifacts (a slow human gets split). If artifacts are common in practice, revisit with a bounded reopen window (e.g., reopen if a turn arrives within `closing_grace_sec / 4` of close).
6. **Outcome tagging for trust deltas.** §F mentions "outcome tags emitted during the interaction." The exact mechanism (action metadata? structured summary field? heuristic from summary text?) is not pinned in this RFC and will be specified in the Phase 2 PR plan.

7. **Calibration of the multi-turn recall boost.** §I sets a default +10% relevance-score boost on `turn_count > 1` rows. The number is a placeholder — too low and legacy single-turn rows still dominate recall; too high and a single mediocre multi-turn summary outranks a highly-relevant legacy row. The boost is also expressed as a percentage of the score; a fixed addend (e.g., +0.05) might compose better with the underlying scorer (whose distribution we have not characterized). Calibrate against early dogfood: pick a value once we have a corpus of mixed legacy + interaction rows and can measure recall@k for both forms.

8. **Stability of `scope` identifiers across participant renames.** §G defines scope as `dm:<a>:<b>`, `group:<name>`, etc. — free-form strings, not foreign keys. Renaming a participant or a channel orphans every prior interaction's scope from the renamed entity. v0.3.0 accepts this as a known limitation (rename is a config-only operation today and is exceptional). If renaming becomes a routine operation, the cleanest fix is a side table mapping historical names to current identities, applied at recall time. RFC 0021's `target_party` (free-form participant ID on commitments) has the same property — see RFC 0021 §F. Out of scope for v0.3.0.

## Decision / Next Steps

This RFC is a prerequisite for the v0.3.0 user-facing promise to land cleanly. Without it, channels (RFC 0011) ship with the per-message episode model and produce the shredded-memory failure mode described in Motivation.

**Required before implementation begins**:

1. Resolve Open Questions 1, 3, and 6 in a PR-plan companion document.
2. Confirm with RFC 0008 author (same author) that §D's Context Packaging pipeline composes with interaction-bounded inputs without further changes — the current expectation is yes, and this is recorded as a no-op for RFC 0008.
3. Add this RFC to ROADMAP.md's RFC Master Index and the v0.3.0 dependency chain (RFC 0020 sits before RFC 0011 Phase 3, parallel with RFC 0008 Phase 1).

**Phasing into v0.3.0**: Phase 1 ships independently of channels and is the lowest-risk piece. Phase 2 lands before any RFC 0011 work that touches memory. Phase 3 is RFC 0011 Phase 3's prerequisite and lands jointly.

## Related Documentation

- [RFC 0005](0005-persona-agent-memory.md) — Episode model, relationship memory, `record_interaction` (this RFC reinterprets the granularity of both).
- [RFC 0008](0008-agent-memory-context-optimization.md) — Context Packaging and Compression Pipeline (§D consumes interaction-bounded inputs).
- [RFC 0011](0011-channels-bridges.md) — Channels and internal agent messaging (Phase 3 of this RFC depends on RFC 0011 Phase 2).
- [RFC 0016](0016-human-participant-chat-interface.md) — Human chat (multi-turn aggregation in Phase 2).
- [RFC 0017](0017-persona-memory-injection-budget.md) — `MemoryBudget` allocator (composes unchanged).
- [RFC 0019](0019-opentelemetry-completion.md) — Telemetry pipeline (interaction lifecycle counters).

## Migration Notes (PR 4)

PR 4 changes when `RelationshipMemory.record_interaction` fires and what
`interaction_count` measures. Operators tuning trust-bootstrap or
auto-reflect thresholds calibrated against the pre-PR-4 behavior should
re-tune.

**Before PR 4 (RFC 0005 §F semantics, shipped through PR 3):**

- `record_interaction` ran once **per inbound chat message** in
  `SendChatMessage` (`agents/server_servicers.py`).
- `relationships.interaction_count` therefore counted **messages**, not
  conversations.
- The auto-reflect counter (`config.memory.notes.auto_reflect_after`)
  also incremented **per message**.

**After PR 4:**

- `record_interaction` fires **once per closed interaction**, from
  `_StatePersistenceMixin._persist_closed_interaction`. A 10-turn DM
  produces one bump, not ten.
- `relationships.interaction_count` now counts **conversations**.
- The auto-reflect counter increments **once per closed interaction**.
- The `outcome` field carries the (truncated) episode summary, or
  `NULL` when the LLM summariser failed and we fell back to
  `[interaction summary unavailable]`.

**Recalibration guide:**

| Knob | Old units (per message) | New units (per closed interaction) | Suggested rescale |
|------|-------------------------|------------------------------------|-------------------|
| `config.memory.notes.auto_reflect_after` | messages | interactions | divide by your typical turns/conversation (e.g., 50 → 5–10) |
| RFC 0005 trust-bootstrap thresholds (see RFC 0005 §F lines 889–920) | message count | interaction count | divide by typical turns/conversation |
| Any external dashboard derived from `relationships.interaction_count` | messages | interactions | re-baseline; do not chart across the migration |

**Calibration window (RFC 0008 PR 6):** the production calibration
window for memory-context tuning closes 2026-05-29. Calibration data
collected before PR 4 lands reflects per-message semantics; reset the
window after PR 4 deploys before drawing conclusions about
`interaction_count`-based heuristics.

**Operational notes:**

- A summariser failure (LLM timeout / error / empty response) writes
  `[interaction summary unavailable]` to the episode `summary` and
  emits the `agent.interactions.summary.failed` counter with
  `{reason: "timeout"|"llm_error"|"empty"}`.
- A persisted-but-unsummarised interaction (`closing` state ended
  before the summariser ran) leaves `summary = "[summary pending]"`;
  the periodic janitor (`cleanup_closing_interactions`, default grace
  300s) backfills these to the unavailable sentinel and emits the same
  counter with `reason: "janitor"`.
- No data migration is required for existing rows — pre-PR-4 episodes
  retain their per-message semantics and remain searchable.
