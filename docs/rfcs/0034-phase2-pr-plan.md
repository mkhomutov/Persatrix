# RFC 0034 Phase 2 — Group-Channel Working Memory — PR Implementation Plan (v0.3.7 scope)

**RFC**: [0034-persona-conversational-working-memory.md](0034-persona-conversational-working-memory.md) (Phase 2 — [§C role mapping](0034-persona-conversational-working-memory.md#c-role-mapping) / [§G group-channel handling](0034-persona-conversational-working-memory.md#g-group-channel-handling))
**Phase 1 plan**: [0034-pr-plan.md](0034-pr-plan.md) (DM channels — shipped v0.3.1; [§Future Phases](0034-pr-plan.md#future-phases) holds the two carry-forwards this plan discharges)
**Created**: 2026-06-04
**Branch prefix**: `feature/v037-rfc0034p2-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.7-plan.md](../v0.3.7-plan.md) (Workstream 1a — the critical path; supplies the in-round transcript the relevance gate and the peer-voice prompt read)

---

## Overview

Phase 1 ([v0.3.1](../v0.3.1-plan.md)) shipped the Conversation Window for **DM channels**: every persona turn reconstructs the LLM `messages` array from the channel store ([`agents/persona_runtime/conversation_window.py`](../../agents/persona_runtime/conversation_window.py)), mapping the persona's own prior turns to `assistant` and the single peer to `user`, sanitized through the same `<|user_message|>` delimiter escape the live event gets.

Phase 2 makes that window read correctly on a **multi-peer group channel** so a persona sees *who said what* this round and can build on it — the substrate every other v0.3.7 workstream stands on ([master plan §Acceptance](../v0.3.7-plan.md#acceptance-for-v037)).

**What Phase 1 already gives us (verified against current code, 2026-06-04):**

- **Multi-peer role mapping already works.** [`_assemble_replayed_turns`](../../agents/persona_runtime/conversation_window.py) maps *any* `sender_id != agent_id` to `role="user"` and `sender_id == agent_id` to `role="assistant"`. There is no DM-only assumption in the role split — N distinct peers already collapse to `user` turns correctly. Phase 2 does **not** "extend the role mapper"; the split is already general.
- **Sanitization already inherited.** [`_format_peer_turn`](../../agents/persona_runtime/conversation_window.py) routes every replayed peer turn through `_format_event`, so the §D delimiter escape applies by construction regardless of peer count.

**What Phase 2 must still do — the two real deltas:**

1. **The inline `[<peer_id>]: ` prefix is missing.** Today a replayed peer turn carries its speaker identity *only* in the `<|user_message user_id="<peer>"|>` wrapper attribute emitted by `_format_event`. RFC [§C](0034-persona-conversational-working-memory.md#c-role-mapping) is explicit that the speaker identifier must ride **inline in the content as `[<peer_id>]: `** (it "performs better than relying on `name`"), and the [master-plan acceptance](../v0.3.7-plan.md#acceptance-for-v037) names "**every peer message prefixed `[<peer_id>]: `**" as the gate. With several distinct peers in the window, the wrapper attribute alone is weak disambiguation; the inline prefix is what lets the model resolve "*the other peer's* prior turn." **PR 1.**
2. **The fetch cache is multi-persona-unsafe.** [§Future Phases carry-forward](0034-pr-plan.md#future-phases): `_WINDOW_CACHE` keys on `channel_id` alone and stores the *first* caller's `max_turns + 1` fetch limit. That is sound only because a DM has exactly one persona/config. On a group channel a small-`max_turns` persona can populate the cache and serve an **undersized** window to a large-`max_turns` peer reacting to the same inbound message. Key on the fetch limit (or bypass for the multi-persona case). **PR 2.**

PR 3 is the closeout: the cross-peer integration gate, the group-channel manual test, the doc/guide naming, and RFC/ROADMAP status hygiene.

**Out of scope (carry-forward, not this plan):** the unbounded `_WINDOW_CACHE` (no eviction) is a *capacity* concern deferred to **Phase 3** (LRU bound, sized from telemetry) per [§Future Phases](0034-pr-plan.md#future-phases) — distinct from the multi-persona *correctness* bug PR 2 fixes. Instrumentation/tuning of `max_turns`/`max_tokens` is also Phase 3.

**Prerequisite**: RFC 0034 Phase 1 (✅ v0.3.1). Reuses the `ChannelHistoryFetcher` Protocol, the `_format_event` delimiter escape, and the `_apply_admission` / `_drop_leading_assistant_turns` admission pipeline — all shipped.

---

## Sequencing

**Merge order: PR 1 → PR 2 → PR 3.**

- **PR 1** is the user-visible disambiguation change (inline prefix), fully unit-tested, plus the cross-peer integration test that is its acceptance gate.
- **PR 2** is an independent correctness fix (cache key); it has no dependency on PR 1 and could merge either side of it, but is sequenced second so PR 1's integration test is green on a clean base first.
- **PR 3** is closeout: manual test + docs + status. It depends on PR 1 + PR 2 both landing the behaviour.

Every PR is **TDD-first**: author the failing test (red) that pins the new contract, then implement to green. The test files are named per PR below.

---

## Dependency Graph

```
PR 1 (inline [<peer_id>]: prefix at the sanitization step; cross-peer integration test)
  ↓
PR 2 (multi-persona cache-key fix: key on fetch limit / bypass; regression test)
  ↓
PR 3 (group-channel manual test; docs/guides naming; RFC §G + ROADMAP status hygiene)
```

PR 1 changes replayed-turn rendering (visible in the prompt); PR 2 is a latent-correctness fix with no surface change; PR 3 carries no code, only the MT + docs + status.

---

## PR Sequence

### PR 1: `feature/v037-rfc0034p2-peer-prefix` — Inline `[<peer_id>]: ` prefix

**Depends on**: Phase 1 baseline.
**Purpose**: Give each replayed peer turn an inline speaker label so a persona can attribute and build on a *specific* peer's contribution — the §C/§G contract and the master-plan acceptance gate.

| File | Change |
|------|--------|
| [`agents/persona_runtime/conversation_window.py`](../../agents/persona_runtime/conversation_window.py) | In `_format_peer_turn` (the replayed-peer path), prepend `[<peer_id>]: ` to the peer message **content before** it is handed to `_format_event`, so the delimiter escape is applied to the combined string by construction (no escape duplicated here, per §D). The persona's own turns (`role="assistant"`) stay **unprefixed**. `peer_id` is the row's server-enforced `sender_id` (schema-constrained `^[A-Za-z0-9][A-Za-z0-9_-]*$`); a missing/non-`str` `sender_id` falls back to the existing `unknown` sender rather than emitting an empty `[]: `. |
| `tests/unit/python/persona_runtime/test_conversation_window.py` | **(TDD — write first.)** Assert: (a) a replayed peer turn's `content` contains `[<peer_id>]: ` ahead of the message body, inside the `<|user_message|>` wrapper; (b) two distinct peers in one window each carry their **own** id; (c) the persona's own replayed turn is `assistant` and carries **no** prefix; (d) a peer message containing literal `<|`/`|>` is still delimiter-escaped *and* prefixed (prefix composes with the §D escape); (e) the DM single-peer case is unchanged in shape (one peer, prefixed). |

**Decision locked**: the prefix applies to **replayed** peer turns only (the window this module owns). The *current* inbound turn is appended by the caller (`action_loop.py`) and already carries speaker identity via the `user_id="<sender>"` wrapper attribute set in `_format_event`; re-prefixing it is out of scope here (no module boundary crossed, and the current speaker is unambiguous — it is the message being answered).

**Acceptance**: `make test` (Python unit lane) green; the new unit assertions hold; existing DM-channel conversation-window tests pass unchanged.

---

### PR 2: `feature/v037-rfc0034p2-cache-key` — Multi-persona fetch-cache correctness

**Depends on**: PR 1 (sequencing only; no code dependency).
**Purpose**: Discharge the [§Future Phases carry-forward](0034-pr-plan.md#future-phases) — prevent a small-`max_turns` persona from serving an undersized cached window to a large-`max_turns` peer on the same channel/message.

| File | Change |
|------|--------|
| [`agents/persona_runtime/conversation_window.py`](../../agents/persona_runtime/conversation_window.py) | Re-key `_WINDOW_CACHE` so a stored window is reused only when the **fetch limit matches**: key on `(channel_id, limit)` (the cached value stays `(message_id, raw_rows)`), or equivalently store the limit alongside the entry and treat a limit mismatch as a miss → refetch. Update the in-module cache-rationale comment (currently documents the DM-only soundness assumption) to record the Phase 2 fix. The Phase-3 *eviction/LRU* gap stays documented and deferred. |
| `tests/unit/python/persona_runtime/test_conversation_window.py` | **(TDD — write first.)** Regression: prime the cache for `(channel_id, message_id)` via a persona with small `max_turns` (small `limit`); a second `build_conversation_messages` on the **same** `(channel_id, message_id)` with a **larger** `max_turns` must **not** be served the undersized cached rows — assert it refetches at the larger limit and the returned window is full-size. Same-limit back-to-back call still hits the cache (no regression to the §F "skip the fetch when no new message arrived" optimization). |

**Acceptance**: `make test` green; the undersized-serve regression fails before the fix and passes after; the same-limit cache-hit path is preserved.

---

### PR 3: `feature/v037-rfc0034p2-closeout` — Group-channel manual test + docs + status

**Depends on**: PR 1 + PR 2.
**Purpose**: The operator-facing surface and the acceptance record.

| File | Change |
|------|--------|
| `tests/integration/persona/test_conversational_continuity.py` | The §G group-channel integration gate (authored in PR 1, re-asserted here as the release gate): two peers + one persona; the persona resolves a pronoun referring to the **other peer's** prior turn (e.g. peer A states a fact, peer B asks "does that work for you?", the persona's reply must bind "that" to A's statement). If authored in PR 1, this row is a no-op confirmation. |
| `docs/manual-tests/MT-PERSONA-CONVERSATION-002.md` (final id at authoring) | **New.** Group-channel continuity MT: three personas + a user on one group channel; a turn that only resolves if the persona saw a *named* peer's prior contribution. Records the realism outcome the master plan's combined MT execution re-runs live. |
| [`docs/guides/persona-agents.md`](../guides/persona-agents.md) | Note that the Conversation Window now reconstructs group-channel transcripts with per-peer `[<peer_id>]: ` attribution (Phase 1 was DM-only). |
| [`0034-persona-conversational-working-memory.md`](0034-persona-conversational-working-memory.md) | Status hygiene: §G / Phased-Implementation-Plan Phase 2 marked implemented; RFC stays `⚠️ Partially Implemented` (Phase 3 instrumentation/tuning + LRU remain). `make rfcs` re-run if front-matter changes. |
| ROADMAP / CHANGELOG | Seed the `[0.3.7]` entry line for group working memory; ROADMAP RFC-index note that RFC 0034 Phase 2 shipped. Per [master-plan §ROADMAP hygiene](../v0.3.7-plan.md#roadmap-hygiene) RFC 0034 stays `⚠️ Partially Implemented`. |

**Acceptance**: a fresh `--enable-ui` run on a multi-persona group channel shows replayed peer turns rendered as `[<peer_id>]: …` in the assembled prompt; the cross-peer pronoun integration test is green on HEAD; `MT-PERSONA-CONVERSATION-002` recorded.

---

## Test Strategy (summary)

- **Unit (PR 1)**: inline `[<peer_id>]: ` present on replayed peer turns, distinct per peer, absent on the persona's own `assistant` turns, composes with the §D delimiter escape; DM single-peer shape unchanged.
- **Unit/regression (PR 2)**: a smaller-`limit` cache entry is not served to a larger-`limit` caller on the same `(channel_id, message_id)`; same-limit cache hit preserved.
- **Integration (PR 1, gated in PR 3)**: two peers + one persona; cross-peer pronoun resolution.
- **Manual (PR 3)**: `MT-PERSONA-CONVERSATION-002` — group-channel continuity with named-peer attribution.
- **Regression**: the full Phase-1 DM conversation-window suite passes unchanged on every PR (backward-compatibility gate).

---

## Status & ROADMAP hygiene

Per [master-plan §ROADMAP hygiene](../v0.3.7-plan.md#roadmap-hygiene):

- **PR 1–2 open** → no RFC status change (RFC 0034 already `⚠️ Partially Implemented`; companion PR plans are excluded from `INDEX.md`).
- **PR 3 merges** → RFC 0034 §G / Phase 2 marked implemented in the RFC body; RFC **stays `⚠️ Partially Implemented`** (Phase 3 remains); CHANGELOG `[0.3.7]` group-working-memory line seeded; `Last updated` refresh; `make rfcs` re-run if front-matter changed.
- **v0.3.7 tag** → group working memory is part of the realism release contract; the combined realism MT (master-plan Phase 3) re-runs the cross-peer continuity leg live on HEAD.

---

## Related documentation

- [RFC 0034 — Persona Conversational Working Memory](0034-persona-conversational-working-memory.md) — §C/§G is the contract this plan implements; §F is the cache framing PR 2 refines.
- [0034-pr-plan.md §Future Phases](0034-pr-plan.md#future-phases) — the two carry-forwards (multi-persona cache key → PR 2 here; LRU eviction → Phase 3).
- [v0.3.7 plan](../v0.3.7-plan.md) — the release this lands in; Workstream 1a (critical path).
- [RFC 0030 relevance-gated-response amendment](0030-amendment-relevance-gated-response.md) + [its PR plan](0030-amendment-relevance-gated-response-pr-plan.md) — the addressing workstream that reads this transcript; Tier B (v0.3.8) hard-depends on the in-round transcript this plan supplies.
- [`agents/persona_runtime/conversation_window.py`](../../agents/persona_runtime/conversation_window.py), [`prompt_assembly.py`](../../agents/persona_runtime/prompt_assembly.py) — the code this plan touches and reuses.
