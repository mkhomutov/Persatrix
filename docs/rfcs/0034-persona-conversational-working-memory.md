---
id: RFC-0034
title: Persona Conversational Working Memory
summary: Reconstruct the LLM `messages` array from the channel store on every persona turn so the model sees the in-progress conversation as a transcript instead of a single isolated message; closes the "persona forgets its own previous question" defect captured in ISSUE-0052.
type: architecture
status: proposed
author: Maksim Khomutov
created: 2026-05-15
target: v0.3.1
depends_on:
  - RFC-0011
  - RFC-0017
  - RFC-0020
  - RFC-0031
---

# RFC 0034 — Persona Conversational Working Memory

**Type**: architecture
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-05-15
**Target**: v0.3.1
**Depends on**: RFC 0011 (Channels — provides the persistent message store and `GET /channels/{id}/messages` history endpoint), RFC 0017 (Memory Injection Token Budget — defines the budget surface this RFC must coexist with), RFC 0020 (Interaction Lifecycle — defines the episode/interaction boundary the transcript window aligns with), RFC 0031 Phase 1 (Per-Session Namespacing — provides the `chat_session_id` / `persatrix_session_id` columns this RFC filters on)
**Relates to**: RFC 0026 (Declarative Facts Tier — fact extraction will inherit the same conversational context surface as a follow-up), RFC 0030 (Multi-Agent Conversation Governance — group-channel role-mapping problem space)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Where the fix lives](#a-where-the-fix-lives)
  - [B. Transcript window definition](#b-transcript-window-definition)
  - [C. Role mapping](#c-role-mapping)
  - [D. Sanitization of replayed turns](#d-sanitization-of-replayed-turns)
  - [E. Token-budget interaction](#e-token-budget-interaction)
  - [F. Caching and fetch policy](#f-caching-and-fetch-policy)
  - [G. Group-channel handling](#g-group-channel-handling)
  - [H. Replay-mode interaction](#h-replay-mode-interaction)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Today the persona runtime calls the LLM with a `messages` array that is
**rebuilt from scratch every turn and contains only the current user
message** ([`action_loop.py:402-404`](../../agents/persona_runtime/action_loop.py)).
There is no short-term/working conversational memory in the model's
context. Long-term memory tiers (relationship summary, channel-history
*episode summaries*, extracted facts, episodic recall, agent notes)
inject into the system prompt — but they are **summarization tiers**,
written when an episode closes; an in-progress conversation has no
usable summary yet. The result: mid-conversation, the model receives
nothing but the current line, and treats every turn as the first turn.

This RFC proposes that the persona runtime **reconstruct the `messages`
array each turn from the channel store** before the LLM call:
fetch the last N messages of the current `event.channel_id`, map peer
messages to `{"role": "user"}` and the persona's own messages to
`{"role": "assistant"}`, sanitize each replayed turn through the same
delimiter-wrapping sanitization the current event already gets, and
append the current event last. Long-term memory tiers stay in the
system prompt; raw recent turns ride the conversation channel where
they belong semantically.

ISSUE-0052 is the operational report; this RFC is the proper fix that
closes it.

> **Terminology.** This RFC's title uses "Conversational Working Memory"
> as the human-readable description of the defect class, but the
> canonical project term for the live in-channel transcript surfaced
> through the LLM `messages` array is **Conversation Window**
> ([glossary](../ai-glossary.md#conversation-window)). The remainder of
> this document and the per-RFC PR plan use "Conversation Window" for
> the runtime concept. The phrase "working memory" elsewhere in the
> codebase refers to the in-RAM bridged memory tier
> ([Memory](../ai-glossary.md#memory) /
> [Scratchpad](../ai-glossary.md#scratchpad-memory-tier)) and is
> deliberately kept out of the runtime module name
> (`conversation_window.py`) to avoid the collision.

## Motivation

### The defect

| Observed symptom | Confirmed cause |
|---|---|
| `"what was your question before?"` → "this appears to be the start of our conversation" | The model never receives its own prior turn; `messages` holds only the current message. |
| `"I like it"` / `"I do"` never stored as a preference | Referential fragment — the referent (`"coffee"`) was in the persona's own previous question, which it cannot see, so it cannot resolve `"it"` or extract a fact. |
| Self-contained statements *do* persist | Single-message-extractable; survives because the fact tier does not need conversational context. |
| `"when did we last interact?"` → "I don't track that" | No tier renders a `last seen` line; short test sessions often never close, so episode summaries never surface. |

The mix of *"remembers some things, forgets others"* is fully explained
by this single root cause: **self-contained statements survive the
fact tier; anything that depends on conversational continuity does
not.** Full diagnosis in
[ISSUE-0052](../issues/ISSUE-0052-persona-conversational-working-memory-gap.md).

### Why long-term memory is the wrong vehicle

The instinct on first encountering this defect is to make the channel-
history tier carry raw turns instead of episode summaries. That is
the wrong fix:

- The system prompt is for *instructions and persistent context*, not
  dialogue. A transcript inside the system prompt is read by the model
  as instructions ("the user previously said X, now do Y") rather than
  as a conversation. Quality-of-output on every existing benchmark for
  multi-turn `messages` formats is materially better than the
  equivalent "transcript-in-system-prompt" form.
- Raw turns would compete for the system-prompt token budget with
  episode summaries, facts, and relationship state. The token budget
  is finite; the long-term tiers are exactly what gets evicted to
  make room for raw turns, which is the opposite of what we want.
- Two separate paths (live transcript via `messages`, summarized
  history via the system prompt) is the canonical LLM-API split.
  Conflating them is a category error.

### Why a per-process buffer is also the wrong fix

A `chat_session_id`-keyed deque in the persona runtime would work
mid-process but:

- Duplicates the durable channel store that already persists every
  message.
- Dies on restart — the catch-up path (`channel_catchup.py`) ingests
  durable history into episodic memory but cannot rehydrate a buffer
  it does not own.
- Does not help group channels where multiple personas need a
  consistent view.

The channel store is the source of truth. The fix reads from it.

## Goals

1. The persona's LLM call carries a `messages` array containing the
   last N turns of the current channel, in chronological order, with
   peer messages mapped to `role="user"` and the persona's own messages
   to `role="assistant"`.
2. Within a live conversation, the persona can answer
   `"what did you just ask?"` and resolve referential follow-ups
   (`"I like it"` → fact extracted with the prior question's referent).
3. The fix works for both DM channels and group channels (group-channel
   role mapping is defined in §G).
4. Replayed turns are sanitized identically to the current event before
   entering the prompt — no new prompt-injection surface.
5. The transcript window has a defined, configurable upper bound that
   coexists with the existing memory token budget without starving
   long-term tiers.
6. Long-term memory tiers (RFC 0017 budget surface) are unchanged.
   No tier is renamed, removed, or has its priority order shifted.
7. [MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md)
   Legs that depend on conversational continuity (the referential
   follow-up legs called out in [ISSUE-0052 Impact](../issues/ISSUE-0052-persona-conversational-working-memory-gap.md#impact))
   flip green once this lands alongside RFC 0026.

## Non-Goals

- **Cross-session conversational continuity.** RFC 0034 ships
  per-channel transcript reconstruction. Whether a different
  `chat_session_id` on the same channel sees the prior session's
  raw turns is an Open Question (see §Open Questions #1) — the
  default proposed below is "yes, last N regardless of session,
  filtered by channel". Per-session isolation of *recall* is
  RFC 0031 Phase 2, separately tracked.
- **Fact-extractor conversational context.** Giving
  `fact_extractor.py` the same transcript surface so referential
  facts (`"I like it"` → `(Max, likes, coffee)`) become
  extractable is a follow-up, expected to ride RFC 0026's PR plan
  or a v0.3.2 amendment. RFC 0034 is the substrate; RFC 0026
  consumes it.
- **Episode-close timing fixes.** ISSUE-0052 §Adjacent #2 calls out
  short-session episodes never closing/summarizing. That is an
  RFC 0020 follow-up; out of scope here.
- **Catch-up dedup / watermarks.** ISSUE-0052 §Adjacent #5 — pre-
  existing, owned by a future RFC 0011 amendment.
- **Group-channel governance.** Multi-persona reply-budget /
  termination semantics are RFC 0030. RFC 0034 only defines the
  role mapping for prompt assembly.
- **Recency-fence rework on the channel-history *summary* tier.**
  ISSUE-0052 §Adjacent #9; orthogonal.

## Design / Implementation

### A. Where the fix lives

A new module `agents/persona_runtime/conversation_window.py` owns the
transcript-window reconstruction. The single entry point:

```python
async def build_conversation_messages(
    *,
    event: AgentEvent,
    agent_id: str,
    history_fetcher: ChannelHistoryFetcher,
    current_user_message: str,
    config: ConversationWindowConfig,
) -> list[dict[str, Any]]:
    """Return the LLM messages array seeded with channel transcript.

    The last element is always the current event wrapped through the
    same `<|user_message|>` delimiter sanitization used today; every
    earlier element is a sanitized replayed turn.
    """
```

`_on_event_inner` in `action_loop.py` calls this **immediately before**
the existing `messages: list[dict[str, Any]] = [...]` line and uses
the returned list in place of the single-element seed. The tool-use
loop below is unchanged: it appends tool-call / tool-result rounds to
`messages` exactly as today.

`ChannelHistoryFetcher` is a Protocol. The production binding is
factored out of `agents/channel_catchup.py::_fetch_channel_history`
(today a private helper) into a small, reusable module
`agents/channel_history_fetcher.py` that both catch-up and this RFC
import. No behaviour change for catch-up; it keeps using the same
implementation through the same Protocol.

### B. Transcript window definition

The window is **last N messages of `event.channel_id`**, where:

- `N` is `conversation_window.max_turns`, default **20**, bounded by
  `config["max_tokens"]`-aware truncation (see §E).
- The window is **per-channel**, not per-session. The persona's
  durable identity in a channel is the channel itself (RFC 0011);
  a new `chat_session_id` is a CLI-process-lifetime concept, not a
  conversation boundary.
- The current event is **excluded** from the fetched window and
  appended as the last entry after sanitization, so we never
  duplicate the in-flight message if it has already landed in the
  channel store.

Open Questions §1 records the per-session-vs-per-channel decision
and the migration path if v0.3.2 wants to flip the default.

### C. Role mapping

| Source `sender_id` | Mapped role |
|---|---|
| `event` recipient agent's own `agent_id` | `assistant` |
| Any other sender | `user` |

For group channels (multiple peers), all peer messages map to `user`.
A `name` field is **not** added to peer turns — the LLM API surface
varies on whether `messages[i].name` is honored, and stuffing a peer
identifier into the content with a `[<peer_id>]:` prefix performs
better than relying on `name`. The prefix is added at the same
sanitization step as the delimiter wrapping (§D).

### D. Sanitization of replayed turns

Every replayed turn passes through the same `_format_event`
`<|user_message|>` / `<|assistant_message|>` delimiter wrapping the
current event already gets. This closes the prompt-injection vector
ISSUE-0052 §Adjacent #8 calls out: a peer message containing
`<|user_message|>...<|/user_message|>` literals cannot smuggle
synthetic prior turns past the wrapper. The wrapper escapes the
literal in replayed content before wrapping, identical to today's
single-event path.

**Verified against current code (2026-05-15).** The escape lives at
[`agents/persona_runtime/prompt_assembly.py` lines 355–362](../../agents/persona_runtime/prompt_assembly.py#L355-L362)
inside the `EventType.CHANNEL_MESSAGE` branch of `_format_event`:
`safe_content = content.replace("<|", "\\<|").replace("|>", "\\|>")` runs
before the `<|user_message ...|> ... <|/user_message|>` wrapping. Phase 1
must call `_format_event` per replayed turn (not duplicate the wrapping
logic) so this escape is inherited by construction; the unit test in
§Test Strategy ("a peer message containing `<|user_message|>` literal is
escaped before wrapping") asserts the round-trip on replayed content.

### E. Token-budget interaction

The transcript window has its own budget separate from the system-
prompt memory budget:

- `conversation_window.max_tokens`, default **2048**, deducted from
  the model's `max_tokens` *before* the system-prompt memory budget
  computes its allocation.
- If the last N turns exceed the transcript budget, oldest turns are
  dropped first (FIFO). The current event is never dropped.
- The system-prompt memory budget is unchanged. Long-term tiers
  (relationship, channel-history summary, facts, episodic recall,
  notes) keep their existing priorities and totals.
- The split — transcript on the `messages` channel, summaries on the
  system channel — is what makes this safe. Both channels can be
  full without one starving the other.

### F. Caching and fetch policy

Per-turn fetches dominate the cost. Mitigations:

- **In-process cache**, keyed by `(channel_id, last_known_message_id)`.
  The dispatcher hands the runtime the new message id with the event;
  if the cache key matches, no fetch is issued and the cached window
  is reused with the new event appended.
- **Cache invalidation** is the cheapest possible: any `on_event` with
  a `CHANNEL_MESSAGE` whose `message_id` is not the cached "last
  known" triggers a refetch, then updates the cache.
- The fetcher uses the same `aiohttp` session and 10s timeout the
  catch-up path uses. On fetch failure, the runtime degrades
  gracefully to "current event only" (today's behaviour) and logs
  a WARNING with `reason="conversation_window_fetch_failed"`. The
  persona is no worse off than it is today.

> **Known gap — cache hit rate in steady state.** As specified above the
> cache key advances with every inbound event (`last_known_message_id`
> moves on each turn), so back-to-back events on the same channel each
> compute a *new* key and miss. The optimization the cache buys is
> therefore "skip the fetch when no new message arrived between two
> wake-ups of the same persona on the same channel" (e.g. retries,
> sub-agent return paths) — *not* steady-state turn-over-turn
> short-circuiting. The Phase 1 PR plan must either (a) accept this
> framing and document the expected steady-state hit rate as low, or
> (b) re-spec the cache to short-circuit *window assembly* on
> `(channel_id, last_message_id_in_returned_window)` while still
> issuing the fetch, separating "did the channel change?" from "do I
> have to re-render the window?". Phase 3 telemetry
> (`persatrix.persona.conversation_window.cache_hit_rate`) is the
> arbiter; the default is (a) until measurement justifies (b).

A measurement harness is part of [Phase 3](#phase-3-instrumentation-and-tuning):
the cache-hit rate, fetch latency, and the share of LLM calls that
fall back to "current event only" are exposed as metrics so the
default `N=20` and `max_tokens=2048` can be re-tuned with data.

### G. Group-channel handling

Group channels (RFC 0011) use the same Protocol and the same role
mapping (§C). Per-peer prefixing (`[<peer_id>]: ...`) lets the model
disambiguate speakers without adding a non-standard `name` field.
RFC 0030 (multi-agent conversation governance) builds on top of this
substrate; nothing in §G blocks it.

### H. Replay-mode interaction

`channel_catchup.py` events carry `metadata["replay_mode"] = True`
and short-circuit before reaching `_on_event_inner`'s LLM call. RFC
0034 does **not** change replay-mode semantics — the conversation
window is reconstructed only on live, non-replay `CHANNEL_MESSAGE`
events. Catch-up continues to seed episodic memory; the conversation
window seeds the live prompt. The two paths remain independent.

**Verified against current code (2026-05-15).** The short-circuit is at
[`agents/persona_runtime/action_loop.py` lines 280–289](../../agents/persona_runtime/action_loop.py#L280-L289):
on `event.metadata.get("replay_mode") is True` the handler stores the
event into episodic memory (when `sender_id != agent_id`) and returns
`[AgentAction(action_type=ActionType.DO_NOTHING, ...)]` — well before
the `messages: list[dict[str, Any]] = [...]` LLM seed (~line 402) and
before any tool-use loop or LLM call. Phase 1 places the
`build_conversation_messages` call at the seed line, so it inherits
this guard by construction; no additional `replay_mode` check is
required inside `conversation_window.py`. The marker is set on the
catchup-emitted event payload at
[`agents/channel_catchup.py` line 494](../../agents/channel_catchup.py#L494).

## Security Considerations

- **Prompt injection via replayed turns** — closed by §D
  (delimiter-wrapping every replayed turn through the same
  sanitizer the current event uses today). No new attack
  surface beyond what the current event already has.
- **Cross-channel leakage** — the fetcher filters on
  `event.channel_id` only. A persona member of multiple channels
  cannot accidentally see another channel's messages because the
  history endpoint is itself channel-scoped (RFC 0011 §C, enforced
  server-side).
- **Cross-session leakage when sessions are isolated** — see
  Open Questions §1. The default proposal is per-channel
  reconstruction (no session filter), which preserves continuity
  across `bin/persatrix chat` invocations on the same channel.
  If RFC 0031 Phase 2 introduces a per-session recall mode that
  the operator opts into, the conversation window picks up the
  same `persatrix_session_id` filter at that time — non-additive
  surfaces are deferred to that RFC.
- **Token-budget DoS** — bounded by §E (`max_tokens=2048`,
  `max_turns=20`). A flood of 1KB peer messages cannot inflate
  the prompt past the transcript budget; FIFO drop applies.
- **Cache-key confusion** — the cache key includes `channel_id`,
  so two channels with overlapping `last_known_message_id` (the
  message id is globally unique today, but defence-in-depth) cannot
  cross-pollinate.

## Phased Implementation Plan

### Phase 1: Substrate + DM channels

- Factor `_fetch_channel_history` out of `channel_catchup.py` into a
  shared `agents/channel_history_fetcher.py` behind a Protocol.
- New `agents/persona_runtime/conversation_window.py` implements
  `build_conversation_messages` for DM-channel events.
- Wire into `_on_event_inner` immediately before the `messages = [...]`
  seed line.
- Sanitization (§D) reuses the current event's delimiter wrapper.
- Defaults: `max_turns=20`, `max_tokens=2048`. Configurable via
  `config/agents.yaml` per-agent and `config/optimization.yaml`
  defaults (a new `conversation_window` block, schema addition).
- Unit tests cover the empty-channel, single-prior-turn, role-mapping,
  and oversize-window cases. Integration test asserts a persona answers
  `"what did you just ask?"` correctly within one DM session.

### Phase 2: Group channels and per-peer prefixing

- Extend the role mapper to multi-peer channels (§C, §G).
- Add the `[<peer_id>]: ` prefix at the sanitization step.
- Integration test: two peers + one persona; persona resolves a
  pronoun referring to the *other* peer's prior turn.

### Phase 3: Instrumentation and tuning

- Cache-hit rate, fetch latency, fallback-to-empty-window count
  exposed as OTEL metrics
  (`persatrix.persona.conversation_window.*`).
- Re-tune defaults from a one-week telemetry sample on the
  dogfood persona.
- Document the tunables in
  [`docs/guides/persona-agents.md`](../guides/persona-agents.md).

Phases 2 and 3 are part of the v0.3.1 RFC 0034 PR plan but reviewed
independently. Phase 1 is the load-bearing acceptance gate for
v0.3.1.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | `agents/persona_runtime/conversation_window.py` (new) | Phase 1 substrate |
| Python agents | `agents/channel_history_fetcher.py` (new) | Factor out of catchup |
| Python agents | `agents/persona_runtime/action_loop.py` | Call site at the `messages = [...]` seed |
| Python agents | `agents/channel_catchup.py` | Switch to the shared fetcher |
| Python agents | `agents/persona_runtime/__init__.py` | Re-exports |
| Python agents | `agents/observability/metrics.py` | New `conversation_window.*` metrics (Phase 3) |
| Config | `config/agents.yaml`, `config/optimization.yaml` | New `conversation_window` block |
| Schemas | `schemas/agents.schema.json`, `schemas/optimization.schema.json` | New block validation |
| Docs | `docs/guides/persona-agents.md` | Phase 3 tunables |
| Docs | `docs/manual-tests/MT-MEMORY-005-*.md` | Update expected outcomes |
| Tests | `tests/unit/python/persona_runtime/test_conversation_window.py` (new) | Phase 1 unit |
| Tests | `tests/integration/persona/test_conversational_continuity.py` (new) | Phase 1 + Phase 2 integration |

## Test Strategy

- **Unit tests**:
  - `build_conversation_messages` with empty / 1-turn / N-turn / N+5-turn channels.
  - Role mapping for DM (Phase 1) and group (Phase 2) channels.
  - Sanitization: a peer message containing `<|user_message|>` literal
    is escaped before wrapping.
  - FIFO truncation at the transcript token budget.
  - Cache hit / miss / invalidation.
  - Fetch failure → fall back to current-event-only without raising.
- **Integration tests**:
  - Persona answers `"what did you just ask?"` in a DM channel within
    one session.
  - Persona resolves a pronoun referring to a peer's prior turn in a
    group channel (Phase 2).
- **Manual tests**:
  - [MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md):
    referential-follow-up legs flip green when RFC 0034 + RFC 0026
    are both merged. The MT execution doc is a v0.3.1 release-prep
    deliverable.
  - New `MT-PERSONA-CONVERSATION-001` (Phase 1 deliverable):
    minimal repro of ISSUE-0052 (`"what was your question?"` after
    a prior persona turn) ships green.

## Open Questions

> **Status (2026-05-15)**: all four open questions resolved at PR-plan
> authoring time. Resolutions are recorded inline below and cross-cited
> from [`docs/rfcs/0034-pr-plan.md` §Open-question resolutions](0034-pr-plan.md#open-question-resolutions-locked-at-plan-authoring-time).
> Phase 1 PRs may open against these resolutions.

1. **Per-session vs. per-channel transcript window.** Should the
   fetched window filter on `chat_session_id` (or `persatrix_session_id`,
   RFC 0031) or include all channel turns regardless of session?

   **Resolution (1a — per-channel, no session filter).** The window
   filters on `event.channel_id` only; rows are admitted regardless of
   `chat_session_id` or `persatrix_session_id`. Justification:
   - **Policy anchor.** [RFC 0031 PR plan §OQ #1 resolution 1a](0031-pr-plan.md#open-question-resolutions-locked-at-plan-authoring-time)
     positions Phase 1's session columns as **operational hygiene**
     (test-run isolation + future recall scoping), not a
     **prompt-content privacy boundary**. The Conversation Window
     follows the policy: no prompt-content filter is required.
   - **User-visible continuity.** Restarting `bin/persatrix chat` on a
     DM channel under a fresh `PERSATRIX_SESSION_ID` should preserve
     in-channel transcript continuity — the channel is the durable
     identity, the session is a process-lifetime tag.
   - **Forward compatibility.** If RFC 0031 Phase 2 (recall filtering)
     ever re-frames the session boundary as privacy-bearing, the
     Conversation Window picks up the same `persatrix_session_id`
     filter at that time. Phase 1 of this RFC reserves but does not
     consume the column; the change is additive, non-breaking.
   - **Test obligation.** Phase 1 unit tests cover the no-filter
     contract explicitly: two events on the same channel under
     different `persatrix_session_id` values share one window.

2. **Window size N and budget interaction.** Defaults `N=20`,
   `max_tokens=2048` are guesses. Phase 3 telemetry retunes them.

   **Resolution (2a — both bind, tighter wins per turn).** Phase 1
   ships both bounds; the per-turn admission loop applies the tighter
   constraint at each step (FIFO drop on token-overflow first; if the
   surviving turns still exceed `N`, drop oldest until count ≤ `N`).
   `N=20` and `max_tokens=2048` are the committed Phase 1 defaults.
   Retuning is a one-line constant change in Phase 3 once the
   `cache_hit_rate` / `fetch_latency` / `fallback_to_empty` metrics
   land — no schema or config-shape implication.

3. **Per-peer disambiguation in group channels.** Inline prefix
   (`[<peer_id>]: ...`) vs. `messages[i].name`.

   **Resolution (3a — inline prefix, deferred to Phase 2).** Phase 1
   ships DM channels only (one peer); the disambiguation question
   does not bind. Phase 2 implements the inline-prefix form per §C
   (no `messages[i].name`); Phase 2's group-channel integration test
   exercises the disambiguation explicitly. This RFC's role-mapping
   contract (§C) does not change between Phase 1 and Phase 2 — the
   prefix is a sanitization-step concern, not a role-mapping one.

4. **Fact extractor conversational context.** Out of scope per
   [Non-Goals](#non-goals).

   **Resolution (4a — deferred to RFC 0026 follow-up).** RFC 0034
   Phase 1 ships the substrate (`build_conversation_messages`); RFC
   0026's extractor consumes it as a follow-up tracked under that
   RFC's PR plan. This RFC closes when its Phase 1 lands; the
   RFC 0026 consumer side is not a Phase 1 acceptance gate.

## Decision / Next Steps

1. Land this RFC + the issue + the v0.3.1 plan amendment in one PR
   (the PR opening this entry).
2. Resolve [Open Question §1](#open-questions) in the RFC 0034 review
   thread. The PR plan (Phase 1 of the per-RFC PR plan, opened next)
   cannot start until this is fixed.
3. Author `docs/rfcs/0034-pr-plan.md` modeled on
   [RFC 0017 PR plan](0017-pr-plan.md) — Phase 1 fully fleshed out,
   Phases 2–3 scoped under a `## Future Phases` block.
4. Implement Phase 1 in v0.3.1, after RFC 0031 Phase 1 has merged
   (so the `chat_session_id` / `persatrix_session_id` columns are
   available if Open Question §1 flips to per-session). RFC 0034
   does not block on RFC 0026; the two land in parallel under the
   v0.3.1 umbrella.

## Related Documentation

- [ISSUE-0052 — Persona conversational working-memory gap](../issues/ISSUE-0052-persona-conversational-working-memory-gap.md) — the operational report this RFC closes.
- [v0.3.1 plan §RFC 0034 workstream](../v0.3.1-plan.md) — release sequencing.
- [RFC 0011 — Channels & Internal Agent Messaging](0011-channels-bridges.md) — provides the durable channel store and history endpoint.
- [RFC 0017 — Persona Memory Injection Token Budget](0017-persona-memory-injection-budget.md) — defines the system-prompt memory budget this RFC stays orthogonal to.
- [RFC 0020 — Interaction Lifecycle](0020-interaction-lifecycle.md) — episode/interaction boundary definitions.
- [RFC 0026 — Declarative Facts Tier](0026-declarative-facts-tier.md) — consumer of this substrate for referential fact extraction.
- [RFC 0030 — Multi-Agent Conversation Governance](0030-multi-agent-conversation-governance.md) — group-channel governance built atop the role mapping defined here.
- [RFC 0031 — Per-Session Namespacing](0031-per-session-namespacing-channels.md) — Phase 1 columns this RFC may filter on per Open Question §1.
- [Architecture spec](../ai-agents-orchestration-spec.md), [Extension spec](../persatrix-extension-spec.md).
