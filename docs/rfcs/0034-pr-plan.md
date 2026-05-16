# RFC 0034 — PR Implementation Plan (Phase 1 — v0.3.1 scope)

**RFC**: [0034-persona-conversational-working-memory.md](0034-persona-conversational-working-memory.md)
**Created**: 2026-05-15
**Branch prefix**: `feature/v031-rfc0034p1-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.1-plan.md Phase 1 (RFC 0034 PR plan PR — row 1b)](../v0.3.1-plan.md#phase-1--author-the-three-rfc-pr-plans)

---

## Overview

RFC 0034 reconstructs the LLM `messages` array from the channel store on every persona turn so the model sees the in-progress conversation as a transcript instead of a single isolated message — closing the "persona forgets its own previous question" defect captured in [ISSUE-0052](../issues/ISSUE-0052-persona-conversational-working-memory-gap.md). The RFC spans three phases; **only Phase 1 lands in v0.3.1** (DM channels — substrate, sanitization, token-bounded window, in-process cache, fall-back). Phases 2–3 (group-channel role mapping, instrumentation/tuning) are reserved for v0.3.x patches — see [§Future Phases](#future-phases).

Phase 1 splits into **5 PRs**. The first factors the channel-history fetcher out of `channel_catchup.py` behind a Protocol; the second introduces the `conversation_window` module and config/schema additions; the third wires the call into `_on_event_inner` and adds the integration test; the last two are review follow-ups + Phase 1 closeout.

**Prerequisite**: v0.3.0 merged (✅ — released 2026-05-12). [RFC 0031 PR plan](0031-pr-plan.md) Phase 1 PR 3 merged (✅ — 2026-05-13) so the `chat_session_id` / `persatrix_session_id` columns are present *if* a future amendment flips [§OQ #1 resolution](#open-question-resolutions-locked-at-plan-authoring-time) to per-session windowing. Phase 1 of this plan does **not** consume those columns at the read path; the dependency is reservation-only and would otherwise be soft.

### Open-question resolutions locked at plan-authoring time

[v0.3.1-plan §Risk and mitigations](../v0.3.1-plan.md#risk-and-mitigations) names [OQ #1](0034-persona-conversational-working-memory.md#open-questions) as a hard gate before Phase 1 PR 1 opens. All four RFC open questions resolve here; the RFC's [Open Questions](0034-persona-conversational-working-memory.md#open-questions) section mirrors these resolutions inline.

- **[OQ #1](0034-persona-conversational-working-memory.md#open-questions) — per-session vs. per-channel transcript window: resolution 1a (per-channel, no session filter).** The window filters on `event.channel_id` only; rows are admitted regardless of `chat_session_id` or `persatrix_session_id`. Policy anchor: [RFC 0031 PR plan §OQ #1 resolution 1a](0031-pr-plan.md#open-question-resolutions-locked-at-plan-authoring-time) records single-session default-recall semantics as a Phase-2 load-bearing decision and notes that "Phase 1 lands no recall changes; the resolution is informational here." RFC 0031 Phase 1 is column-add only, so no prompt-content privacy contract attaches to the session columns in v0.3.1 — the Conversation Window is free to ignore them in Phase 1. Forward-compatible: if RFC 0031 Phase 2 ever attaches a privacy-bearing recall policy to the columns, the window picks up the same session filter additively through `build_conversation_messages`. **Phase 1 unit-test obligation**: assert two events on the same channel under different `persatrix_session_id` values share one window.
- **[OQ #2](0034-persona-conversational-working-memory.md#open-questions) — N vs. `max_tokens` binding: resolution 2a (both bind, tighter wins).** Phase 1 ships both bounds. Per-turn admission: token-overflow FIFO drop first; if surviving turns still exceed `N`, drop oldest until count ≤ `N`. Defaults `N=20`, `max_tokens=2048`. Retune is a one-line constant change once Phase 3 telemetry lands.
- **[OQ #3](0034-persona-conversational-working-memory.md#open-questions) — per-peer disambiguation in group channels: resolution 3a (inline prefix, deferred to Phase 2).** Phase 1 ships DM channels only — exactly one peer per channel — so the disambiguation question does not bind. Phase 2 implements the `[<peer_id>]: ` inline prefix per [RFC §C](0034-persona-conversational-working-memory.md#c-role-mapping). The role-mapping contract (§C) does not change between Phase 1 and Phase 2.
- **[OQ #4](0034-persona-conversational-working-memory.md#open-questions) — fact-extractor conversational context: resolution 4a (deferred to RFC 0026 follow-up).** Phase 1 ships the substrate (`build_conversation_messages`); RFC 0026's extractor consumes it as a follow-up tracked under that RFC's PR plan. Not a Phase 1 acceptance gate.

### Sequencing

**Recommended merge order**: **PR 1 → PR 2 → PR 3 → PR 4 → PR 5**. PR 1 (fetcher factoring) is a no-behaviour-change refactor and unblocks PR 2 (substrate) by exposing the Protocol. PR 2 ships the module and config/schema additions but adds no call site. PR 3 wires the call into `_on_event_inner` and ships the integration test that exercises the full path; it is the load-bearing acceptance gate. PRs 4–5 are review follow-ups and closeout.

This plan is independent of the [RFC 0026 PR plan](0026-pr-plan.md) at the implementation layer — the two RFCs share an acceptance surface ([MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) referential-follow-up legs) but no code paths. RFC 0026 PR plan PRs may run in parallel with this plan's PRs; only the v0.3.1 release-prep phase ([v0.3.1-plan §Phase 4](../v0.3.1-plan.md#phase-4--v031-release-prep-execution)) joins them.

---

## Dependency Graph

```
PR 1 (Factor _fetch_channel_history into agents/channel_history_fetcher.py behind Protocol)
  ↓
PR 2 (New agents/persona_runtime/conversation_window.py + config + schema; no call site)
  ↓
PR 3 (Wire build_conversation_messages into _on_event_inner; integration test;
      MT-PERSONA-CONVERSATION-001 manual test doc)
  ↓
