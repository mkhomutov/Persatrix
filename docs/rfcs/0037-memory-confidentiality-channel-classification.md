---
id: RFC-0037
title: Memory Confidentiality & Channel Classification
summary: Add an ordered confidentiality classification to every channel and a protection level to every channel-derived persona memory entry, with a deterministic hard gate in the memory-injection layer that withholds verbatim protected memory from any prompt assembled for a lower-classified channel — so a persona can learn from a confidential channel without leaking it.
type: feature
status: proposed
author: Maksim Khomutov
created: 2026-05-16
target: v0.3.x
depends_on:
  - RFC-0011
  - RFC-0036
---

# RFC 0037 — Memory Confidentiality & Channel Classification

**Type**: feature
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-05-16
**Target**: v0.3.x
**Depends on**: RFC 0011 (Channels — the `channels` table and the `channels.yaml` config surface a classification is added to), RFC 0036 (Persona Verbatim Message Recall — §F retrofits its server-side scoped-search query with the acting-channel classification filter)
**Relates to**: RFC 0034 (Persona Conversational Working Memory — the conversation window, shown in §H to be classification-safe by construction), RFC 0017 (Persona Memory Injection Token Budget — the injection layer the §D hard gate extends), RFC 0005 / RFC 0008 / RFC 0026 (the episodic, notes, and facts memory tiers that gain a protection level), RFC 0020 / RFC 0027 (Interaction Lifecycle / Reflection-Driven Consolidation — where protection levels are stamped and §E projections are generated), RFC 0009 (Agent Identity, Security & Sandboxing — the audit subsystem), RFC 0029 (Personal/Society Storage Split — protection levels must survive the migration to the society store), RFC 0012 (Protocols & Organizations — the *authority* axis and the enforced egress gate that this RFC's logging-only tripwire becomes), RFC 0038 (Persona Concurrent-Context Awareness & Cross-Channel Relay — enforces the single-channel-turn property §D and §H rely on, and gives cross-channel flow a §D-gated path)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. The classification lattice](#a-the-classification-lattice)
  - [B. Channel classification](#b-channel-classification)
  - [C. Memory provenance and protection level](#c-memory-provenance-and-protection-level)
  - [D. The hard gate at memory injection](#d-the-hard-gate-at-memory-injection)
  - [E. Declassification projections](#e-declassification-projections)
  - [F. Recall classification filter](#f-recall-classification-filter)
  - [G. The leak tripwire](#g-the-leak-tripwire)
  - [H. The conversation window is classification-safe by construction](#h-the-conversation-window-is-classification-safe-by-construction)
  - [I. Forward-compatibility with the storage split](#i-forward-compatibility-with-the-storage-split)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

A persona that belongs to several channels accumulates memory derived from
all of them. [RFC 0036](0036-persona-message-recall.md) controls what a
persona may **recall from the channel store** — reads are scoped to the
channels and membership intervals the persona had access to. Nothing
controls what a persona may **say** with what it legitimately learned: a
persona that learned something in a confidential channel can paraphrase
or quote it into a public one. Recall is gated on the way *in*; egress is
not gated on the way *out*.

This RFC adds a **confidentiality axis**:

1. An ordered **classification** (`public` < `internal` < `restricted` <
   `secret`) on every channel — declared in `config/channels.yaml` for
   group channels, defaulted for DMs and threads, and stored on the
   channel store's `channels` table.
2. A **protection level** stamped onto every persona memory entry derived
   from channel content (episodes, facts, notes) — equal to the maximum
   classification across the entry's provenance.
3. A **deterministic hard gate** in the memory-injection layer: when a
   turn's prompt is assembled for a channel of classification `L`, no
   memory entry with a protection level above `L` is injected in
   **verbatim** form.

Because a persona turn is **single-channel** — one inbound event, one
channel, one prompt, one set of candidate actions, all addressed to that
channel (§H) — the hard gate makes *"verbatim confidential memory never
reaches a lower-classified channel's prompt"* a **structural guarantee**,
not a behavioural request that depends on the model complying.

A **declassification projection** mechanism (§E) lets a protected memory
still *inform* the persona at lower classifications in abstracted form —
"you have reason to be cautious about Project X" without the reason — so
the persona can **learn from a confidential channel without leaking it**.
A logging-only **leak tripwire** (§G) observes the residual paraphrase
path.

This RFC is the **confidentiality** half of a two-axis model. The
**authority / integrity** half — a persona reasoning about whether a
*directive* from one context should influence its behaviour in another,
and accepting / adapting / refusing it — is [RFC 0012](0012-protocols-organizations.md),
because authority is only rankable against an organization structure.
Confidentiality is enforceable now, inside a single persona's own memory
store plus channel config, with no organizations, no shared society
store, and no policy engine — which is why it ships in v0.3.x.

## Motivation

### The capability gap

Personas are multi-channel. The demo set alone (`config/agents.yaml`)
puts three personas on a shared `planning` channel, and DM channels open
on demand between any pair. Every channel is first-class and
**unclassified** — `config/channels.yaml` has no notion that one channel
might be more sensitive than another. A persona that discusses an
unannounced reorganization in a leadership DM, then answers a question in
a public channel an hour later, carries the first conversation in its
episodic memory and has no signal that repeating it is a disclosure.

RFC 0036 closed the *read-in* side of channel access: a persona can only
recall channels it was a member of, scoped to its membership intervals,
enforced server-side in SQL. But RFC 0036 is explicit that **rebroadcast
is out of scope** — see its Security Considerations: "verbatim recall
lets a persona lift user A's exact words from channel X and repeat them
into channel Y … recall *is* read access to that text." RFC 0036 leaves
the *say-out* side open by design. This RFC closes it.

### The single-channel-turn property makes this enforceable

A persona turn is driven by exactly one inbound event with exactly one
`channel_id` ([`action_loop.py` `_on_event_inner`](../../agents/persona_runtime/action_loop.py)).
The prompt is assembled once, for that channel; the candidate actions a
turn can emit (`SEND_CHANNEL_MESSAGE`, `USE_TOOL`, `DO_NOTHING`, …) all
resolve against that same channel. A turn cannot read channel A and, in
the same turn, publish to channel B. Information crosses a channel
boundary only by being **written to memory in one turn and read back in a
later turn** — and that later turn re-assembles its prompt from scratch.

That is the property that makes a confidentiality gate *deterministic*
rather than aspirational. If the memory-injection layer withholds
verbatim protected memory whenever the turn's channel is too low, then a
persona acting in a public channel **physically does not have** the
confidential text in its context window. It cannot quote what it was
never shown. The gate does not depend on the model choosing to be
discreet.

### "Learn from it without leaking it"

A hard gate that simply withholds protected memory is *safe* but blunt:
the persona acting in a public channel becomes unable to be *influenced*
at all by what it learned in confidence. The intent is finer — the
persona should be able to *act on* a confidential belief without
*disclosing* it. If it learned in a leadership DM that a project is
likely to be cancelled, it should be able to be appropriately cautious
about that project in a public channel without saying why. That is what
the §E declassification projection delivers: a lower-classified,
abstracted form of a protected memory that carries the *bearing* without
the *content*.

### Why this is a v0.3.x RFC

Confidentiality is **local**. Every mechanism here lives inside one
persona's own `memory.db` (the protection level and projections) or in
channel configuration and the channel store (the classification). It
needs no organization graph, no shared society store, and no decision
policy engine. It is the same class of change as RFC 0034 / 0035 / 0036:
schema columns plus an injection-layer rule.

The **authority** axis is not local — "a directive from context A
outranks a directive from context B" is meaningless without roles and a
hierarchy to rank A and B. That axis, and the *enforced* (blocking)
egress gate, depend on the organization model and ship in
[RFC 0012](0012-protocols-organizations.md) for v0.4.0. This RFC and
RFC 0012 are deliberately split along the line of *what is enforceable
without organizations*.

## Goals

1. An ordered, four-level **classification** is declarable per group
   channel in `config/channels.yaml`, defaulted for DMs and threads, and
   persisted on the channel store's `channels` table.
2. Every persona memory entry derived from channel content (episodes,
   facts, notes) carries a **protection level** equal to the maximum
   classification across its provenance.
3. A **deterministic hard gate** in the memory-injection layer withholds
   verbatim protected memory from any prompt assembled for a channel
   whose classification is below the entry's protection level.
4. Given the single-channel-turn property (§H), the hard gate is a
   **structural guarantee**: verbatim protected memory cannot enter the
   prompt for a lower-classified channel, independent of model behaviour.
5. **Declassification projections** let a protected memory inform the
   persona at lower classifications in abstracted form, so the persona
   can learn from a confidential channel without leaking it.
6. RFC 0036 verbatim recall is retrofitted with an **acting-channel
   classification filter** (§F): recall cannot return a `secret`-channel
   message verbatim into a turn acting in a `public` channel.
7. A logging-only **leak tripwire** (§G) makes the residual paraphrase
   path observable, without blocking — the enforced gate is RFC 0012.
8. No new prompt-injection surface: classified content reaching the model
   still passes the RFC 0034 / RFC 0036 `_format_event` delimiter-escape.

## Non-Goals

- **The authority / integrity axis.** Whether a *directive* issued in one
  context should influence the persona's behaviour in another — and the
  persona accepting, adapting, or refusing it — is
  [RFC 0012](0012-protocols-organizations.md). This RFC governs what
  flows *out* of a context; RFC 0012 governs what flows *in*.
- **An enforced, blocking egress gate.** v0.3.x ships the tripwire as
  **observability only** (§G). Promoting it to a gate that blocks or
  routes a publish to human review is an organizational-policy decision
  (whose policy, what escalation path) and ships in RFC 0012.
- **Persona clearance and membership-time checks.** *Which* channels a
  persona may join — a clearance derived from organizational role — is
  RFC 0012. This RFC classifies channels and protects derived memory; it
  does not change who is configured into a channel.
- **Encryption or at-rest cryptography.** A classification is an
  access-control *label*, not a cryptographic boundary. The channel
  store and `memory.db` are unencrypted as today.
- **Classifying relationship trust *scores*.** A bond's numeric trust
  score is not classified; the episodic detail *behind* a bond inherits
  the protection level of its source episode like any other episode.
- **Operator-defined custom lattice levels.** The lattice is the fixed
  four levels of §A in v0.3.x. Extensibility is Open Question #1.
- **Retroactive reclassification.** Changing a channel's classification
  applies going forward. Backfill of pre-existing memory is a one-time
  migration default (§C); re-deriving historical protection levels after
  a later reclassification is out of scope.
- **Recall-store / membership mechanics.** Owned by RFC 0035 / RFC 0036.

## Design / Implementation

### A. The classification lattice

A fixed, totally ordered lattice of four levels:

| Level | Rank | Meaning |
|-------|:----:|---------|
| `public` | 0 | No confidentiality expectation. The safe floor. |
| `internal` | 1 | Ordinary in-org conversation. **The default.** |
| `restricted` | 2 | Sensitive; need-to-know within a subset. |
| `secret` | 3 | Highly sensitive; disclosure is a material harm. |

The only operations the system needs are the total order (`a ≤ b`) and
`max`. A single canonical helper — `classification_rank(level) -> int` —
is defined once (Go and Python sides) and every comparison goes through
it; no code compares level strings directly. An unknown or absent level
resolves to the default `internal`, never to `public`, so a
mis-configuration fails **closed** (more restrictive), not open.

### B. Channel classification

**Config.** `config/channels.yaml` gains an optional `classification`
field per group channel; `schemas/channel.schema.json` gains a matching
`enum` (`public` | `internal` | `restricted` | `secret`) with
`default: internal`. Absent ⇒ `internal`.

```yaml
channels:
  - name: planning
    description: "engineering + product planning discussion"
    classification: internal          # new; default if omitted
    members:
      - id: ember-owl
        respond: when_mentioned
  - name: leadership
    classification: restricted        # need-to-know
    members: [ember-owl]
```

**Channel store.** Channel-store schema migration **v6** (RFC 0035 landed
v4, RFC 0036 lands v5; this is the next `case 6:` arm in
`applyMigration`, a `migrateV5ToV6` function, `channelStoreSchemaVersion`
bumped to 6, `user_version` stamped inside the migration transaction)
adds a `classification TEXT NOT NULL DEFAULT 'internal'` column to the
`channels` table ([`internal/channels/sqlite_schema.go`](../../internal/channels/sqlite_schema.go)).
The migration backfills every existing channel to `internal`.

- **Group channels** load their declared classification into the
  `channels` row when config is applied.
- **DM channels** (`dm:<a>:<b>`, created on demand by `GetOrCreateDM`)
  are stamped `internal` at creation. A DM is not declared in config, so
  there is nowhere to set it otherwise; Open Question #2 covers whether a
  DM should instead take `min` of its participants' clearances once
  RFC 0012 introduces clearance.
- **Thread channels** (`thread:<message_id>`) copy the classification of
  their parent channel at creation. A thread is never more or less
  confidential than the conversation it forks from.

**On the wire.** The persona runtime needs the acting channel's
classification to run the §D gate. Rather than have the runtime fetch
channel metadata per turn, the orchestrator stamps it onto the channel
event: `ChannelMessageEvent` (`proto/task.proto`) gains a
`classification` string field, populated from the `channels` row when
the event is dispatched. The runtime reads it straight off the event.
A persona's own *autonomous tick* event carries no channel — see §D for
how the gate handles that.

### C. Memory provenance and protection level

Every memory entry that is **derived from channel content** carries a
**protection level**: the maximum classification across the channels its
content came from. A new `agents/memory/migrations.py` migration adds, to
the episodic, facts, and notes tiers:

- `protection_level TEXT NOT NULL DEFAULT 'internal'`
- `source_channel_id TEXT` — the channel the entry was derived from,
  where a single one applies (nullable for synthesized notes).

Where each tier gets its protection level:

- **Interactions** (RFC 0020). An interaction is scoped to exactly one
  channel ([RFC 0020 §G](0020-interaction-lifecycle.md)). The interaction
  record (`agents/memory/interactions.py`) captures the channel's
  classification **at interaction-open** from the channel event (§B). The
  interaction is the single point of truth that the episodic and facts
  tiers below inherit from — so classification is read once per
  interaction, not re-derived per episode or per fact.
- **Episodes** (RFC 0005 / RFC 0008). An episode consolidates one
  interaction; its `protection_level` is the interaction's captured
  classification.
- **Facts** (RFC 0026). A fact is extracted from one interaction; its
  `protection_level` is likewise the interaction's classification. The
  extractor stamps it unconditionally — there is no path that writes a
  fact without a protection level.
- **Notes** (agent-authored prose). A note is written *during a turn*. By
  the §D hard gate, a turn acting in a channel of classification `L` only
  ever has memory with protection level `≤ L` in its context, plus that
  channel's own content (`= L`). A note authored in that turn therefore
  cannot contain anything above `L`, and is stamped `protection_level =
  L`. This is a clean consequence of the gate: notes need no separate
  provenance analysis.
- **Relationship bonds.** Out of scope (see Non-Goals). The trust score
  is unclassified; the episodic detail behind it is an episode and is
  protected as one.

**Migration backfill.** Pre-existing memory has no protection level. The
migration backfills each entry from its recorded source channel's
classification where one is recorded (episodes and facts carry channel
context today), and to the `internal` default otherwise. Because every
channel also backfills to `internal` (§B), the common pre-existing case
resolves consistently to `internal` — neither silently `public` (a
disclosure) nor silently `secret` (which would withhold a persona's
entire history from itself).

### D. The hard gate at memory injection

The memory-injection layer ([`memory_context.py`](../../agents/persona_runtime/memory_context.py),
budgeted per [RFC 0017](0017-persona-memory-injection-budget.md)) gains
one filter, applied to every tier that injects channel-derived memory
(episodic recall, channel-history summary, facts, notes) **before** the
RFC 0017 token budget is applied:

```
let L = classification of the turn's acting channel        (from §B)
for each candidate memory entry E with protection level P:
    if rank(P) <= rank(L):   inject E in full
    else:                    inject the best declassification
                             projection of E with level <= L (§E),
                             or — if none exists — withhold E entirely
```

```mermaid
flowchart TD
    EV[Inbound channel event] --> L[acting classification L]
    MEM[(persona memory tiers)] --> CAND[candidate entries]
    CAND --> CMP{rank P  ≤  rank L ?}
    L --> CMP
    CMP -->|yes| FULL[inject verbatim]
    CMP -->|no| PROJ{projection ≤ L exists?}
    PROJ -->|yes| ABS[inject declassified projection]
    PROJ -->|no| DROP[withhold entirely]
    FULL --> BUDGET[RFC 0017 token budget]
    ABS --> BUDGET
    BUDGET --> PROMPT[system prompt]
```

The filter is **deterministic** and runs in the runtime before any LLM
call. The model never sees withheld content and cannot request it — there
is no tool or argument that overrides the acting classification, exactly
as RFC 0036 binds the recall scope server-side and not from LLM input.

**The structural guarantee.** Combine the gate with the single-channel-
turn property (§H): a turn acting in channel `C` assembles exactly one
prompt, gated to `C`'s classification, and can publish only to `C`.
Therefore verbatim memory of protection level `P` can reach a channel of
classification `L < P` only across *separate* turns — and every separate
turn re-runs this gate. There is no path by which verbatim protected
memory enters the prompt of a lower-classified channel. This is the
load-bearing guarantee of the RFC.

> **Implementation note — single-channel-turn is assumed, not yet
> enforced.** The clause "can publish only to `C`" describes the
> *intended* runtime, not the code today: `SEND_CHANNEL_MESSAGE` carries
> its own `channel_id` payload and `validate_action_payload`
> ([`action_validation.py`](../../agents/persona_runtime/action_validation.py))
> checks only that it is non-empty — not that it equals the turn's
> inbound channel. A turn can therefore publish to any channel the
> persona belongs to, and this gate — which keys on the *inbound*
> channel — would not catch a leak on that path.
> [RFC 0038](0038-concurrent-context-awareness-relay.md) §B promotes
> single-channel-turn to a code-enforced invariant and routes deliberate
> cross-channel flow through the §D-gated relay; until its Phase 1 lands,
> this guarantee is contingent on that enforcement, with the §G tripwire
> (which *is* destination-aware) as the interim backstop.

**Autonomous ticks.** A tick event (`EventType.TICK`) carries no channel
([RFC 0005](0005-persona-agent-memory.md) autonomy loop). A tick can
still emit a `SEND_CHANNEL_MESSAGE` to any channel the persona belongs
to. There is no single acting classification, so the gate uses a
**floor**: tick injection is gated to `public` by default — the most
conservative level — so a tick cannot pull verbatim `internal`-or-above
memory into a context that might publish to a public channel. This is
deliberately strict; Open Question #3 covers softening it to the `min`
classification across the persona's channels, which is tighter but
needs the persona→channels mapping a tick does not currently carry.

### E. Declassification projections

A hard gate alone makes a persona *amnesiac* below a memory's level. A
**declassification projection** restores the *bearing* without the
*content*: a lower-classified, abstracted restatement of a protected
memory.

A new `memory_projections` table (same `agents/memory/migrations.py`
migration) holds zero or more projections per protected memory entry:

| Column | Meaning |
|--------|---------|
| `entry_id`, `entry_tier` | The protected episode / fact / note. |
| `level` | The projection's own (lower) classification. |
| `text` | The abstracted restatement, safe at `level`. |

The §D gate, when it must withhold an entry `E`, selects the projection
of `E` with the **highest `level` that is still `≤ L`** and injects that
instead; if `E` has no projection at or below `L`, `E` is withheld
entirely.

**Where projections come from.** Interaction consolidation already makes
an LLM call to summarize an interaction (RFC 0020 close;
[RFC 0027](0027-reflection-driven-consolidation.md) reflection). For a
protected interaction, that same call is asked to *also* produce a
one-line declassified projection at each lower level — no new LLM call,
one extra structured field on a call that already happens. A projection
is therefore best-effort: it is generated by the model, and a model can
abstract imperfectly.

**The honest boundary.** The deterministic guarantee of this RFC is §D's
withholding of **verbatim** protected memory. A projection is *not* part
of that guarantee — it is a best-effort affordance so the persona is
informed rather than amnesiac. A projection that over-discloses is a soft
failure, caught (not prevented) by the §G tripwire. This split is stated
plainly so reviewers do not over-trust projections: **verbatim text is
gated; abstractions are best-effort.** Phase 2 ships projections; Phase 1
ships §D with withhold-only behaviour and is already correct and safe
without them — just blunter.

### F. Recall classification filter

[RFC 0036](0036-persona-message-recall.md) gives a persona a
`recall_channel_messages` tool whose server-side query returns verbatim
messages from every channel the persona was a member of. That scope is
correct for *read access* but is **blind to the acting channel**: a
persona acting in a `public` channel could recall, verbatim, a message
from a `secret` channel it legitimately belongs to — a direct egress of
classified text into a lower context, straight past §D (which gates
*memory*, while recall reads the *channel store*).

This RFC retrofits the RFC 0036 scoped-search query
(`internal/channels/sqlite_search.go` §C — created by RFC 0036's implementation)
with one additional, non-optional clause:

```sql
   -- ... existing membership-interval EXISTS clause (RFC 0036 §C) ...
   AND (SELECT classification_rank(c.classification)
          FROM channels c WHERE c.id = m.channel_id)
       <= classification_rank(?)        -- the acting channel's level
```

The acting channel's classification is bound server-side from a new
required parameter on the recall endpoint
(`POST /api/v1/personas/{id}/recall`), and the `recall_channel_messages`
tool passes the **current event's** `classification` (§B) into it —
closure-bound alongside `agent_id`, never LLM-supplied, the same trust
pattern RFC 0036 already uses for the scope participant. A recall result
can never be more confidential than the channel the persona is acting in.

Recall while acting on an autonomous **tick** uses the §D `public` floor.

### G. The leak tripwire

§D and §F close the *verbatim* paths. The residual path is the persona
**paraphrasing** — restating a protected memory, or over-sharing a §E
projection, in its own generated words. This cannot be prevented
deterministically (it is the model's output, in novel wording) and a
*blocking* response to it is an organizational-policy decision deferred
to RFC 0012. v0.3.x ships **observability**:

When a persona emits a `SEND_CHANNEL_MESSAGE`, the publish path
([`channel_publisher.py`](../../agents/channel_publisher.py)) runs a
tripwire **before** the message leaves: for each protected memory entry
that was in the turn's context with protection level above the target
channel's classification, check the outgoing text for a verbatim span of
that entry's content (a normalized substring match over spans above a
length threshold — lexical, not semantic). On a hit, emit an RFC 0009
audit event (`channel.confidentiality_tripwire`) recording the persona,
the target channel and its classification, and the implicated entry's
protection level — **not** the leaked text itself. The message is **not
blocked**. The tripwire is a smoke detector, not a lock.

Because §D already keeps verbatim protected memory out of the prompt, a
*true* verbatim tripwire hit on a sub-`L` channel indicates a **bug** —
an entry stamped with the wrong protection level, a projection that
copied source text verbatim, or a missed injection path — which is
exactly the class of defect this RFC most needs surfaced in early
operation. Tuning the span threshold waits until tripwire telemetry from
real workloads exists; it is not guessed here and not put on a calendar.

### H. The conversation window is classification-safe by construction

[RFC 0034](0034-persona-conversational-working-memory.md)'s conversation
window reconstructs the LLM `messages` array from the **last N messages
of the turn's own channel** — `event.channel_id` and no other. A turn
acting in channel `C` therefore only ever sees `C`'s own transcript in
its window, and `C`'s transcript is by definition at `C`'s
classification. The window can neither raise nor lower the confidentiality
of what the turn sees; it needs **no gate and no change**. This section
exists to record that the analysis was done — the window is the obvious
place to suspect a cross-channel leak, and it is in fact safe because it
is single-channel, the same property §D and §H depend on throughout.

### I. Forward-compatibility with the storage split

[RFC 0029](0029-personal-society-storage-split.md) moves persona memory
toward a personal/society split with a Postgres society backend in
v0.4.0. The `protection_level`, `source_channel_id`, and
`memory_projections` columns introduced here are ordinary tier columns
and travel with the episodic / facts / notes tiers through that
migration unchanged — protection level is **a property of the memory
entry**, not of the storage engine. RFC 0029's facade work must carry
these columns; this is noted so the storage split does not silently drop
them. No society-store schema beyond what RFC 0029 already plans is
required by this RFC.

## Security Considerations

- **The verbatim guarantee is deterministic; the abstraction is not.**
  §D withholding of verbatim protected memory, combined with the
  single-channel-turn property (§H), is a structural guarantee that does
  not depend on model behaviour. §E declassification projections are
  LLM-generated and **best-effort** — a projection may abstract
  imperfectly. Reviewers must not conflate the two: *verbatim text is
  gated; abstractions are best-effort and observed by the §G tripwire.*
- **Fail-closed on misconfiguration.** An absent or unrecognized
  classification resolves to `internal`, never `public` (§A). A channel
  the operator forgot to classify is treated as confidential-by-default,
  not public-by-default.
- **The gate is not LLM-controllable.** The acting classification comes
  from the channel event (§B), bound in the runtime; the recall filter's
  acting classification is bound server-side (§F). There is no tool, no
  argument, and no prompt phrasing that raises a turn's effective
  classification. This mirrors RFC 0036's closure-bound scope.
- **Recall egress is closed (§F).** Before this RFC, RFC 0036 verbatim
  recall could return `secret`-channel text into a `public`-channel turn.
  The §F classification clause is non-optional and server-side; recall
  output can never exceed the acting channel's classification.
- **Residual paraphrase risk is documented, not eliminated.** A persona
  can still restate a protected memory in novel words (§G). v0.3.x makes
  this **observable** via the tripwire; *blocking* it is RFC 0012's
  enforced egress gate. This residual risk is explicitly accepted for
  v0.3.x and is the reason RFC 0012 exists.
- **Correctness depends on the protection level being stamped right.**
  The gate is only as good as the `protection_level` on each entry. §C
  routes every channel-derived tier through a single choke point — the
  interaction record's captured classification — so there is one place
  to audit, not three. The tripwire (§G) is the backstop that surfaces a
  mis-stamp in operation.
- **Prompt injection is unchanged.** Classified content that *is*
  injected (at or below the acting level, or as a projection) still
  passes the RFC 0034 / RFC 0036 `_format_event` delimiter-escape
  ([`prompt_assembly.py`](../../agents/persona_runtime/prompt_assembly.py)).
  This RFC narrows *what* is injected; it does not change the sanitization
  *of* what is injected.
- **Audit.** Channel reclassification and every tripwire hit emit RFC 0009
  audit events. Reclassification is a security-relevant configuration
  change and leaves a trail; the tripwire trail records metadata only,
  never the implicated text, so the audit log does not itself become a
  confidentiality sink.
- **Backfill is conservative-neutral.** The §C migration backfills to
  `internal`, not `public` — pre-existing memory is never silently
  declassified to the public floor by the act of upgrading.

## Phased Implementation Plan

### Phase 1: Classification, protection level, the hard gate, and the recall filter

The complete **deterministic** confidentiality boundary. Safe and correct
on its own; §E projections only make it less blunt.

1. **Lattice helper** — `classification_rank` in Go and Python, single
   source each side; fail-closed on unknown input.
2. **Channel classification** — `classification` field in
   `config/channels.yaml` + `schemas/channel.schema.json`; channel-store
   migration **v6** adding `channels.classification`; DM-creation and
   thread-creation stamping (§B); `classification` on `ChannelMessageEvent`
   (`proto/task.proto`) and the dispatch path.
3. **Protection level** — `agents/memory/migrations.py` migration adding
   `protection_level` / `source_channel_id` to the episodic, facts, and
   notes tiers, plus the `memory_projections` table (created here, used in
   Phase 2); interaction-open classification capture (§C); episodic and
   facts consolidation stamping; migration backfill.
4. **The hard gate** — the §D filter in `memory_context.py`, with
   withhold-only behaviour (no projections yet); the autonomous-tick
   `public` floor.
5. **Recall classification filter** — the §F clause in
   `internal/channels/sqlite_search.go`, the new required acting-channel
   parameter on the recall endpoint, and the `recall_channel_messages`
   tool passing the event classification.
6. Unit, migration, and integration tests per the Test Strategy.

Dependencies: **RFC 0011** (the `channels` table). Step 5 depends on
**RFC 0036** Phase 1 — if RFC 0036 has not yet landed, step 5 lands
together with it; steps 1–4 are independent of RFC 0036.

### Phase 2: Declassification projections

1. `memory_projections` writes: the interaction-consolidation LLM call
   (RFC 0020 close / RFC 0027 reflection) extended to emit one-line
   projections at each lower level.
2. The §D gate's projection-selection branch (highest projection `≤ L`).
3. Integration test: a persona acting in a `public` channel is correctly
   *informed* by — but does not verbatim-disclose — a `restricted`
   memory, via its projection.

Dependencies: Phase 1.

### Phase 3: The leak tripwire

1. The §G tripwire in `channel_publisher.py`; the
   `channel.confidentiality_tripwire` RFC 0009 audit event in
   `internal/security/audit_event.go`.
2. Tripwire-rate instrumentation alongside the existing persona metrics.
3. Integration test: a deliberate verbatim paraphrase is detected and
   audited; a benign message produces no hit.

Dependencies: Phase 1. Independent of Phase 2 and separately reviewable.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Go orchestrator | `internal/channels/sqlite_schema.go` | v6 migration: `channels.classification` column + backfill; bump `channelStoreSchemaVersion` |
| Go orchestrator | `internal/channels/sqlite.go` (DM/thread creation) | Stamp classification on `GetOrCreateDM` and thread creation (§B) |
| Go orchestrator | `internal/channels/sqlite_search.go` | §F acting-channel classification clause on the RFC 0036 scoped query |
| Go orchestrator | `internal/server/channel_handlers.go`, `channel_types.go` | Required acting-channel parameter on the recall endpoint |
| Go orchestrator | `internal/channels/` (event dispatch), `internal/security/audit_event.go` | `classification` on dispatched channel events; reclassification + `channel.confidentiality_tripwire` audit events |
| Go orchestrator | `internal/channels/classification.go` (new) | `classification_rank` helper, fail-closed |
| Protos | `proto/task.proto` | `classification` field on `ChannelMessageEvent` |
| Python agents | `agents/memory/migrations.py` | `protection_level` / `source_channel_id` on episodic/facts/notes; `memory_projections` table |
| Python agents | `agents/memory/interactions.py` | Capture channel classification at interaction-open |
| Python agents | `agents/memory/episodic.py`, `agents/memory/facts.py`, `agents/memory/notes.py` | Stamp / read `protection_level` |
| Python agents | `agents/persona_runtime/memory_context.py` | The §D hard gate + projection selection |
| Python agents | `agents/persona_runtime/classification.py` (new) | Python `classification_rank` helper |
| Python agents | `agents/channel_publisher.py` | §G leak tripwire |
| Python agents | `agents/tools/recall.py` | Pass acting-channel classification to the recall endpoint |
| Python agents | consolidation path (`agents/memory/`, RFC 0027 reflection) | Emit §E projections during interaction consolidation |
| Config / schema | `config/channels.yaml`, `schemas/channel.schema.json` | `classification` field + `enum` |
| Docs | `docs/guides/persona-agents.md`, `docs/diagrams/memory-architecture.md` | Document classification, protection levels, the two-axis model |
| Tests | `internal/channels/*_test.go`, `tests/unit/python/`, `tests/integration/persona/` | Per Test Strategy |

## Test Strategy

- **Unit tests**:
  - `classification_rank` total order and fail-closed-to-`internal` on
    unknown input, both Go and Python.
  - The §D gate: an entry with `protection_level` at, below, and above
    the acting classification is injected fully, injected fully, and
    withheld respectively; with a projection present, the highest
    projection `≤ L` is selected; the autonomous-tick `public` floor.
  - The §C stamping: an episode and a fact from a `restricted`
    interaction are stamped `restricted`; a note authored in an
    `internal` turn is stamped `internal`.
  - The §F query: a `secret`-channel message is excluded from a recall
    acting in a `public` channel and included from one acting in a
    `secret` channel; the clause composes with the RFC 0036 membership
    join.
  - The §G tripwire fires on a verbatim span and not on a benign
    message; the audit event carries metadata and not the text.
- **Migration tests**: v5 → v6 adds `channels.classification` and
  backfills to `internal`; the memory migration adds the columns and
  `memory_projections` table and backfills `protection_level` from
  recorded source channels where present, `internal` otherwise; both
  migrations are idempotent on reopen and stamp `user_version` inside the
  transaction.
- **Integration tests**:
  - A persona learns a fact in a `restricted` channel, then acts in a
    `public` channel: the verbatim fact is absent from the public-turn
    prompt; with Phase 2, its projection is present.
  - End-to-end recall: a persona that belongs to a `secret` channel
    cannot recall its messages while acting in a `public` channel, and
    can while acting in the `secret` channel.
- **Manual tests**: a new `MT-PERSONA-CONFIDENTIALITY-001` — a persona on
  both a `restricted` leadership channel and the `public` `planning`
  channel is asked, in `planning`, a question whose answer it knows only
  from the leadership channel; it must decline to disclose while still
  acting consistently with what it knows (the §E "learn from it without
  leaking it" behaviour).

## Open Questions

1. **Operator-defined lattice levels.** The lattice is fixed at four
   levels (§A). Proposed resolution: ship the fixed lattice — four levels
   cover the v0.3.x channel use case, and a configurable lattice is
   complexity with no current consumer. Revisit if RFC 0012's
   organizational clearances need finer gradations.
2. **DM classification.** DMs are stamped `internal` at creation (§B).
   Once RFC 0012 introduces per-persona clearance, a DM could instead
   take `min` of its two participants' clearances. Proposed resolution:
   `internal` in v0.3.x; revisit in RFC 0012 when clearance exists.
3. **Autonomous-tick gate floor.** §D gates tick injection to `public`.
   A softer rule — the `min` classification across the persona's
   channels — is less amnesiac on ticks but needs a persona→channels
   mapping the tick event does not carry today. Proposed resolution:
   ship the `public` floor; reconsider if tick-time over-withholding is
   observed to matter.
4. **Projection generation cost.** §E adds structured output to the
   consolidation LLM call. If a future consolidation path is made
   non-LLM, projections would need another source. Proposed resolution:
   acceptable while consolidation is LLM-based; flagged for whoever
   revisits consolidation.
5. **Tripwire span threshold.** §G matches verbatim spans above a length
   threshold. The threshold is not specified here. Proposed resolution:
   ship a conservative default and tune when tripwire telemetry from real
   workloads exists — not on a fixed schedule.

## Decision / Next Steps

1. Review this RFC alongside [RFC 0012](0012-protocols-organizations.md):
   the two are the confidentiality and authority halves of one model and
   should be read together, even though they ship in different versions.
2. Sequence after [RFC 0036](0036-persona-message-recall.md) so the §F
   recall filter has a query to retrofit; Phase 1 steps 1–4 may proceed
   in parallel with RFC 0036.
3. Implement Phase 1 (the deterministic boundary), then Phase 2
   (projections — the "learn from it" affordance), then Phase 3 (the
   tripwire). Phase 1 is safe and shippable without Phases 2–3.
4. Create `docs/rfcs/0037-pr-plan.md` with PR slices once this RFC is
   accepted.
5. Regenerate [INDEX.md](INDEX.md) via `make rfcs`.

## Related Documentation

- [RFC 0012 — Protocols & Organizations](0012-protocols-organizations.md) — the *authority / integrity* axis and the enforced egress gate; the other half of the two-axis model.
- [RFC 0036 — Persona Verbatim Message Recall](0036-persona-message-recall.md) — the recall query §F retrofits; the egress side this RFC closes.
- [RFC 0034 — Persona Conversational Working Memory](0034-persona-conversational-working-memory.md) — the conversation window, shown classification-safe in §H.
- [RFC 0038 — Persona Concurrent-Context Awareness & Cross-Channel Relay](0038-concurrent-context-awareness-relay.md) — enforces the single-channel-turn property §D and §H assume; routes deliberate cross-channel flow through the §D gate.
- [RFC 0011 — Channels & Internal Agent Messaging](0011-channels-bridges.md) — the `channels` table and `channels.yaml` config surface.
- [RFC 0017 — Persona Memory Injection Token Budget](0017-persona-memory-injection-budget.md) — the injection layer the §D gate extends.
- [RFC 0020 — Interaction Lifecycle](0020-interaction-lifecycle.md) / [RFC 0027 — Reflection-Driven Consolidation](0027-reflection-driven-consolidation.md) — where protection levels are stamped and projections generated.
- [RFC 0026 — Declarative Facts Tier](0026-declarative-facts-tier.md) / [RFC 0005](0005-persona-agent-memory.md) / [RFC 0008](0008-agent-memory-context-optimization.md) — the memory tiers that gain a protection level.
- [RFC 0029 — Personal/Society Storage Split](0029-personal-society-storage-split.md) — protection levels must survive the migration to the society store (§I).
- [RFC 0009 — Agent Identity, Security & Sandboxing](0009-security-sandboxing.md) — the audit subsystem.
- [Architecture spec](../ai-agents-orchestration-spec.md), [Extension spec](../persatrix-extension-spec.md).
