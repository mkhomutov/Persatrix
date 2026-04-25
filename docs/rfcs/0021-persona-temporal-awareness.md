# RFC 0021 — Persona Temporal Awareness

**Type**: architecture
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-04-25
**Target**: v0.3.0 (Phase 1) + v0.4.0 (Phases 2–4)
**Depends on**: RFC 0005 (persona memory substrate); RFC 0017 (memory budget — composes, no API change); RFC 0020 (interaction lifecycle — Phase 1 consumes its `started_at` / `closed_at` columns)
**Consumed by**: RFC 0008 (§D Context Packaging — adds temporal-prefix rendering to recall output); RFC 0011 (channel recall inherits temporal annotation in v0.3.0)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Vocabulary](#a-vocabulary)
  - [B. The Clock Abstraction](#b-the-clock-abstraction)
  - [C. Now-Anchor in the System Prompt](#c-now-anchor-in-the-system-prompt)
  - [D. Relative-Time Rendering on Recall](#d-relative-time-rendering-on-recall)
  - [E. Relationship Temporality](#e-relationship-temporality)
  - [F. Commitments — Forward-Looking Memory](#f-commitments--forward-looking-memory)
  - [G. Time Tool Surface](#g-time-tool-surface)
  - [H. REMINDER Event and Tick-Loop Coupling](#h-reminder-event-and-tick-loop-coupling)
  - [I. Duration Calibration and Estimation](#i-duration-calibration-and-estimation)
  - [J. Token-Budget Integration](#j-token-budget-integration)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Persona agents today have no sense of time. The system prompt assembled in [agents/persona_runtime/prompt_assembly.py:40-116](../../agents/persona_runtime/prompt_assembly.py#L40-L116) carries identity, behavior, goals, and dynamic state but no temporal anchor — the LLM does not know what year it is, when the last interaction happened, or whether a memory is hours or months old. Memory recall surfaces episode summaries with no recency rendering. There is no way for a persona to commit to a future obligation, to be reminded of one, or to learn how long things take.

This RFC introduces a temporal layer across four cooperating capabilities: a **now-anchor** (current time injected into every prompt), **recency rendering** (relative-time prefixes on recalled episodes), **commitments** (a new forward-looking memory class with due dates), and **duration calibration** (learned priors from past task and interaction durations). Phase 1 lands in v0.3.0 as cheap scaffolding on top of RFC 0020's interaction timestamps. Phases 2–4 land in v0.4.0 as a coherent forward-memory + estimation surface that personas can act on.

## Motivation

Persatrix's v0.3.0 user-facing promise — *"give agents a shared channel and watch them talk, negotiate, and form opinions over time"* — has the words "over time" doing a lot of work that the system does not actually deliver. Concretely:

1. **Personas cannot tell now from then.** The LLM has no current-time context. When asked "have we talked recently?" the model either hallucinates a date from training data or refuses to engage. When recalling an episode, the agent cannot tell whether it was yesterday or last quarter — both are rendered as ungrounded summary text. This is the cheapest possible failure to fix and the most pervasive.

2. **Recall is temporally flat.** [agents/persona_runtime/memory_context.py](../../agents/persona_runtime/memory_context.py) packages episodes into the prompt with summary text but no recency rendering. RFC 0020 will add `started_at` / `closed_at` to every episode ([RFC 0020 §D](0020-interaction-lifecycle.md#d-storage-model)), but those timestamps go nowhere visible to the LLM. The information density gain from "we negotiated X with Bob" → "3 days ago, over 47 minutes, we negotiated X with Bob" is large for a few extra tokens.

3. **Relationships look static.** [agents/memory/relationship_types.py:44-45](../../agents/memory/relationship_types.py#L44-L45) already stores `interaction_count` and `last_interaction_at`, but neither reaches the prompt. An agent with strong trust toward Alice has no way to express "Alice and I haven't spoken in three weeks" — a real social signal that should shape behavior.

4. **There is no forward memory.** [agents/memory/notes.py](../../agents/memory/notes.py) provides retrospective storage (topic + content + tags). There is no slot for *"I promised Alice I would review the proposal by Friday"* with a `due_at`. Without forward memory, personas cannot make plans they remember, cannot be reminded, and cannot demonstrate followthrough — the texture that makes a relationship believable.

5. **Estimation is impossible without history.** A persona that has resolved twenty similar tasks should be able to say "this one usually takes me about two hours." Today the agent has no calibration store and no surfaced priors, so estimation collapses to LLM intuition.

The user-facing failure mode is subtle but corrosive: agents *behave plausibly within a single turn* and *appear amnesic across time*. A persona that says "let's circle back next week" and then has no mechanism to actually circle back is worse than one that never made the offer.

## Goals

1. **Establish a now-anchor** in every persona prompt: current absolute time (ISO-8601 with timezone), day of week, and a coarse human-readable form ("Saturday afternoon"). Computed in Python — the LLM never does date arithmetic.
2. **Render recency on recall** as a pre-computed relative prefix on every recalled episode summary, drawn from the episode's `started_at` / `closed_at` ([RFC 0020 §D](0020-interaction-lifecycle.md#d-storage-model)) plus the current-time anchor.
3. **Surface relationship temporality** as part of the relationship summary: `last_interaction_at` rendered relative to now ("you last spoke with Alice 3 weeks ago"), included alongside trust score and interaction count when the relationship is referenced in context.
4. **Introduce a Commitment memory class** — a structured forward-looking memory with `due_at`, `target_party`, `description`, and lifecycle states (`open` → `fulfilled` | `missed` | `cancelled`). Store, query, fulfill, and cancel via tools.
5. **Add a time-tool surface** — `get_current_time`, `time_since`, `time_until`, `set_reminder` — gated by a new `time:read` permission (no write capability — these tools observe, they do not mutate clocks).
6. **Couple commitments to the tick loop** via a new `REMINDER` event type that fires when a commitment's `due_at` enters a configurable proximity window.
7. **Build a duration calibration store** that records actual elapsed time for completed tasks and closed interactions, exposed as a `recall_typical_duration(category)` query.
8. **Make all of the above test-deterministic** by routing every wall-clock read through a `Clock` abstraction with a frozen-clock test implementation.
9. **Compose with the existing memory budget** ([RFC 0017](0017-persona-memory-injection-budget.md)) without API change — temporal tags are a low-cost prefix on existing budget items, not new budget categories.

## Non-Goals

- **Cross-agent shared time** beyond the wall clock each agent observes locally. NTP, monotonic guarantees across orchestrator and agents, or replay-safe causal time are deferred — this RFC assumes participants agree on wall-clock time within a few seconds.
- **Per-relationship time-granularity policy** ("I tell trusted friends the exact minute, strangers only get 'recently'"). Conceptually appealing but only matters once external bridges (RFC 0011 v0.5.0) introduce cross-trust-boundary surfaces. Deferred to v0.5.0.
- **Calendar / scheduling / iCal integration.** Commitments are an internal memory primitive, not a calendaring system. External calendar sync is RFC 0011-tier bridge work, not this.
- **Agent-to-agent shared commitments.** When Agent A promises Agent B something, both sides record their own view in their own commitment store. A canonical shared-commitment ledger across agents is a v0.4.0+ concern adjacent to RFC 0012 (organizations).
- **Real-time topic-shift / context-switch detection.** Aligned with RFC 0020's deferral — temporal awareness does not include "the user just changed topic."
- **Replaying historical episodes to backfill duration data.** Pre-RFC episodes lack the columns we need; we calibrate forward only, not retroactively.
- **Wall-clock skew detection.** If the host clock jumps, the agent will see it. We do not detect or compensate. Containers running NTP are assumed.

---

## Design / Implementation

### A. Vocabulary

| Term | Definition | Storage |
|------|------------|---------|
| **Now** | The current wall-clock time as observed by the agent's `Clock`. Not a stored value. | None — ephemeral. |
| **Now-anchor** | The block injected into the system prompt that names the current time. Carries absolute time, weekday, and a coarse human form. | Prompt-only. |
| **Recency tag** | A pre-computed string ("3 days ago", "47 min ago") rendered alongside a recalled episode or relationship fact. | Prompt-only; never stored. |
| **Commitment** | A first-class forward-looking memory: a structured intention with a `due_at` and a lifecycle. Distinct from a Note (retrospective, unstructured). | New `commitments` table. |
| **Reminder** | A tick-loop event that surfaces a near-due commitment into the agent's next reasoning step. | Transient — fires from the tracker, not stored as such. |
| **Duration record** | A `(category, duration_seconds, recorded_at)` row used to compute typical-duration priors. | New `duration_records` table. |

### B. The Clock Abstraction

Every wall-clock read in the temporal layer routes through a single `Clock` protocol:

```python
# agents/clock.py (new)
from typing import Protocol

class Clock(Protocol):
    def now(self) -> float:
        """Seconds since epoch, UTC."""
        ...

    def now_iso(self) -> str:
        """ISO-8601 representation of now() in the agent's configured timezone."""
        ...

class WallClock:
    """Default implementation. Reads time.time()."""

class FrozenClock:
    """Test implementation. Returns a pinned timestamp; advanceable via .advance(seconds)."""
```

The persona runtime resolves a `Clock` once at construction and threads it through prompt assembly, memory packaging, commitment tracking, and the tick loop. **No call to `time.time()` is made directly inside the temporal layer** — every read goes through `Clock.now()`. This is non-negotiable: without it, every test that asserts "rendered as '3 days ago'" becomes flaky against the wall clock.

The agent's timezone is read from `persona.timezone` (new optional config field, default `UTC`). Mismatched timezones across agents are not a coordination concern at this layer; each agent renders relative to its own configured zone.

Existing direct uses of `time.time()` outside the temporal layer (e.g., [agents/memory/episodic.py:203](../../agents/memory/episodic.py#L203)) are not refactored as part of this RFC. They produce raw timestamps that the temporal layer interprets — the rendering responsibility moves up, the storage responsibility stays where it is.

### C. Now-Anchor in the System Prompt

`_build_system_prompt` in [agents/persona_runtime/prompt_assembly.py:40](../../agents/persona_runtime/prompt_assembly.py#L40) gains a temporal block, inserted between the dynamic-state block and the user-message-boundary instruction:

```
Current time: 2026-04-25T14:32:00+00:00 (Saturday afternoon, UTC).
```

The format is deliberately three pieces of information packed into one line:

1. **ISO-8601 absolute** — unambiguous, machine-readable, the LLM can quote it back accurately.
2. **Day-of-week + coarse part-of-day** — what an English speaker actually uses ("Saturday afternoon"). Reduces the chance the LLM ignores the ISO timestamp because it looks like noise.
3. **Timezone abbreviation** — disambiguates absolute time without forcing the LLM to do offset arithmetic.

Token cost: roughly 25–35 tokens per prompt. Acceptable across the board; not metered as a budget category. The line is unconditionally appended (no behavioral toggle) — there is no scenario in which a persona is better off not knowing the current time.

The coarse part-of-day mapping is fixed in code (a deterministic function of hour-of-day in the configured timezone): `00–05 = "early morning"`, `05–08 = "morning"`, `08–12 = "late morning"`, `12–17 = "afternoon"`, `17–21 = "evening"`, `21–24 = "night"`. Exact text is bikeshed-fodder; we commit to a stable mapping that tests can pin.

### D. Relative-Time Rendering on Recall

When [agents/persona_runtime/memory_context.py](../../agents/persona_runtime/memory_context.py) packages episodes for prompt injection, each episode's summary is prefixed with a relative-time tag computed against the now-anchor:

```
[3 days ago, over 47 min, with Bob] We negotiated the API contract; Bob conceded on…
```

The prefix carries three signals when available:

- **Recency** — `format_relative(closed_at, now)` returning one of the bucketed forms below.
- **Duration** — if `turn_count > 1` and `started_at` is non-null, `format_duration(closed_at - started_at)` rendered as "over 47 min", "over 2 hours", etc. Single-turn legacy episodes (RFC 0020 §I migration: `interaction_id IS NULL`) skip this.
- **Counterparty** — the participant most active in the episode if known from existing summary metadata. Falls back to scope ("in #planning") if no clear counterparty.

The bucketed recency forms — chosen to be cheap and unambiguous — are:

| Age | Rendered as |
|-----|-------------|
| < 60 s | "just now" |
| 1–59 min | "N min ago" |
| 1–23 h | "N hours ago" or "today, HH:MM" if same calendar day |
| 1 day | "yesterday" |
| 2–6 days | "N days ago" or "last <weekday>" if within 7 days |
| 7–13 days | "last week" |
| 14–60 days | "N weeks ago" |
| 61 days – 12 months | "N months ago" |
| > 12 months | "over a year ago" or "in <year>" |

The mapping is implemented in `agents/temporal/rendering.py` as pure functions taking `(timestamp_then, timestamp_now, timezone) → str`. Pure and deterministic, so unit-testable with frozen clocks and no I/O.

**Why pre-compute, not let the LLM compute.** LLMs are demonstrably bad at date arithmetic — they can quote a date back accurately but routinely err when subtracting. Asking the model "the episode is at 1714056720 epoch seconds, what's that relative to now?" is a worse engineering choice than computing the answer in Python and handing it to the model as text. This is a hard rule for the entire temporal layer.

**Why prefix, not separate field.** A prefix renders inside the existing episode-summary token budget (RFC 0017) without adding a new budget category. The cost is roughly 5–10 tokens per recalled episode. With a typical recall of 3–5 episodes, the total per-prompt cost is 15–50 tokens — comfortably below noise relative to the summary content itself.

### E. Relationship Temporality

The relationship summary path used by prompt assembly when a particular peer is referenced — currently producing strings like `"trust=0.72, interaction_count=14"` — is extended to include:

- `last_interaction_at` rendered as a recency tag: `"last seen 3 weeks ago"`.
- A coarse cadence bucket if `interaction_count > 5`: `"frequent"` (more than once per week on average over the relationship lifetime), `"regular"` (once per week to once per month), `"sparse"` (less than once per month). Pure function of `(interaction_count, first_interaction_at, last_interaction_at, now)`.

No schema change — both fields already exist in [agents/memory/relationship_types.py:44-45](../../agents/memory/relationship_types.py#L44-L45). What changes is the rendering call site in `memory_context.py`.

This is the smallest piece of the RFC and the highest-leverage social signal. Personas that can say "I haven't talked to Alice in a month" feel materially more believable than ones that cannot.

### F. Commitments — Forward-Looking Memory

A commitment is a structured intention to do something in the future. It is fundamentally distinct from a note (retrospective, unstructured) and from a goal (long-running, no due date). The data shape:

```python
@dataclass
class Commitment:
    id: str              # ULID
    description: str     # natural-language: "review Alice's proposal"
    due_at: float        # epoch seconds; the scheduled time
    target_party: str | None  # peer agent or user this commitment is to (e.g., "alice")
    created_at: float
    state: Literal["open", "fulfilled", "missed", "cancelled"]
    fulfilled_at: float | None
    source_interaction_id: str | None  # RFC 0020 link, if commitment was made during a known interaction
    tags: list[str]
```

Storage is a new SQLite table in the agent's local DB:

```sql
CREATE TABLE commitments (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    due_at REAL NOT NULL,
    target_party TEXT,
    created_at REAL NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'fulfilled', 'missed', 'cancelled')),
    fulfilled_at REAL,
    source_interaction_id TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX idx_commitments_due_state ON commitments(state, due_at);
CREATE INDEX idx_commitments_target ON commitments(target_party, state);
```

**Why a separate table, not Notes.** Notes are unstructured topic-content pairs with no due date and no lifecycle. Commitments need indexed queries on `due_at` (for the tick-loop scan), structured `target_party` (so "what did I promise Alice?" returns deterministically), and a state machine. Trying to store commitments as notes would mean parsing free-text descriptions to find due dates — exactly the failure pattern this RFC exists to fix.

**Why per-agent (not centralized in the orchestrator).** Each agent owns its own view. When Agent A promises Agent B, A records "I promised B" in A's store; B may independently record "A promised me" in B's store. A canonical ledger across agents is a v0.4.0+ concern (RFC 0012 organizational layer). Per-agent gives us the right behavior with no new cross-agent contracts.

**Lifecycle transitions.**

```
   created via store_commitment        marked via fulfill_commitment
   ──────────────────────────►  open  ───────────────────────────►  fulfilled
                                  │
                                  │ due_at < now and not fulfilled
                                  ▼
                                missed (set by janitor; see §H)
                                  │
                                  │ explicit cancel
   open ─────cancel_commitment─► cancelled
```

Once a commitment is `fulfilled`, `missed`, or `cancelled`, it is read-only. Missed commitments remain in the store as material for episodic recall ("you missed your last commitment to Alice three days ago") — they are not deleted.

**Recall surface.** `MemoryFacade` gains `list_open_commitments(within_seconds=None)` and `list_commitments_to(target_party)`. The persona-runtime memory packaging path surfaces upcoming open commitments (those with `due_at` within a configurable horizon, default 7 days) into the prompt context as a dedicated block:

```
Open commitments:
- in 2 hours: review Alice's proposal (to alice)
- tomorrow afternoon: send Bob the design notes (to bob)
- next Tuesday: prepare quarterly summary
```

This block is budgeted under RFC 0017's allocator with a fixed cap (default 300 tokens; configurable via `optimization.yaml` → `temporal.commitments_prompt_budget_tokens`). Past-due missed commitments are *not* surfaced in this block automatically — they live in episodic recall instead, where age and relevance are weighted normally.

### G. Time Tool Surface

Four tools, all gated by a new `time:read` permission:

| Tool | Signature | Purpose |
|------|-----------|---------|
| `get_current_time` | `() -> {iso: str, epoch: float, weekday: str, part_of_day: str}` | Return now in machine + human form. Useful when the persona needs to reason about precise time without re-reading the prompt anchor. |
| `time_since` | `(timestamp: float \| str) -> {seconds: int, rendered: str}` | Compute elapsed time from a stored timestamp. Accepts ISO-8601 or epoch seconds. Returns both raw seconds and a bucketed string ("3 days ago"). |
| `time_until` | `(timestamp: float \| str) -> {seconds: int, rendered: str}` | Mirror of `time_since` for future timestamps. Returns negative seconds for past targets, with `rendered` set to "passed N <unit> ago". |
| `set_reminder` | `(description: str, due_at: float \| str, target_party: str = "", tags: str = "") -> {commitment_id: str}` | Create a commitment. Thin wrapper over the commitments store; gated by `commitments:write` rather than `time:read`. Listed here because LLMs tend to look for it in the time-tool family. |

Companion commitment tools (separate permission family `commitments:read` / `commitments:write`):

| Tool | Signature | Purpose |
|------|-----------|---------|
| `list_commitments` | `(state: str = "open", within_seconds: int = 0, limit: int = 20) -> [Commitment]` | Query commitments. `within_seconds = 0` returns all open; nonzero filters to those due within the window. |
| `fulfill_commitment` | `(commitment_id: str, note: str = "") -> {ok: bool}` | Mark fulfilled. Optional note becomes a Note linking back to the commitment. |
| `cancel_commitment` | `(commitment_id: str, reason: str = "") -> {ok: bool}` | Cancel. Reason captured in tags. |

Permission defaults: `time:read` is granted by default (it's pure observation). `commitments:read` and `commitments:write` are granted by default to persona agents (consistent with `memory:read` / `memory:write` defaults in [agents/tools/builtin.py](../../agents/tools/builtin.py)) and revocable per-agent in `agents.yaml`.

The system prompt gains a one-line nudge analogous to the existing memory-tool nudge ([prompt_assembly.py:98-114](../../agents/persona_runtime/prompt_assembly.py#L98-L114)):

> When you make a commitment to do something at a future time, you MUST call `set_reminder` — do not just say you will remember. When asked what you have on your schedule, call `list_commitments`.

Without this nudge, the LLM tends to acknowledge commitments verbally and never persist them — exactly the failure pattern that motivates this RFC.

### H. REMINDER Event and Tick-Loop Coupling

A new event type joins [agents/persona_types.py:32-41](../../agents/persona_types.py#L32-L41):

```python
class EventType(Enum):
    ...
    REMINDER = "reminder"
```

The autonomous tick loop ([agents/persona_runtime](../../agents/persona_runtime/)) gains a pre-tick scan: before the agent reasons on a `TICK` event, the runtime queries `list_open_commitments(within_seconds=reminder_horizon_sec)` (default 3600s, configurable). If any are returned, the runtime emits a `REMINDER` event ahead of the `TICK`, with payload:

```python
{
    "commitments": [
        {"id": "...", "description": "...", "due_at": 1714060000.0,
         "target_party": "alice", "rendered_due": "in 47 min"}
    ]
}
```

The persona's event handler treats `REMINDER` like any other inbound event — it can choose to act (send a message, complete the task, request a delay) or `DO_NOTHING`. The reminder is *informational*; it does not auto-fulfill the commitment. Fulfillment requires an explicit `fulfill_commitment` tool call.

**Janitor for missed commitments.** A periodic sweep (running on the same cadence as the tick loop, but cheap — a single indexed query) transitions any `open` commitment with `due_at < now - missed_grace_sec` (default 1 hour) to `missed`. Missed commitments are *not* automatically surfaced via `REMINDER`; they enter episodic recall through the standard relationship/memory paths.

**Why a separate event type, not inline injection.** Treating reminders as events keeps the persona's reasoning loop uniform — every input is an `AgentEvent`. The alternative (silently injecting "you have these commitments" into the next event's payload) couples the temporal layer to every event handler in dispatch and is harder to test.

**Restart behavior.** Reminders are stateless: on orchestrator restart, the next tick scans the commitments table fresh and re-emits `REMINDER` events for anything still in the proximity window. We do not track "which reminders have already fired" — duplicates are accepted as the cost of statelessness.

### I. Duration Calibration and Estimation

A persona that has resolved twenty similar tasks should be able to draw on history when estimating the next one. Two storage primitives:

```sql
CREATE TABLE duration_records (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,           -- free-form: "code_review", "design_doc", "negotiation"
    duration_seconds REAL NOT NULL,
    recorded_at REAL NOT NULL,
    source TEXT NOT NULL,             -- 'task' | 'interaction' | 'manual'
    source_id TEXT,                   -- task id or interaction_id
    tags_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX idx_duration_category ON duration_records(category, recorded_at);
```

**Population.** Two sources contribute records automatically:

1. **Task completion** — when [agents/persona_runtime/__init__.py:367](../../agents/persona_runtime/__init__.py#L367) emits a `COMPLETE_TASK` action, the runtime records `(category=task.category, duration=now - task.started_at, source='task', source_id=task.id)`.
2. **Interaction close** (RFC 0020) — on `closed` transition, the runtime records `(category='interaction:<scope_kind>', duration=closed_at-started_at, source='interaction', source_id=interaction_id)` for multi-turn interactions only (`turn_count > 1`).

A third source is the explicit tool `record_duration(category, seconds)` (manual; gated by `duration:write`) — useful for personas that want to log a duration they observed externally.

**Query surface.** One tool, `recall_typical_duration`:

```python
recall_typical_duration(category: str, sample_size: int = 10) -> {
    "median_seconds": int,
    "p25_seconds": int,
    "p75_seconds": int,
    "n_samples": int,
    "rendered": str,    # "usually 30 min – 2 hours, median 47 min (n=14)"
}
```

Quartiles + median are robust to outliers; mean-and-stdev would be misleading on heavy-tailed task durations. If `n_samples < 3` the tool returns `{"n_samples": 0, "rendered": "no calibration data yet"}` — we do not pretend to estimate from one or two examples.

**Why no LLM-based clustering of "similar" tasks.** Categorization is whatever string the caller passes. A future iteration may add embedding-based similarity ("find the 10 historical tasks most similar to this description"), but that's a substantive design choice with cost implications. v0.4.0 ships exact-match-by-category only.

**Implicit injection.** When the persona is presented with a task whose category has calibration data, the prompt assembly path may surface a one-liner: `Calibration: tasks tagged "code_review" usually take 30 min – 2 hours (median 47 min, n=14).` This is gated by a config flag `temporal.inject_duration_priors` (default `true`) and budget-capped at 80 tokens.

### J. Token-Budget Integration

The temporal layer adds three new sources of prompt tokens:

| Source | Typical cost | Budget treatment |
|--------|-------------|------------------|
| Now-anchor | 25–35 tokens, fixed per prompt | Unmetered — too small to budget. |
| Episode recency prefix | 5–10 tokens × N recalled episodes | Charged to the existing per-episode budget (RFC 0017). The `_MAX_EPISODE_SUMMARY_CHARS` cap is reduced by the prefix length so total cost is bounded. |
| Relationship recency tag | 5–10 tokens per surfaced relationship | Charged to the existing relationship-summary budget. |
| Commitments block | Up to `temporal.commitments_prompt_budget_tokens` (default 300) | New dedicated budget line. The greedy-fill loop in [memory_budget.py](../../agents/persona_runtime/memory_budget.py) admits commitments after notes, before episodes. |
| Duration calibration line | Up to 80 tokens, when present | Charged to a new `temporal.duration_priors_budget_tokens` line. |

The total worst-case temporal overhead for a typical prompt is ~400–500 tokens — measurable but not dominant against the existing 80k context window ([config/optimization.yaml:21](../../config/optimization.yaml#L21)). We commit to this overhead unconditionally rather than gating on a flag; the alternative (toggleable temporal layer) creates two prompt shapes that must both be tested and reasoned about, for negligible benefit.

---

## Security Considerations

- **Time as an exfiltration channel.** A compromised tool that could call `set_reminder` with attacker-controlled `due_at` and `description` could in principle smuggle data into the agent's next REMINDER event. Mitigation: `commitments:write` defaults to allow for first-party persona tools but is revocable, and commitment descriptions are subject to the same input-sanitization pass as message content (RFC 0009 Phase 1 — input sanitization). Description is capped at 1000 chars.
- **Reminder-storm DoS.** A persona that creates thousands of commitments could flood its own tick loop with `REMINDER` events. Mitigation: a soft cap on open commitments per agent (default 500) and a per-tick limit on `REMINDER` payload size (max 10 commitments per event; remainder are deferred to subsequent ticks).
- **Wall-clock skew as a manipulation surface.** A host with a tampered clock could cause the agent to perceive time incorrectly. Out of scope to detect — we trust the host clock and document that reliable timekeeping (NTP) is an operational prerequisite.
- **Cross-agent commitment-id leakage.** Each agent's commitment IDs are local ULIDs and are never exposed in protobuf payloads to peer agents. When a persona references a commitment in conversation, it does so by description, not ID. This is consistent with RFC 0020's `interaction_id` privacy stance ([RFC 0020 §Security](0020-interaction-lifecycle.md#security-considerations)).
- **Calibration data leaking task content.** `recall_typical_duration` returns aggregated quartiles only — it does not surface task descriptions or counterparties. The underlying `duration_records` table is local to the agent and not exposed via tools beyond the aggregated query.
- **Prompt injection via commitment descriptions.** Commitment text injected into the prompt block is wrapped in the same XML-style boundary delimiters used for user messages ([prompt_assembly.py:84-91](../../agents/persona_runtime/prompt_assembly.py#L84-L91)) when the commitment was created from an external (non-self) source. Self-created commitments are treated as trusted.

## Phased Implementation Plan

### Phase 1: Now-Anchor + Recency Rendering (v0.3.0)

**Summary**: Land the `Clock` abstraction, the now-anchor block in the system prompt, recency-tag rendering on episode and relationship recall. No schema changes. No new tools. Pure prompt-shape and rendering work on top of RFC 0020's timestamp columns.

**Deliverables**:

1. New module `agents/clock.py`: `Clock` protocol, `WallClock`, `FrozenClock` test impl.
2. New module `agents/temporal/rendering.py`: pure functions `format_relative(then, now, tz) → str`, `format_duration(seconds) → str`, `format_part_of_day(hour) → str`. Exhaustive unit coverage.
3. `_build_system_prompt` ([prompt_assembly.py:40](../../agents/persona_runtime/prompt_assembly.py#L40)) gains the now-anchor block. Clock injected via the persona-runtime constructor.
4. `memory_context.py` recall packaging gains recency-prefix rendering on episodes (uses `closed_at` from RFC 0020 §D where available, falls back to `created_at` for legacy rows).
5. Relationship summary path renders `last_interaction_at` as a recency tag and computes the cadence bucket.
6. Optional `persona.timezone` config field added to persona schema; defaults to `UTC`.
7. Telemetry counters: `temporal.now_anchor_emitted`, `temporal.recency_rendered{source=episode|relationship}`.

**Dependencies**: RFC 0020 Phase 1 (provides `started_at`, `closed_at`, `turn_count` columns). Phase 1 is otherwise self-contained — does not depend on RFC 0008 or RFC 0011.

**Out of Phase 1**: No commitments table, no new tools, no `REMINDER` event. The temporal experience after Phase 1 is "the persona knows when things happened" — not yet "the persona keeps promises."

### Phase 2: Commitments Memory Class (v0.4.0)

**Summary**: Add the `commitments` table, the commitment data model, and the commitment-management tools (`set_reminder`, `list_commitments`, `fulfill_commitment`, `cancel_commitment`). Surface upcoming commitments in the prompt block. No tick-loop coupling yet — commitments are read-on-demand.

**Deliverables**:

1. New module `agents/memory/commitments.py`: `Commitment` dataclass, store/query/mutate functions, lifecycle state machine.
2. Schema migration adding the `commitments` table and indices. New permission strings `commitments:read` and `commitments:write` registered in [agents/tools/permissions.py](../../agents/tools/permissions.py).
3. New tools in [agents/tools/builtin.py](../../agents/tools/builtin.py): `set_reminder`, `list_commitments`, `fulfill_commitment`, `cancel_commitment`. System-prompt nudge added.
4. Prompt assembly gains the open-commitments block, budgeted via `temporal.commitments_prompt_budget_tokens`.
5. `MemoryFacade.list_open_commitments(within_seconds=None)`, `MemoryFacade.list_commitments_to(target_party)`.
6. Integration test: persona told "remind me to call Bob tomorrow at 3pm" stores a commitment that appears in subsequent prompts.

**Dependencies**: Phase 1 (uses the `Clock` and the rendering helpers).

### Phase 3: REMINDER Event + Tick-Loop Janitor (v0.4.0)

**Summary**: Couple commitments to the autonomous tick loop. Pre-tick scan emits `REMINDER` events for proximate commitments; missed-commitment janitor transitions overdue rows.

**Deliverables**:

1. `EventType.REMINDER` added to [agents/persona_types.py:32](../../agents/persona_types.py#L32).
2. Persona-runtime tick handler gains the pre-tick commitment scan, with `temporal.reminder_horizon_sec` config (default 3600).
3. Missed-commitment janitor: periodic sweep transitioning `open` rows with `due_at < now - missed_grace_sec` to `missed`. Same cadence as tick.
4. Time-tool surface added: `get_current_time`, `time_since`, `time_until`. New `time:read` permission.
5. Telemetry counters: `temporal.reminders_emitted`, `temporal.commitments_missed`, `temporal.commitments_fulfilled`.
6. Integration test: commitment with `due_at` 30 min in the future fires a `REMINDER` event on the next tick within the horizon; commitment 2 days out does not.
7. Soft cap of 500 open commitments per agent enforced at `set_reminder` time with a clear error message.

**Dependencies**: Phase 2.

### Phase 4: Duration Calibration (v0.4.0)

**Summary**: Add the `duration_records` table and `recall_typical_duration` tool. Auto-populate from task completions and closed multi-turn interactions. Optional implicit injection of duration priors for tasks with calibration data.

**Deliverables**:

1. Schema migration adding `duration_records` table and the `idx_duration_category` index.
2. Auto-population hooks in [agents/persona_runtime/__init__.py](../../agents/persona_runtime/__init__.py) (on `COMPLETE_TASK` action) and in the RFC 0020 interaction-close path (multi-turn only).
3. New tool `recall_typical_duration` with quartile-based statistics; new tool `record_duration` for manual logging. New permission `duration:read` (default allow) and `duration:write` (default allow).
4. Optional duration-prior injection in prompt assembly when task category matches calibration data with `n_samples >= 3`. Gated by `temporal.inject_duration_priors` (default `true`).
5. Integration test: ten consecutive tasks of category `"code_review"` with varying durations produce a `recall_typical_duration("code_review")` result with sane median and IQR.

**Dependencies**: Phase 1 (uses recency rendering for the prior text). Independent of Phase 2/3.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | `agents/clock.py` | (new) Clock protocol, WallClock, FrozenClock |
| Python agents | `agents/temporal/rendering.py` | (new) pure rendering functions for relative time, duration, part-of-day |
| Python agents | `agents/temporal/__init__.py` | (new) module marker |
| Python agents | `agents/persona_runtime/prompt_assembly.py` | Inject now-anchor; commitment block (P2); duration prior line (P4); commitment-tool nudge (P2) |
| Python agents | `agents/persona_runtime/memory_context.py` | Render recency prefix on episodes; surface relationship recency; package commitments (P2) |
| Python agents | `agents/persona_runtime/memory_budget.py` | New budget lines for commitments and duration priors |
| Python agents | `agents/persona_runtime/__init__.py` | Pre-tick commitment scan (P3); duration auto-population on `COMPLETE_TASK` (P4) |
| Python agents | `agents/memory/commitments.py` | (new, P2) Commitment dataclass, store/query/mutate |
| Python agents | `agents/memory/duration.py` | (new, P4) Duration record store, quartile aggregation |
| Python agents | `agents/memory/migrations.py` | New tables: `commitments` (P2), `duration_records` (P4) |
| Python agents | `agents/memory/relationship.py` (or `relationship_queries.py`) | Surface `last_interaction_at` as recency-rendered field; cadence bucket |
| Python agents | `agents/persona_types.py` | `EventType.REMINDER` (P3) |
| Python agents | `agents/tools/builtin.py` | New tools: `set_reminder`, `list_commitments`, `fulfill_commitment`, `cancel_commitment`, `get_current_time`, `time_since`, `time_until`, `recall_typical_duration`, `record_duration` |
| Python agents | `agents/tools/permissions.py` | New permission strings: `time:read`, `commitments:read`, `commitments:write`, `duration:read`, `duration:write` |
| Python agents | `agents/dispatch.py` | `REMINDER` event routing (P3) |
| Config | `config/optimization.yaml` | New `temporal.*` block: `commitments_prompt_budget_tokens`, `duration_priors_budget_tokens`, `reminder_horizon_sec`, `missed_grace_sec`, `inject_duration_priors` |
| Schemas | `schemas/persona.schema.json` | Optional `timezone` field |
| Tests | `tests/unit/`, `tests/integration/` | Rendering buckets, clock injection, commitment lifecycle, REMINDER emission, duration aggregation |

No proto changes. No Go orchestrator changes (the temporal layer is agent-local). No CLI changes for Phases 1–3; Phase 4 may add a `persatrix persona inspect-time <agent>` debug subcommand, but that is optional and can be deferred.

## Test Strategy

- **Unit tests**:
  - `format_relative` exhaustive bucket coverage at the boundaries (59s vs 60s, 23h59m vs 24h, 6 days vs 7, etc.) with a `FrozenClock`.
  - `format_duration` for sub-minute, sub-hour, sub-day, multi-day, multi-week ranges.
  - `Clock` injection threaded through the persona runtime — verify no direct `time.time()` calls remain in `agents/temporal/`, `agents/memory/commitments.py`, or `agents/memory/duration.py` (a static-grep test).
  - Commitment state machine: open → fulfilled, open → missed, open → cancelled, illegal transitions rejected.
  - `recall_typical_duration` quartile correctness on synthetic samples; `n_samples < 3` returns the "no data" form.
  - Soft-cap enforcement on open commitments (501st `set_reminder` returns a clear error).
- **Integration tests**:
  - Frozen-clock prompt-rendering test: build a system prompt against a fixed timestamp, assert the now-anchor renders deterministically. Same for episode recall with a synthetic `closed_at`.
  - Persona conversation across mocked time gaps: turn 1 at T, turn 2 at T+3 days. Verify the second turn's prompt renders the first turn's episode with "3 days ago".
  - Commitment created via `set_reminder` for T+30min appears in the next prompt's commitments block; advancing the clock to T+25min and ticking emits a `REMINDER` event; advancing to T+90min without fulfillment transitions the commitment to `missed`.
  - Ten-task duration calibration loop produces a usable `recall_typical_duration` result.
  - Restart with open commitments: orchestrator restart, next tick re-emits `REMINDER` events for still-proximate open commitments.
- **Manual tests**:
  - End-to-end `persatrix chat` session: ask the persona to remind you of something tomorrow, check the commitments block in subsequent prompts via `persatrix persona inspect`. Confirm the persona references the commitment unprompted on the next tick.
  - Cross-session continuity: close and reopen the chat, verify the persona accurately reports how long it's been ("about 20 minutes") in its first turn of the new session.

## Open Questions

1. **Granularity of "today, HH:MM" rendering** — should the threshold for switching from "N hours ago" to "today, HH:MM" depend on whether it's the same calendar day in the agent's timezone? Calendar boundaries feel right but introduce timezone edge cases at midnight. **Lean**: yes, calendar-day-relative; document the midnight-boundary edge.
2. **Default value for `reminder_horizon_sec`** — proposed 3600s (1 hour). Too short and slow-tick agents miss reminders; too long and the persona is constantly nagged about commitments two hours out. Calibrate against early dogfood; revisit before Phase 3 ships.
3. **Should `set_reminder` accept relative time strings** (e.g., `"in 30 minutes"`, `"tomorrow at 3pm"`) **or only ISO-8601 / epoch?** Accepting natural language is more LLM-friendly but introduces parsing edge cases. **Lean**: accept ISO-8601 + epoch for v0.4.0; add a separate `parse_time(natural_text)` tool later if dogfood shows the LLM struggling.
4. **Surfacing missed commitments proactively** — should past-due `missed` commitments be surfaced in the prompt as a separate block ("you missed: …"), or only surface through episodic recall? §F commits to the latter. The former might be more behaviorally correct (a believable persona should be aware of having dropped the ball), but risks rumination loops. Revisit after Phase 3 dogfood.
5. **Cross-agent commitment visibility** — when Agent A says to Agent B "I'll have it by Friday", should B record an *expectation* in B's commitment store (with `target_party=A`)? The clean answer is "no — each agent records its own promises only." A richer answer is "each agent records its own promises and others' promises to it." The latter creates a more interesting social fabric but doubles the storage surface and creates new failure modes (A and B disagree on what was promised). **Lean**: own-promises-only for v0.4.0; revisit alongside RFC 0012 organizational layer.
6. **Duration category vocabulary** — should categories be free-form strings or constrained to a registered taxonomy? Free-form is flexible but sparse (n_samples stays low because everything goes into a slightly different bucket). A taxonomy gives statistical power but requires upkeep. **Lean**: free-form for v0.4.0 with no normalization; consider an embedding-based clustering layer later.
7. **Implicit duration-prior injection** — §I commits to injecting the duration prior automatically when the task has matching calibration data. Pro: the persona acts on history without needing a tool call. Con: tokens spent on every task prompt, even when calibration is weak. **Lean**: inject when `n_samples >= 3` and the IQR is bounded relative to the median (i.e., we have enough data to be confident). Configurable via `temporal.duration_prior_min_samples` (default 3) and `temporal.duration_prior_max_iqr_ratio` (default 2.0).
8. **Timezone display format in the now-anchor** — abbreviation (`UTC`, `PST`) is human-friendly but ambiguous (PST vs. PDT). Full IANA name (`America/Los_Angeles`) is unambiguous but token-expensive and less natural. **Lean**: render the IANA short form (e.g., `UTC`, `PT`) in the human part of the anchor; the ISO-8601 numeric offset already removes ambiguity for any consumer that needs it.

## Decision / Next Steps

This RFC formalizes the temporal substrate that has been implicitly missing across the persona stack. Phase 1 is small enough to land within v0.3.0 alongside RFC 0020 without disturbing the planned RFC 0007 / 0008 / 0011 chain. Phases 2–4 belong to v0.4.0, where they pair naturally with the organizational and skill-registry work — agents that can plan are also agents that can hold roles and own deliverables.

**Required before Phase 1 implementation begins**:

1. Resolve Open Questions 1, 2, and 8 in a PR-plan companion document (`docs/rfcs/0021-pr-plan.md`).
2. Confirm that RFC 0020 Phase 1 (which provides the `started_at` / `closed_at` columns this RFC reads) lands first; sequence Phase 1 of this RFC after RFC 0020 Phase 1 in the v0.3.0 dependency chain.
3. Add this RFC to `ROADMAP.md`'s RFC Master Index and the v0.3.0 "What ships" section (Phase 1 only); add a v0.4.0 line covering Phases 2–4.

**Required before Phases 2–4 implementation begins**:

1. Resolve Open Questions 3, 4, 5, 6, 7 in a v0.4.0 PR-plan document.
2. Confirm that RFC 0008 §D's context-packaging pipeline composes with the new commitments and duration-prior budget lines. Expected to be a no-op for RFC 0008 — the new lines are additional inputs to the same allocator.
3. Coordinate with RFC 0009 Phase 1 (input sanitization) so commitment descriptions and reminder text route through the sanitization pass before storage.

**Phasing rationale**: Phase 1 is the smallest unit of useful temporal awareness — a persona that knows what time it is and how old its memories are is materially more believable than one that doesn't. Shipping it in v0.3.0 means every channel conversation that lands under RFC 0011 already carries temporal annotation from day one, and no "we'll add timestamps to recall later" debt accumulates. Phases 2–4 are deliberately bundled into v0.4.0 because commitments without REMINDER are inert (a persona that stores promises but is never reminded of them is not behaving differently than one that stored a Note), and duration calibration without commitments lacks the natural "how long should I commit to?" call site.

## Related Documentation

- [RFC 0005](0005-persona-agent-memory.md) — Persona memory substrate; this RFC adds the temporal layer on top.
- [RFC 0008](0008-agent-memory-context-optimization.md) — Context packaging; consumes recency-rendered episodes and commitment-block budget lines.
- [RFC 0009](0009-security-sandboxing.md) — Input sanitization (Phase 1) for commitment descriptions and reminder text.
- [RFC 0011](0011-channels-bridges.md) — Channel recall benefits from recency rendering on inherited episode summaries.
- [RFC 0017](0017-persona-memory-injection-budget.md) — Memory budget; composes unchanged, with new optional budget lines for commitments and duration priors.
- [RFC 0020](0020-interaction-lifecycle.md) — Interaction lifecycle; this RFC's Phase 1 reads its `started_at` / `closed_at` columns.
- [Architecture Spec](../ai-agents-orchestration-spec.md)