PR 4 (Review follow-ups)
  ↓
PR 5 (Phase 1 closeout — status: ⚠️ Partially Implemented)
```

PR 1 is a pure refactor; no behaviour change. Catch-up (`channel_catchup.py`) keeps the same fetch path through the same Protocol.

---

## PR Sequence

### PR 1: `feature/v031-rfc0034p1-history-fetcher` — Factor Channel-History Fetcher Behind Protocol

**Depends on**: Nothing (v0.3.0 baseline + RFC 0031 PR plan PR 3 merged).
**Purpose**: Move `_fetch_channel_history` out of [`agents/channel_catchup.py`](../../agents/channel_catchup.py) (private helper at line 352) into a new shared module `agents/channel_history_fetcher.py` (created by this PR) behind a `ChannelHistoryFetcher` Protocol. No behaviour change; catch-up keeps using the same implementation through the new Protocol. PR 2 imports the Protocol — landing the refactor in its own PR keeps each diff reviewable and bisectable.

#### Scope

| File | Change |
|------|--------|
| `agents/channel_history_fetcher.py` | **New**. Module exposes (a) a `ChannelHistoryFetcher` `typing.Protocol` with one method `async fetch(channel_id: str, *, limit: int) -> list[dict[str, Any]] \| None`, and (b) a default production implementation `HttpChannelHistoryFetcher` backed by `aiohttp` with the same 10s timeout the catch-up path uses today. The implementation is the verbatim body of the existing [`_fetch_channel_history`](../../agents/channel_catchup.py#L352) helper, lifted unchanged — same `None`-on-error / list-on-success contract so the catch-up call site at [agents/channel_catchup.py L237](../../agents/channel_catchup.py#L237) (`if messages is None: continue`) keeps its branch verbatim. |
| [`agents/channel_catchup.py`](../../agents/channel_catchup.py) | Replace the inline `_fetch_channel_history` definition with an import of `HttpChannelHistoryFetcher` and call through it at the existing call site (line 237). The private helper symbol is removed; no other catch-up code paths shift. |
| `tests/unit/python/test_channel_history_fetcher.py` | **New** — unit tests on the Protocol contract (empty channel returns `[]`; HTTP 4xx/5xx and transport failure return `None` with a WARN, never raise — the `if messages is None: continue` guard depends on it; 10s default-timeout boundary; a duck-typed fake satisfies the Protocol). Sits beside `test_channel_catchup.py` to share the `orchestrator` loopback fixture. (Plan authored with an `agents/tests/` path; the catch-up suites actually live under `tests/unit/python/`.) |
| `tests/unit/python/test_channel_catchup.py` / `test_channel_catchup_followups.py` | No change. These suites exercise the fetcher end-to-end *through* `replay_channel_history` against the loopback `orchestrator` fixture — they never monkey-patched the private `_fetch_channel_history`, so the refactor is covered by the existing assertions passing unchanged. (Plan anticipated a `FakeChannelHistoryFetcher` fixture migration; none was needed.) |

#### Key implementation details

- The Protocol is defined with `typing.Protocol` (not `abc.ABC`) so a test fake is a duck-typed dataclass without inheritance ceremony.
- The production implementation keeps the existing `aiohttp` session-creation pattern verbatim (no shared-session refactor) — that optimization is out of scope for this PR.
- No call sites added in this PR. PR 2 imports the Protocol; PR 3 wires the call.
- `__all__` is set on the new module so static analyzers list both the Protocol and the default implementation as public surface.

#### Tests

- `HttpChannelHistoryFetcher.fetch(channel_id="c1", limit=20)` against a stub aiohttp server returns the JSON payload's `messages` array.
- Network timeout (simulated via a slow stub) returns `None` and logs a WARN — verbatim from the lifted helper.
- HTTP 404 / 5xx returns `None` and logs a WARN — same behaviour as the lifted helper today.
- Catch-up regression: the `test_channel_catchup.py` / `test_channel_catchup_followups.py` suites still pass unchanged; the `messages is None: continue` branch at [L237](../../agents/channel_catchup.py#L237) is exercised verbatim through `replay_channel_history`.

#### PR checklist

- [x] `pytest tests/unit/python/test_channel_history_fetcher.py tests/unit/python/test_channel_catchup.py tests/unit/python/test_channel_catchup_followups.py -q` passes.
- [x] `ruff check agents/` clean.
- [x] `mypy agents/` clean.
- [x] `_fetch_channel_history` private helper removed from `channel_catchup.py`; one import added.
- [x] No call sites of the new Protocol outside `channel_catchup.py` (PR 2 / PR 3 add the persona-runtime call site).
- [x] [v0.3.1-plan Master Progress Overview](../v0.3.1-plan.md#master-progress-overview) row 3b → 🔄 In progress on this PR opening.

---

### PR 2: `feature/v031-rfc0034p1-conversation-window` — Conversation Window Module + Config + Schema

**Depends on**: PR 1 merged.
**Purpose**: Land the `conversation_window` module and the config/schema additions. No call site yet — PR 3 wires it. Splitting substrate from wiring keeps each diff under the [BRANCHING.md](../BRANCHING.md) 500-line cap and lets PR 3 focus on the call-site decision and the integration test.

#### Scope

| File | Change |
|------|--------|
| `agents/persona_runtime/conversation_window.py` | **New**. Implements `ConversationWindowConfig` dataclass (`max_turns: int = 20`, `max_tokens: int = 2048`, `enabled: bool = True`) and `async def build_conversation_messages(*, event, agent_id, history_fetcher, current_user_message, config) -> list[dict[str, Any]]` per [RFC §A](0034-persona-conversational-working-memory.md#a-where-the-fix-lives). Uses `_format_event` from [`agents/persona_runtime/prompt_assembly.py`](../../agents/persona_runtime/prompt_assembly.py#L355-L362) for sanitization (per [RFC §D verification block](0034-persona-conversational-working-memory.md#d-sanitization-of-replayed-turns) — call `_format_event` per replayed turn so the existing `<\|` / `\|>` escape is inherited by construction). Role mapping per [RFC §C](0034-persona-conversational-working-memory.md#c-role-mapping). Token counting reuses the same `tiktoken`-with-`len // 4`-fallback path `WorkingMemory` uses (precedent from [RFC 0017 PR plan PR 1](0017-pr-plan.md#pr-1-featurev022-memory-budget--memorybudget-allocator--token-aware-truncation)). Per-turn admission applies token-overflow FIFO first then count-overflow FIFO per [§OQ #2 resolution](#open-question-resolutions-locked-at-plan-authoring-time). |
| `agents/persona_runtime/conversation_window.py` | Same file — in-process cache keyed by `(channel_id, last_known_message_id)` per [RFC §F](0034-persona-conversational-working-memory.md#f-caching-and-fetch-policy). The cache scope and the [RFC §F "Known gap" block](0034-persona-conversational-working-memory.md#f-caching-and-fetch-policy) are honoured: Phase 1 accepts framing (a) — the cache short-circuits only when no new message arrived between two wake-ups on the same channel. Steady-state turn-over-turn hit rate is documented as low; Phase 3 telemetry is the arbiter for any re-spec. |
| `agents/persona_runtime/conversation_window.py` | Same file — fetch-failure fall-back: on any exception from the Protocol, return `[current_event_only]` and log a WARN with `reason="conversation_window_fetch_failed"`. The persona is no worse off than today. |
| [`agents/persona_runtime/__init__.py`](../../agents/persona_runtime/__init__.py) | Re-export `ConversationWindowConfig` and `build_conversation_messages`. |
| [`config/agents.yaml`](../../config/agents.yaml) | Add an optional per-agent `conversation_window: {enabled: bool, max_turns: int, max_tokens: int}` block. Omitted ⇒ defaults from `optimization.yaml`. |
| [`config/optimization.yaml`](../../config/optimization.yaml) | Add a top-level `conversation_window: {enabled: true, max_turns: 20, max_tokens: 2048}` defaults block. |
| [`schemas/agent.schema.json`](../../schemas/agent.schema.json) | Add `conversation_window` object schema under each agent definition (all properties optional, `additionalProperties: false`). |
| `schemas/optimization.schema.json` (new — companion to `config/optimization.yaml`) | Add the matching defaults block schema. The optimization config has no schema today; this PR introduces it scoped to the `conversation_window` block only. Future RFCs may grow it. |
| `tests/unit/python/persona_runtime/test_conversation_window.py` | **New** — covers every branch in [RFC §Test Strategy unit-tests bullet](0034-persona-conversational-working-memory.md#test-strategy): empty channel, single-prior-turn, role mapping for DM, sanitization-escape round-trip on `<\|user_message\|>` literal in peer content, FIFO truncation at the transcript token budget, count-overflow FIFO at `N+5`, cache hit / miss / invalidation, fetch-failure fall-back to current-event-only without raising, **two events on the same channel under different `persatrix_session_id` values share one window** (the [§OQ #1 resolution](#open-question-resolutions-locked-at-plan-authoring-time) test obligation). |

#### Key implementation details

- **No call site added in this PR.** `_on_event_inner` is untouched. PR 3 wires the call.
- **Sanitization reuses, never duplicates, `_format_event`.** Per [RFC §D verified-against-current-code block](0034-persona-conversational-working-memory.md#d-sanitization-of-replayed-turns), Phase 1 calls `_format_event` per replayed turn to inherit the `safe_content = content.replace("<\|", "\\<\|").replace("\|>", "\\\|>")` escape by construction. Duplicating the wrapping logic in `conversation_window.py` is explicitly out of scope — a divergence-prone pattern this RFC exists to avoid.
- **Per-channel filter only.** The Protocol call passes `channel_id=event.channel_id` and no session filter. Cross-channel leakage is prevented server-side by the channel-scoped history endpoint (RFC 0011 §C).
- **Cache key includes `channel_id`.** Defence-in-depth against the (currently impossible) cross-channel `last_known_message_id` collision per [RFC §Security Considerations](0034-persona-conversational-working-memory.md#security-considerations).
- **No telemetry instruments shipped in Phase 1.** The `persatrix.persona.conversation_window.*` metrics surface lands in Phase 3 per [RFC §Phase 3](0034-persona-conversational-working-memory.md#phase-3-instrumentation-and-tuning); shipping inert counters now would invite premature dashboard work.
- **Replay-mode interaction.** No additional check in `conversation_window.py` — per [RFC §H verified-against-current-code block](0034-persona-conversational-working-memory.md#h-replay-mode-interaction), the replay-mode short-circuit at `action_loop.py:280-289` returns before reaching the LLM-seed line where PR 3 will install the call. The guard is inherited by construction.
- **`persatrix_session_id` on `AgentEvent` (verified against current code 2026-05-15).** The OQ #1 unit-test obligation ("two events on the same channel under different `persatrix_session_id` values share one window") depends on the test fixture surfacing a per-event session id. `AgentEvent` ([agents/persona_types.py](../../agents/persona_types.py) `class AgentEvent`) carries no top-level `persatrix_session_id` field today — it has an extensible `metadata: dict[str, Any]` slot and is constructed with the agent's resolved `PERSATRIX_SESSION_ID` at the dispatcher boundary (single read at construction; see [`agents/persona.py`](../../agents/persona.py)). PR 2's unit test therefore drives the contract by passing two synthetic session-id values through `build_conversation_messages` directly (or via `event.metadata`), without depending on a proto-level field that does not exist in v0.3.1. If a future RFC promotes `persatrix_session_id` to a top-level event field, the test migrates additively.

#### Tests

Beyond the per-branch coverage above:

- `ConversationWindowConfig` defaults match the values in `optimization.yaml`. A regression test loads `config/optimization.yaml` and asserts the parsed block equals the dataclass defaults — this catches the same drift class [RFC 0017 PR plan PR 2](0017-pr-plan.md#pr-2-featurev022-memory-context-rewrite--_inject_memory_context-allocate-loop) flagged on `_MEMORY_BUDGET_TOKENS`.
- Schema validation: `make validate` passes against the new blocks. Missing-block ⇒ defaults; explicit `enabled: false` ⇒ `build_conversation_messages` returns `[current_event_only]` (escape hatch for the dogfood persona if Phase 3 telemetry surfaces a regression).
- `_format_event` round-trip: a peer message containing the literal `<|user_message|>` arrives in the returned `messages` list with the literal escaped exactly as the current event's escape produces.

#### PR checklist

- [ ] `pytest tests/unit/python/persona_runtime/test_conversation_window.py -q` passes.
- [ ] `ruff check agents/` clean; `mypy agents/` clean.
- [ ] `make validate` passes against the new schema blocks.
- [ ] `agents/persona_runtime/__init__.py` re-exports both public symbols.
- [ ] No edits to `agents/persona_runtime/action_loop.py` (call-site wiring is PR 3).
- [ ] `_format_event` is **called** by `conversation_window.py`, not duplicated — verified by grep on the wrapping literals.
- [ ] [§OQ #1 resolution](#open-question-resolutions-locked-at-plan-authoring-time) test ships green.

---

### PR 3: `feature/v031-rfc0034p1-wire-and-itest` — Wire Call Site + DM Integration Test + Manual-Test Doc

**Depends on**: PR 2 merged.
**Purpose**: Install the `build_conversation_messages` call in [`agents/persona_runtime/action_loop.py`](../../agents/persona_runtime/action_loop.py#L402) immediately before the existing `messages: list[dict[str, Any]] = [...]` seed. Ship the DM integration test asserting the [ISSUE-0052](../issues/ISSUE-0052-persona-conversational-working-memory-gap.md) repro flips green. Author `MT-PERSONA-CONVERSATION-001` for Phase 4 release-prep execution.

#### Scope

| File | Change |
|------|--------|
| [`agents/persona_runtime/action_loop.py`](../../agents/persona_runtime/action_loop.py#L402) | At the existing seed line (~line 402), call `build_conversation_messages(...)` and use the returned list as the seed instead of the single-element list literal. The tool-use loop below is unchanged: it appends tool-call / tool-result rounds to `messages` exactly as today. The call passes `event=event`, `agent_id=self.agent_id`, `history_fetcher=self._history_fetcher`, `current_user_message=user_message`, `config=self._conversation_window_config`. |
| [`agents/persona_runtime/action_loop.py`](../../agents/persona_runtime/action_loop.py) (constructor / init path) | Resolve `ConversationWindowConfig` from the agent's `config/agents.yaml` block at construction time, falling back to `config/optimization.yaml` defaults; resolve the `HttpChannelHistoryFetcher` instance once and stash on `self._history_fetcher`. The fetcher reuse mirrors how `channel_catchup.py` constructs its fetcher today. |
| [`agents/persona_runtime/conversation_window.py`](../../agents/persona_runtime/conversation_window.py) | Add `_drop_leading_assistant_turns`, run as the final admission step in `_assemble_replayed_turns`, so the reconstructed `messages` array can never open with an `assistant` turn (see *Leading-role guard* below). |
| `tests/integration/test_conversational_continuity.py` | **New** — minimal repro of [ISSUE-0052](../issues/ISSUE-0052-persona-conversational-working-memory-gap.md): boot a persona, drive two DM turns where the persona asks a question on turn 1 and the user replies "what did you just ask?" on turn 2; assert the LLM-call recording shows the persona's turn-1 assistant message in the `messages` array on turn 2. Uses the existing `LLMClient` mock pattern; no real network. The integration test does not assert prose-level model output (which would be flaky); it asserts the `messages` array shape — the substrate guarantee — leaving the prose-level assertion to MT-PERSONA-CONVERSATION-001 manual execution. |
| `docs/manual-tests/MT-PERSONA-CONVERSATION-001.md` (new) | **New** (deliverable of this PR) — minimal-repro script per [v0.3.1-plan §Phase 2 cross-cutting acceptance](../v0.3.1-plan.md#phase-2--implement-the-three-rfcs): boot the dogfood persona, send "what's your favourite season?", reply "what did you just ask?", assert the persona answers correctly (cites its own prior question). Execution lives in [v0.3.1-plan Phase 4 PR 1](../v0.3.1-plan.md#phase-4--v031-release-prep-execution). The doc follows the [MT-SESSION-001](../manual-tests/MT-SESSION-001.md) structural template. |
| [`docs/manual-tests/MT-MEMORY-005-dementia-test.md`](../manual-tests/MT-MEMORY-005-dementia-test.md) | Update expected-outcomes for the referential-follow-up legs (the legs called out in [ISSUE-0052 Impact](../issues/ISSUE-0052-persona-conversational-working-memory-gap.md#impact)) — flagged as "expected pass once RFC 0034 + RFC 0026 both merge", per [RFC §Goals point 7](0034-persona-conversational-working-memory.md#goals). The expected-outcomes table edit is co-located with the equivalent RFC 0026 edit (RFC 0026's plan owns the long-term-memory legs; this plan owns the conversational-continuity legs). |

#### Key implementation details

- **Call placement.** The call goes immediately before the existing seed line, not inside it — keeps the diff tight and the rollback trivial (revert the inserted `messages = await build_conversation_messages(...)` line and restore the literal). The replay-mode short-circuit at `action_loop.py:280-289` (verified in [RFC §H](0034-persona-conversational-working-memory.md#h-replay-mode-interaction)) returns before this line, so catch-up replay events never reach the new call.
- **Leading-role guard.** Wiring `build_conversation_messages` into the live LLM seed makes a PR 2 substrate gap reachable: the replayed transcript carries no guarantee its first turn is a `user` turn. `_apply_admission` FIFO-trims the oldest turns with no role-parity guard, and a persona-first channel (the persona sent the channel's opening message) replays as an `assistant`-leading transcript outright — either way `build_conversation_messages` would return an `assistant`-leading `messages` array, which the Anthropic Messages API rejects with a hard 400 (`messages[0]` must use the `user` role; the persona runtime passes the seed through unmodified). PR 3 adds `_drop_leading_assistant_turns` to `conversation_window.py` as the final admission step — it strips any leading `assistant` prefix, and since it only ever shrinks the transcript the admission token / count bounds still hold. The fix belongs in the substrate module but lands with PR 3 because PR 3 is the commit that makes the path live. Regression coverage: `TestLeadingAssistantTurnGuard` in `test_conversation_window_followups.py`.
- **Config resolution timing.** `ConversationWindowConfig` is built once at agent construction, not per turn — the per-agent block (if any) wins over the `optimization.yaml` defaults. A re-resolution path on hot-reload is out of scope for Phase 1.
- **`enabled=false` integration test — added on follow-up review.** The plan originally carried no `enabled=false` integration test, reasoning that disabled-path *semantics* are PR 2's unit-test territory. A follow-up review reversed that: `test_disabled_config_degrades_to_current_event_only` is the *only* test that pins PR 3's config pass-through wiring — that `_build_seed_messages` resolves the persona's own `conversation_window` block and forwards it to `build_conversation_messages`. The success-path test (`test_persona_sees_its_own_prior_turn_on_the_next_dm_turn`) cannot pin it: its per-agent block is value-identical to the `ConversationWindowConfig` defaults, so a regression that dropped the resolved config and used defaults would leave it green. `enabled: false` is maximally distinct from the default `enabled: true`. The disabled-path *substrate* semantics remain PR 2's `test_conversation_window.py::TestDisabled` territory — the new test pins only the wiring. See *From PR 3 review* below.
- **Integration-test mock surface.** The test injects a `FakeChannelHistoryFetcher` returning a curated 1-message history; the `LLMClient` mock records the `messages` payload. The test asserts the payload's shape — `[user_msg_turn1, assistant_question_turn1, user_msg_turn2]` — not the model's response. This is the substrate guarantee.

#### Tests

- Integration: ISSUE-0052 minimal repro — `messages` array shape on turn 2 contains the persona's turn-1 assistant message.
- Integration: when the fetcher raises, the seed degrades to `[current_event_only]` and the persona still produces a turn (no exception bubbles to the dispatcher).
- No regression in any persona-runtime unit test that exercises the old seed line — every test that previously asserted a single-element seed gets a `FakeChannelHistoryFetcher` returning `[]` so the seed is `[current_event_only]`, identical to today.

#### PR checklist

- [ ] `pytest tests/integration/test_conversational_continuity.py -q` passes.
- [ ] `pytest agents/tests/ tests/unit/python/persona_runtime/ -q` passes.
- [ ] `messages` leading-role guard in place — `pytest tests/unit/python/test_conversation_window_followups.py::TestLeadingAssistantTurnGuard -q` green.
- [ ] PR 3 review follow-ups folded in (unwired short-circuit ordering, resolver range check — see *From PR 3 review* below) — `pytest tests/unit/python/test_conversation_window_config.py -q` green.
- [ ] `ruff check agents/` clean; `mypy agents/` clean.
- [ ] `make validate` clean.
- [ ] `docs/manual-tests/MT-PERSONA-CONVERSATION-001.md` authored; execution deferred to v0.3.1-plan Phase 4 PR 1.
- [ ] [MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) referential-follow-up legs flagged "expected pass" with cross-reference to this RFC.
- [ ] [RFC 0034 row in ROADMAP](../../ROADMAP.md#rfc-master-index) → `🚧 Implementing` on this PR opening (the first PR that touches a runtime call site — PRs 1–2 are refactor + dormant substrate).
- [ ] [v0.3.1-plan Master Progress Overview](../v0.3.1-plan.md#master-progress-overview) row 3b → 🔄 In progress (already flipped on PR 1; reaffirm).

---

### PR 4: `feature/v031-rfc0034p1-followups` — Review Follow-Ups

**Depends on**: PR 3 merged.
**Purpose**: Address review findings surfaced during PRs 1–3 review. Follows the [RFC 0017 PR plan §PR 6 precedent](0017-pr-plan.md) — "From PR N review" subsections, each finding paraphrased inline (no link to local review reports per [.github/copilot-instructions.md](../../.github/copilot-instructions.md)).

#### Scope

Items below are populated as PRs are reviewed. Per [.github/copilot-instructions.md](../../.github/copilot-instructions.md) ("Local-only files MUST NEVER be referenced in any committed file"), each entry paraphrases the finding inline and does **not** reference or link any local PR review report.

##### From PR 1 review

- **`fetch` can raise on a successful-but-malformed response.** `HttpChannelHistoryFetcher.fetch` reads `data.get("messages")` *outside* the `try` block that wraps the request and `resp.json()`. A 2xx response whose JSON body is not an object (a bare array, string, number, or `null`) makes that attribute lookup raise `AttributeError`, which escapes `fetch` instead of degrading to `[]` the way a `dict` with an unusable `messages` field does. PR 1 lifted the helper verbatim, so the gap is pre-existing and was preserved deliberately (PR 1 is a no-behaviour-change refactor). **Fix**: move the `data.get` / `isinstance` shape guard inside the request `try` so a request that succeeds with an unusable body degrades to `[]` and the "never raises across the seam" intent holds for the PR 3 conversation-window caller. The gap is pinned under `xfail(strict=True)` by `TestHttpChannelHistoryFetcherTopLevelNonObjectBody` in `tests/unit/python/test_channel_history_fetcher.py` — closing it flips that test to xpass, so this PR must also remove the `xfail` marker.
- **WARN log prefix `"channels: catch-up history …"` is caller-specific.** Both `logger.warning` strings in `HttpChannelHistoryFetcher.fetch` were lifted verbatim from the catch-up helper and read "catch-up history". Correct while catch-up is the only caller, but once PR 3 wires the persona conversation-window caller a fetch failure on a normal persona turn logs a catch-up-flavoured message, which misleads an operator reading the log. **Fix**: genericize the prefix (e.g. `"channels: history fetch …"`) so it is accurate for both callers. Pure log-string change — confirm no log-scraping alert or dashboard greps the old literal before renaming.

##### From PR 2 review

_None recorded at plan-authoring time. Add findings here if surfaced post-merge._

##### From PR 3 review

Of the four PR 3 review findings — one blocking, three non-blocking — three were resolved in PR 3 itself (two fixed, one evaluated and rejected) and none were deferred to this PR. The blocking finding (the reconstructed `messages` array could open with an `assistant` turn — a hard Anthropic 400) was fixed via the `_drop_leading_assistant_turns` guard, described under *Leading-role guard* in the PR 3 entry above. The three non-blocking findings:

- **`conversation_window` config was resolved on the unwired short-circuit path.** `_ConversationWindowMixin._build_seed_messages` ([`conversation_seed.py`](../../agents/persona_runtime/conversation_seed.py)) resolved and cached `_conversation_window_config` *before* the `_history_fetcher is None` check. On the unwired path — task-only agents and partial-init test paths — the resolved config was then discarded: `resolve_conversation_window_config` ran for nothing. It could not crash (the resolver never raises), so this was harmless, but a reader expects the `None`-fetcher short-circuit to skip *all* conversation-window work. **Fixed**: the `_history_fetcher is None` check now runs first, so the short-circuit is total — an unwired persona resolves no config. Pure reordering, no behaviour change. Regression coverage: `test_unwired_fetcher_skips_conversation_window_config_resolution` in `test_conversational_continuity.py` pins that the resolver cache stays unpopulated on the unwired path.
- **`resolve_conversation_window_config` type-checked but did not range-check the integer counts.** The resolver rejected non-`int` (and `bool`) values for `max_turns` / `max_tokens` and degraded them to the per-key default, but accepted any other `int` — including `0` and negatives. [`schemas/agent.schema.json`](../../schemas/agent.schema.json) enforces `minimum: 1`, so configs gated through `make validate` were already safe; dict-built and test configs bypass that gate, and a `max_turns: 0` (or negative) silently yielded an empty replayed window — `_apply_admission` drops every turn — instead of degrading to the default. **Fixed**: the resolver now treats `max_turns < 1` / `max_tokens < 1` as malformed and degrades to the per-key default, consistent with the wrong-type path and with the schema lower bound; the docstring was corrected to match. The JSON schema stays the production range gate — this only makes the resolver self-consistent for the configs that bypass it. Regression coverage: `test_zero_or_negative_count_falls_back_per_key` and `test_count_of_one_is_the_accepted_lower_bound` in `test_conversation_window_config.py`.
- **Mixin base-class list formatting in `_LLMPersonaAgent`.** [`agents/persona_runtime/__init__.py`](../../agents/persona_runtime/__init__.py) places `_ActionLoopMixin, _ConversationWindowMixin,` on one shared line while every other base in the class statement is one-per-line. **Not applied — evaluated and rejected.** The file sits at exactly the 500-line `file_size.py --strict` cap — the same cap that motivated carving `_ConversationWindowMixin` into its own `conversation_seed.py` module — so the shared-line grouping is a deliberate one-line saving that keeps `__init__.py` under it, not an oversight. Splitting the line pushes the file to 501 and fails the pre-commit file-size check. A purely cosmetic consistency tweak does not justify either breaking the cap or evicting an unrelated line to fund it; the grouping stays.

**Follow-up review pass.** A later review of the wired branch surfaced one further actionable finding plus one evaluated non-defect, both folded into PR 3:

- **No test pinned PR 3's config pass-through.** The wired-success integration test (`test_persona_sees_its_own_prior_turn_on_the_next_dm_turn`) uses a `conversation_window` block — `enabled: true`, `max_turns: 20`, `max_tokens: 2048` — that is value-identical to the `ConversationWindowConfig` dataclass defaults. A regression that dropped the resolved per-agent config and fell back to defaults inside `_build_seed_messages` would therefore leave every existing test green: the resolver suite (`test_conversation_window_config.py`) tests `resolve_conversation_window_config` in isolation, not its call site. **Fixed**: added `test_disabled_config_degrades_to_current_event_only` to `test_conversational_continuity.py` — a persona with `conversation_window.enabled: false` and a fetcher wired to a non-empty transcript. `enabled: false` is maximally distinct from the default `enabled: true`: if the resolved block did not reach `build_conversation_messages` the window would reconstruct the transcript and seed more than one turn. This supersedes the plan's original *No `enabled=false` path in the integration test* note (PR 3 *Key implementation details* above, now updated). Disabled-path *substrate* semantics stay pinned by `test_conversation_window.py::TestDisabled`.
- **Consecutive same-role turns — evaluated, not a defect.** `_drop_leading_assistant_turns` guards `messages[0]`; a reviewer may ask whether the substrate can also emit consecutive same-role turns *elsewhere* in the array — e.g. a peer sending two DM messages back-to-back before the persona replies replays as `[user, user, …]`. It can, but the Anthropic Messages API combines consecutive same-role turns into a single turn — no role-validity error, and no model-relevant information loss (both elements are peer input). No guard is warranted for Phase 1's `AnthropicProvider`. A provider with a strict-alternation contract would change this; that is a Phase 2 / multi-provider concern, not a PR 3 gap.

#### PR checklist

- [ ] All deferred review findings addressed or downgraded to tracked issues with rationale.
- [ ] `make test` + `make lint` clean.
- [ ] If any finding touched the call site, the integration test from [PR 3 §Tests](#pr-3-featurev031-rfc0034p1-wire-and-itest--wire-call-site--dm-integration-test--manual-test-doc) re-runs green.

---

### PR 5: `feature/v031-rfc0034p1-close` — Phase 1 Closeout

**Depends on**: PR 4 merged.
**Purpose**: Mark Phase 1 implemented and hand off to v0.3.x for Phases 2–3.

#### Scope

| File | Change |
|------|--------|
| [`docs/rfcs/0034-persona-conversational-working-memory.md`](0034-persona-conversational-working-memory.md) | Status → `⚠️ Partially Implemented (Phase 1)`. Append "Phase 1 implemented in v0.3.1" note to Decision/Next Steps. |
| [`ROADMAP.md`](../../ROADMAP.md) | RFC 0034 row → `⚠️ Partially Implemented`; target column `v0.3.1 (Phase 1) + v0.3.x (Phases 2–3)`. `Last updated` refresh. |
| [`docs/rfcs/0034-pr-plan.md`](0034-pr-plan.md) | [Progress Overview](#progress-overview-phase-1) rows filled with merged-PR numbers and dates. |

No code changes; doc-only.

#### PR checklist

- [x] RFC 0034 status = `⚠️ Partially Implemented`.
- [x] [ROADMAP RFC Master Index](../../ROADMAP.md#rfc-master-index) updated.
- [x] [v0.3.1-plan Master Progress Overview](../v0.3.1-plan.md#master-progress-overview) row 3b → ✅.
- [x] [ISSUE-0052](../issues/ISSUE-0052-persona-conversational-working-memory-gap.md) status updated to "Closed by RFC 0034 Phase 1 (v0.3.1)".

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| The PR 1 fetcher refactor accidentally changes catch-up behaviour (e.g. error semantics on a flaky history endpoint). | PR 1 lifts the body verbatim, including the `Optional[list[dict]]` return contract (`None` on HTTP 4xx/5xx and on exception); `test_channel_catchup.py` runs unchanged after fixture migration. The Protocol introduction is type-only. |
| Cache key advances per-turn so steady-state hit rate is low — operator may expect a higher-leverage cache than they get. | [RFC §F "Known gap" block](0034-persona-conversational-working-memory.md#f-caching-and-fetch-policy) documents framing (a) explicitly. Phase 3 telemetry is the arbiter; re-spec to framing (b) is additive, not breaking. |
| Sanitization regression — a future contributor refactors `_format_event` and breaks the escape that `conversation_window.py` depends on by construction. | PR 2's escape-round-trip unit test on the literal `<|user_message|>` content covers this — any regression in `_format_event` fails the conversation-window suite, not just the prompt-assembly suite. |
| Fetch-failure fall-back masks a real outage (silent degradation). | WARN log with `reason="conversation_window_fetch_failed"` per [RFC §F](0034-persona-conversational-working-memory.md#f-caching-and-fetch-policy). Phase 3 ships the `fallback_to_empty_window` counter so the rate is observable. Until then, log-grep is the operator surface. |
| OQ #1 resolution 1a becomes load-bearing in a future RFC 0031 Phase 2 amendment that re-frames the session boundary as privacy-bearing. | Resolution is recorded in both [§Open-question resolutions](#open-question-resolutions-locked-at-plan-authoring-time) and [RFC §Open Questions §1](0034-persona-conversational-working-memory.md#open-questions). Forward path is additive: the future amendment threads the session filter into `build_conversation_messages` without changing its signature semantics for existing callers. |
| Defaults `N=20`, `max_tokens=2048` are guesses — a high-volume DM channel may exhaust the transcript budget every turn and starve long-term tiers via downstream effects. | The transcript budget is deducted *before* the system-prompt memory budget computes its allocation per [RFC §E](0034-persona-conversational-working-memory.md#e-token-budget-interaction); long-term tiers cannot be starved by the conversation window. Phase 3 telemetry retunes the defaults. |
| RFC 0026 ships before RFC 0034 (or vice versa) and the v0.3.1 release-prep MT execution catches a referential-follow-up regression. | The two RFCs share an acceptance surface but no code paths. Either ordering produces the same runtime — the substrate (this RFC) and the consumer (RFC 0026) compose at the recall-time fact-extractor call. v0.3.1 release-prep ([Phase 4 PR 1](../v0.3.1-plan.md#phase-4--v031-release-prep-execution)) re-runs MT-MEMORY-005 once both are merged. |
| Group-channel users assume Phase 1 covers them and report a "broken" persona on group channels. | Phase 1 is DM-only by [§Future Phases](#future-phases). The CHANGELOG `[0.3.1]` Upgrade Notes (specified by [v0.3.1-plan Phase 3](../v0.3.1-plan.md#phase-3--v031-release-prep-plan)) carries an explicit "Group-channel transcript reconstruction lands in v0.3.x (RFC 0034 Phase 2)" line. |

---

## ROADMAP Hygiene

Per [.github/copilot-instructions.md](../../.github/copilot-instructions.md) "Status Hygiene" and [v0.3.1-plan §ROADMAP hygiene](../v0.3.1-plan.md#roadmap-hygiene):

- **PR 1 opens** → no RFC 0034 status change (refactor only); [v0.3.1-plan Master Progress Overview](../v0.3.1-plan.md#master-progress-overview) row 3b → 🔄 In progress.
- **PR 3 opens** → RFC 0034 row → `🚧 Implementing` (first runtime call-site PR).
- **Each PR merges** → fill the [Progress Overview](#progress-overview-phase-1) row.
- **PR 5 merges** → RFC 0034 row → `⚠️ Partially Implemented`; master-plan row 3b → ✅; `Last updated` refresh; ISSUE-0052 closed.

---

## Future Phases

Reserved for v0.3.x patches beyond v0.3.1. Out of scope for this plan; tracking notes only.

### Phase 2 — Group-Channel Role Mapping

Extend the role mapper to multi-peer channels per [RFC §C](0034-persona-conversational-working-memory.md#c-role-mapping) and [RFC §G](0034-persona-conversational-working-memory.md#g-group-channel-handling). Add the `[<peer_id>]: ` inline prefix at the sanitization step. Integration test: two peers + one persona; persona resolves a pronoun referring to the *other* peer's prior turn. Phase 2 PR plan opens only after this Phase 1 plan closes and the v0.3.1 release tag cuts. Cross-cutting dependency on [RFC 0030](0030-multi-agent-conversation-governance.md) Layer 1 — reply-budget / termination semantics layer on top of the Phase 2 role mapping; Phase 2 ships the substrate, RFC 0030 ships the governance.

**Conversation-window fetch cache must be revisited.** Phase 1's in-process cache in [`agents/persona_runtime/conversation_window.py`](../../agents/persona_runtime/conversation_window.py) keys on `(channel_id, message_id)`, and the cached rows carry the *first* caller's `max_turns + 1` fetch limit. That is sound only because a DM channel has exactly one persona — the same `(channel_id, message_id)` is always processed under one `conversation_window` config, so the fetch limit is constant. Once a channel can host multiple personas with independent configs, a small-`max_turns` persona could populate the cache and serve an undersized window to a large-`max_turns` peer reacting to the same inbound message. Phase 2 must key the cache on the fetch limit (or bypass it for multi-persona channels). Surfaced in PR 2 review; harmless in Phase 1, so deferred here rather than fixed in PR 2.

### Phase 3 — Instrumentation and Tuning

Cache-hit rate, fetch latency, fallback-to-empty-window count exposed as OTEL metrics under `persatrix.persona.conversation_window.*`. Re-tune `max_turns` and `max_tokens` defaults from a one-week telemetry sample on the dogfood persona. Document the tunables in [`docs/guides/persona-agents.md`](../guides/persona-agents.md). Phase 3 may also re-spec the [RFC §F "Known gap" cache framing](0034-persona-conversational-working-memory.md#f-caching-and-fetch-policy) from (a) to (b) if telemetry justifies — that is the point at which "do I have to re-render the window?" gets separated from "did the channel change?".

**Conversation-window fetch cache has no eviction.** Phase 1's in-process `_WINDOW_CACHE` in [`agents/persona_runtime/conversation_window.py`](../../agents/persona_runtime/conversation_window.py) never deletes an entry: a channel seen once keeps its `(message_id, raw_rows)` tuple for the life of the process, so the dict grows with the number of *distinct* channels a long-running orchestrator ever serves — not the number concurrently active — and each entry holds up to `max_turns + 1` raw row dicts. Harmless for the Phase 1 DM dogfood (a handful of channels); unbounded over a long-lived process across many channels. Phase 3 owns the cache-hit-rate telemetry and is the natural place to add an LRU bound — the telemetry supplies a real channel-count distribution to size the bound against, rather than guessing a capacity now. Surfaced in PR 2 review; the in-module comment documents the gap, so deferred here rather than fixed in PR 2.

---

## Progress Overview (Phase 1)

| # | Title | Branch | Status | GitHub PR | Merged |
|---|-------|--------|--------|-----------|--------|
| 1 | Factor channel-history fetcher behind Protocol | `feature/v031-rfc0034p1-history-fetcher` | ✅ Merged | [#351](https://github.com/mkhomutov/Persatrix/pull/351) | 2026-05-16 |
| 2 | Conversation Window module + config + schema | `feature/v031-rfc0034p1-conversation-window` | ✅ Merged | [#352](https://github.com/mkhomutov/Persatrix/pull/352) | 2026-05-16 |
| 3 | Wire call site + DM integration test + manual-test doc | `feature/v031-rfc0034p1-wire-and-itest` | ✅ Merged | [#356](https://github.com/mkhomutov/Persatrix/pull/356) | 2026-05-16 |
| 4 | Review follow-ups | `feature/v031-rfc0034p1-followups` | ✅ Merged | [#357](https://github.com/mkhomutov/Persatrix/pull/357) | 2026-05-16 |
| 5 | Phase 1 closeout | `feature/v031-rfc0034p1-close` | 🔀 PR open | — | — |

---

## Related Documentation

- [RFC 0034 — Persona Conversational Working Memory](0034-persona-conversational-working-memory.md) — canonical spec.
- [ISSUE-0052 — Persona conversational working-memory gap](../issues/ISSUE-0052-persona-conversational-working-memory-gap.md) — operational driver; closes after PR 5.
- [v0.3.1-plan.md](../v0.3.1-plan.md) — master plan (row 3b is this workstream).
- [v0.3.1 plan amendment 2026-05-15](../v0.3.1-plan-amendment-2026-05-15.md) — absorption rationale.
- [RFC 0011 — Channels & Internal Agent Messaging](0011-channels-bridges.md) — durable channel store + history endpoint that this plan reads from.
- [RFC 0017 — Persona Memory Injection Token Budget](0017-persona-memory-injection-budget.md) — orthogonal system-prompt memory budget; [RFC 0017 PR plan](0017-pr-plan.md) is the structural template for this plan.
- [RFC 0020 — Interaction Lifecycle](0020-interaction-lifecycle.md) — episode/interaction boundary alignment.
- [RFC 0026 — Declarative Facts Tier](0026-declarative-facts-tier.md) / [RFC 0026 PR plan](0026-pr-plan.md) — paired Phase-1 workstream in v0.3.1; consumes this RFC's substrate as a follow-up.
- [RFC 0030 — Multi-Agent Conversation Governance](0030-multi-agent-conversation-governance.md) — group-channel governance built atop this RFC's Phase 2 role mapping.
- [RFC 0031 — Per-Session Namespacing](0031-per-session-namespacing-channels.md) / [RFC 0031 PR plan](0031-pr-plan.md) — Phase 1 columns this plan reserves but does not consume per [§OQ #1 resolution](#open-question-resolutions-locked-at-plan-authoring-time).
- [MT-MEMORY-005 — Dementia test](../manual-tests/MT-MEMORY-005-dementia-test.md) — referential-follow-up legs flip green when this RFC + RFC 0026 are both merged.
- `docs/manual-tests/MT-PERSONA-CONVERSATION-001.md` — minimal repro of ISSUE-0052; authored in PR 3, executed in v0.3.1-plan Phase 4 PR 1.
