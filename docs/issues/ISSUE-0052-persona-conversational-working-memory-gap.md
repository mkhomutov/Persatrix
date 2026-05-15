---
id: ISSUE-0052
summary: Persona LLM call sees only the current message — no in-progress conversation transcript, breaking referential follow-ups and self-reference within a session
status: open
severity: critical
area: persona
created: 2026-05-15
refs:
  - docs/rfcs/0034-persona-conversational-working-memory.md
  - agents/persona_runtime/action_loop.py
  - agents/persona_runtime/memory_context.py
  - agents/channel_catchup.py
---

## Summary

A persona loses all track of the conversation it is currently having. It
cannot recall its own previous question, cannot resolve referential
follow-ups (`"I like it"`), and treats every turn as the first turn of
the session. Self-contained statements still survive (the fact tier
extracts them turn-locally), but anything that depends on conversational
continuity does not.

> **Terminology.** This issue's title uses "conversational working memory"
> as the human-readable description of the defect; the canonical project
> term for the live in-channel transcript surfaced through the LLM
> `messages` array is **Conversation Window**
> ([glossary](../ai-glossary.md#conversation-window)). The body below uses
> the canonical term for the runtime concept.

## Context

Reproduced repeatedly against a long-running `ember-owl` persona over a
DM channel. The original captured trace (full content now promoted into
this issue and into
[RFC 0034](../rfcs/0034-persona-conversational-working-memory.md))
walks through the symptom set:

| Observed symptom | Confirmed cause |
|---|---|
| `"what was your question before?"` → "this appears to be the start of our conversation" | The model never receives its own prior turn. |
| `"I like it"` / `"I do"` never stored as a preference | Referential fragment — the referent (`"coffee"`) was in the persona's own previous question, which it cannot see, so it cannot resolve `"it"` or extract a fact. |
| Session 1's hiking / dog / married / child *did* persist | Self-contained statement → fact tier extracts turn-locally → stored → recalled later. |
| `"when did we last interact?"` → "I don't track that" | No tier renders a `last seen` line; a short test session's episode often never closes/summarizes, so nothing surfaces. |

### Confirmed root cause

Every LLM call for an inbound message is built in
[`agents/persona_runtime/action_loop.py`](../../agents/persona_runtime/action_loop.py)
around lines 402–404:

```python
# 2. Multi-turn tool-use loop (user_message already computed above)
messages: list[dict[str, Any]] = [
    {"role": "user", "content": user_message},
]
```

`user_message` is the single current event, formatted by
`_format_event`. The loop below this line appends only tool-call /
tool-result rounds for *this one event* — it never carries prior
conversation turns. When the next inbound message arrives, the array is
built fresh again.

Everything else the persona "knows" enters via `_inject_memory_context`
([`memory_context.py`](../../agents/persona_runtime/memory_context.py))
as **system-prompt sections**:

- relationship summary
- channel-history → **episode summaries**, not raw turns
  ([`channel_history.py`](../../agents/persona_runtime/channel_history.py))
- extracted facts
- episodic recall → **episode summaries**
- agent-authored notes

All of these are long-term, recall-based tiers. Episode summaries are
written when an episode *closes* and is summarized; an in-progress
conversation has no usable summary yet. Mid-conversation, the model
receives nothing but the current line.

The conversation data is **not lost** — the DM channel persists every
message server-side, and `GET /api/v1/channels/{id}/messages?limit=N`
reads it back.
[`agents/channel_catchup.py`](../../agents/channel_catchup.py) already
calls that endpoint, but only at process boot and only to replay into
episodic memory; it never seeds the live prompt.

## Impact

**Severity: critical.** The persona experience is the v0.2 / v0.3
user-facing story. With this defect:

- Multi-turn conversations are unusable beyond a single self-contained
  statement.
- Every public demo of "talk to your persona" misrepresents what the
  product does — within two turns the persona contradicts the user's
  expectation.
- The fact tier (RFC 0026, currently being implemented for v0.3.1)
  ships into a runtime where its referential extraction path cannot
  see the referent, so RFC 0026's success metric ([MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md))
  silently undercounts.
- Group channels (RFC 0011) have the same defect — every persona in
  the channel responds with no awareness of prior turns.

This issue is the dominant remaining failure mode for the v0.3.1
"memory works" story and must ship in v0.3.1.

## Proposed fix / investigation path

Tracked under [RFC 0034 — Persona Conversational Working Memory](../rfcs/0034-persona-conversational-working-memory.md)
(target v0.3.1). Summary: reconstruct the LLM `messages` array each
turn from the channel store, mapping peer messages to `role="user"`
and the persona's own messages to `role="assistant"`, with the current
event as the last entry. The per-RFC PR plan and the v0.3.1 master
plan amendment land in the same PR that opens this issue.

### Adjacent findings worth confirming during RFC 0034 implementation

Spotted while tracing the bug; out of scope for the report but
called out so RFC 0034's PRs do not have to rediscover them:

1. **Fact extraction is also single-message.**
   [`fact_extractor.py`](../../agents/persona_runtime/fact_extractor.py)
   sees only the current message. Even after the responding model gets
   conversation history, the *extraction* pass may still fail to turn
   `"I like it"` into `(subject=Max, predicate=likes, object=coffee)`
   unless it too receives conversational context. Likely an
   RFC 0026 follow-up rather than blocking RFC 0034.
2. **Episode close / summarization timing.** Episode summaries are
   only recallable after an episode closes. Check
   [`summarize_close.py`](../../agents/persona_runtime/summarize_close.py)
   and `interaction_janitor.py` — a short session that never closes is
   never summarized and never recalled, contributing to the
   `"last interaction"` failure.
3. **`chat_session_id` lifecycle vs. the DM channel.** The Rust REPL
   holds `session_id` only within one process; each
   `bin/persatrix chat` invocation starts empty and the server mints
   a new `chat_session_id`. The DM channel
   (`dm:<agent>:<user>`) is stable across all of them. Decide whether
   "the conversation" is per-session or per-channel — the answer
   drives how much raw history to replay (an [RFC 0034 Open Question](../rfcs/0034-persona-conversational-working-memory.md#open-questions)).
4. **Agent's own outbound replies in channel history.** Confirm the
   persona's replies are persisted to the channel with
   `sender_id == agent_id` and are returned by the history endpoint —
   the fix needs them to label `assistant` turns correctly.
   ([`channel_handlers.go`](../../internal/server/channel_handlers.go)
   confirms `sender_id` is required and round-tripped.)
5. **Catch-up replay duplicates on restart.**
   `channel_catchup.py` documents at-most-once, no watermark, no
   dedup: K restarts within the catch-up window produce K×N ingested
   turns and inflate `turn_count`. Pre-existing and documented;
   relevant if episodic memory is leaned on more heavily after RFC
   0034 ships.
6. **Memory-budget contention.** If raw recent turns are also added
   to the system prompt as a hedge, they compete with summarized
   episodes for the token budget (`MemoryBudget`, fixed tier
   priorities). RFC 0034 §Design avoids this by routing raw turns
   through the `messages` array, *not* the system prompt — but if
   it ever needs a fallback, raw recent turns must outrank older
   summarized episodes.
7. **Group channels share the same defect.** Any fix should also
   cover group channels, where `user` / `assistant` role mapping is
   harder (multiple participants).
8. **Prompt-injection boundary for replayed turns.** Only the current
   event passes through `_format_event`'s `<|user_message|>` delimiter
   sanitization. Historical turns pulled raw from the channel store
   must get the same treatment before entering the prompt.
9. **Recency-fence in channel-history recall.**
   `channel_history.py` already documents that an agent active in
   many channels can have its recency window dominated by other
   channels and admit fewer same-channel episodes than exist.
   Pre-existing limitation worth revisiting under RFC 0034.
10. **Possibly-abandoned working-memory turn buffer.**
    [`agents/memory/working.py`](../../agents/memory/working.py) is
    named "working memory" but appears to be used only as a container
    for injected recall sections. May be the natural home for the
    fix — RFC 0034 §Design picks the canonical location.

## Notes

> 2026-05-15 — initial capture during persona regression session against
> `ember-owl`. Promoted from a top-level scratch file
> (`conversational-memory-gap.md`) into this issue + RFC 0034 in the
> same PR that opens this entry.
