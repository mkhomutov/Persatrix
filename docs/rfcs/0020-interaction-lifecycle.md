# RFC 0020 — Interaction Lifecycle: Dialogue Boundaries and Episode Granularity

**Type**: architecture
**Status**: 📋 Proposed
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
                          ▲                        │
                          │ next turn arrives      │ (summarization in flight)
                          └────── reopens ─────────┘
                          (only valid pre-summary;
                           see "reopen window" below)
```

| State | Meaning | Storage |
|-------|---------|---------|
| `open` | Active interaction; turns are accumulating in working memory. Idle timer is armed. | In-memory `InteractionTracker` per agent, keyed by scope (§G). Not persisted as `open` rows. |
| `closing` | Boundary tripped; summary call is enqueued or in flight. Working-memory turns are frozen for this interaction. | A `closing` row is written to `episodes` with `closed_at` set but `summary` empty, so a restart can reconcile. |
| `closed` / `summarized` | Summary persisted; row is now eligible for episodic recall. | Final `episodes` row with non-empty `summary`. |

**Reopen window.** If a new turn arrives in the same scope while the interaction is `closing` (e.g., during the seconds between the timer firing and the summary call returning), the reopen rule is: **do not reopen**. The just-closing interaction summarizes as-is, and the new turn starts a fresh interaction. This sacrifices some semantic fidelity (a participant who was just slow to reply gets split across two episodes) for a much simpler concurrency model — no race between summary completion and turn arrival, no rollback paths.

**Restart behavior.** On orchestrator restart with an `open` interaction in memory: the in-memory tracker is lost, the stored channel/conversation history is intact, and the *next* arriving turn starts a new interaction. Pre-restart turns are recoverable from message storage but are not auto-summarized as a closed interaction. This is acceptable because (a) restarts are expected to be rare relative to interaction frequency, and (b) the alternative — durable open-interaction state — adds a transactional contract that's not worth its complexity for v0.3.0.

**Closing rows.** A row that's been in `closing` state for more than `closing_grace_sec` (default 300s) without a successful summary is treated as failed. A janitor (Phase 2) writes a fallback summary `"[interaction summary unavailable]"` and transitions the row to `closed` so it doesn't block recall. Failed-summary count is exported as a metric.

### D. Storage Model

The `episodes` table from [RFC 0005 §`episodes`](0005-persona-agent-memory.md#L736) is extended additively. No existing column is changed in semantics; four new columns are added:

```sql
ALTER TABLE episodes ADD COLUMN interaction_id TEXT;        -- ULID, unique per interaction
ALTER TABLE episodes ADD COLUMN started_at REAL;            -- first turn timestamp
ALTER TABLE episodes ADD COLUMN closed_at REAL;             -- boundary-trip timestamp
ALTER TABLE episodes ADD COLUMN turn_count INTEGER;         -- number of turns aggregated
ALTER TABLE episodes ADD COLUMN scope TEXT;                 -- 'channel:<name>', 'dm:<a>:<b>', 'thread:<id>', 'tick'
CREATE INDEX IF NOT EXISTS idx_episodes_scope ON episodes(scope, closed_at);
```

Existing per-event rows (pre-RFC) have NULL in all four new columns. Recall queries treat NULL `interaction_id` as "single-turn legacy episode" and surface them at lower priority than multi-turn interactions of similar age (see §I).

Per-turn message text is **not** stored in `episodes`. Channel messages live in [RFC 0011's `messages` table](0011-channels-bridges.md). Human-chat messages are transient (RFC 0016) and only the resulting interaction summary persists. This keeps the episodic store from doubling as a message log.

### E. Memory Injection Contract

The persona-runtime memory-injection caller ([agents/persona_runtime/memory_context.py](../../agents/persona_runtime/memory_context.py)) is updated as follows:

- **Episodic recall** (`MemoryFacade.retrieve_relevant`) returns **only `closed` episodes**. Open-interaction context is supplied separately by working memory (next bullet).
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

**Recall priority for mixed legacy + new rows.** When a query returns both legacy single-turn rows and new interaction rows, the recall layer applies a small boost to interaction rows with `turn_count > 1` to reflect their higher information density. The boost is configurable (default: +10% on the relevance score) and disabled if it produces empty results. The +10% default is a placeholder pending dogfood data — see Open Question 7 for calibration.

**No bulk re-clustering.** Attempting to retroactively cluster old single-turn episodes into multi-turn interactions is explicitly out of scope. The cost (an LLM clustering pass over the entire history per agent) is high, the benefit decays with episode age, and the existing rows continue to function.

**Counter migration.** The `auto_reflect_after` counter is preserved as-is at the cutover. Once the new code path is live, increments come from closed interactions; pre-existing counter values are treated as legacy interaction counts. No reset.

---

## Security Considerations

- **Summary injection via interaction content.** An interaction's summary is generated by an LLM call over participant-supplied content. Adversarial messages can attempt to influence summary text, which then becomes a stored memory injected into future contexts. Mitigation: summary generation runs against the same input-sanitization pass as other LLM calls (RFC 0009 Phase 1), and the summary is stored verbatim — it is never executed or interpreted as instructions. Summary size is capped (default 2000 chars; extends `_MAX_EPISODE_SUMMARY_CHARS` from RFC 0017).
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
4. `record_interaction` call site moved from per-event into the close path. Trust-bootstrap thresholds that assumed per-message increments are recalibrated as part of this phase: a recalibration checklist is produced (target location: the v0.3.0 release-prep doc once it exists, or this RFC's own "Migration Notes" appendix as a fallback) listing every config knob that was scaled against per-message `interaction_count` and its post-RFC equivalent.
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

8. **Stability of `scope` identifiers across participant renames.** §G defines scope as `dm:<a>:<b>`, `channel:<name>`, etc. — free-form strings, not foreign keys. Renaming a participant or a channel orphans every prior interaction's scope from the renamed entity. v0.3.0 accepts this as a known limitation (rename is a config-only operation today and is exceptional). If renaming becomes a routine operation, the cleanest fix is a side table mapping historical names to current identities, applied at recall time. RFC 0021's `target_party` (free-form participant ID on commitments) has the same property — see RFC 0021 §F. Out of scope for v0.3.0.

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
