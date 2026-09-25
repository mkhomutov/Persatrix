---
id: ISSUE-0169
summary: "Claude Opus 5 and Sonnet 5 think when a request sets no `thinking`, and Fable always does, but the Anthropic adapter was written for models that do not. It sends no thinking or effort setting, so the 64–320-token side calls (salience bids, the reflexion critic, working-memory compression) can spend their whole `max_tokens` on thinking and come back with no text; a bid then fails closed to silence. It also drops thinking blocks when it replays a tool round, and reads a `refusal` stop reason as a normal end of turn. No shipped config uses these models, which #995 made callable."
status: open
severity: medium
area: agents
created: 2026-09-25
refs:
  - https://github.com/mkhomutov/Persatrix/pull/995
  - agents/llm_providers.py
  - agents/llm_types.py
  - agents/llm_factory.py
  - agents/llm_client.py
  - agents/llm_gemini.py
  - agents/salience_deliberation.py
  - agents/salience_bid.py
  - agents/persona_runtime/reflexion.py
  - agents/memory/working.py
  - agents/base.py
  - agents/persona_runtime/action_loop.py
  - docs/experiments/EXP-001-harness-choices.md
---

# ISSUE-0169: The Anthropic adapter does not handle Claude models that think by default

## Summary

Claude Opus 5 and Sonnet 5 think when a request sets no `thinking`, and
Fable 5 and 5.1 always think. `max_tokens` caps the thinking and the reply
together. `AnthropicProvider` sends no thinking or effort setting, keeps only
text and tool-use blocks from a response, and knows three stop reasons. On
these models that breaks three things: short side calls can come back empty,
tool rounds lose their thinking blocks, and a refusal reads as a finished turn.

## Context

[#995](https://github.com/mkhomutov/Persatrix/pull/995) stopped sending
`temperature` to the Claude models that reject it with a 400 (Opus 4.7 and
later, Sonnet 5, Fable), so those models can now be called at all. Its review
found what else they need (findings F-4 and F-6). The API behaviour below is
from Anthropic's model migration guidance.

**Thinking uses the reply's budget.** Opus 5 and Sonnet 5 run adaptive
thinking when `thinking` is omitted; Opus 4.7 and 4.8 do not. Fable always
thinks, and `thinking: {type: "disabled"}` returns a 400 on it, as it does on
Opus 5.5. Opus 5 accepts `disabled` only at effort `high` or below.
`max_tokens` is a hard cap on thinking plus text, so a call sized tightly
around its answer can stop before writing any.

The adapter's request (`create_message` in
[`llm_providers.py`](../../agents/llm_providers.py)) carries no `thinking`
and no `output_config`. These calls use small budgets:

- the salience bid, on the `fast` alias: 64, 128 or 320 tokens by mode
  ([`salience_deliberation.py`](../../agents/salience_deliberation.py),
  [`salience_bid.py`](../../agents/salience_bid.py))
- the reflexion critic, on `fast`: 64 tokens
  ([`reflexion.py`](../../agents/persona_runtime/reflexion.py))
- working-memory compression, on `summarizer`: half the section, at least 64
  tokens ([`working.py`](../../agents/memory/working.py))

A reply that holds only a thinking block and stops at `max_tokens` becomes
`text=None` with stop reason `MAX_TOKENS`. The bid then fails its parse and
the persona stays silent, with at most a DEBUG log line; the critic reads the
empty reply as "not weak". Before #995 the same configuration failed loudly,
with a 400 logged as a WARNING.

The Gemini adapter already handles this failure. An alias's
`provider_config.thinking_budget` caps or disables thinking
([`llm_gemini.py`](../../agents/llm_gemini.py)), and
`config/demo/gemini/optimization.yaml` sets it to 0 on `fast` and
`summarizer`. [`llm_factory.py`](../../agents/llm_factory.py) builds
`AnthropicProvider` without any `provider_config`.

**Thinking blocks are dropped in tool rounds.** `_normalize` keeps only `text`
and `tool_use` blocks, and `LLMResponse`
([`llm_types.py`](../../agents/llm_types.py)) has no place for others.
`append_tool_round` rebuilds the assistant turn from the text and the tool
calls, so a turn that was `[thinking, tool_use]` is sent back as
`[tool_use]`. Both tool loops use it: `BaseAgent`
([`base.py`](../../agents/base.py)) and the persona action loop
([`action_loop.py`](../../agents/persona_runtime/action_loop.py)).
Anthropic's guidance is to pass thinking blocks back unchanged in tool-use
loops, and it warns that removing them can cause ordering or signature 400s.
At best, the reasoning behind each tool call is lost.

**A refusal reads as a finished turn.** Opus 5 and Fable can decline a
request with HTTP 200 and `stop_reason: "refusal"`. The adapter's map knows
`end_turn`, `tool_use` and `max_tokens`; anything else is logged as unmapped
and returned as `END_TURN`. A task agent then reports the step `COMPLETED`
with empty or partial text, and a persona posts partial text as its answer.
The fallback is deliberate: `StopReason` documents it, and
[EXP-001's harness choices](../experiments/EXP-001-harness-choices.md) read a
refusal as `end_turn` for arm A.

## Impact

Nothing shipped is affected today. The shipped Anthropic configs map the
aliases to `claude-sonnet-4-6` and `claude-haiku-4-5-20251001`, which think
only when asked. An operator who points `fast` or `summarizer` at Opus 5,
Sonnet 5 or Fable gets governed channels that go quiet without an error, a
critic that never objects, and compression that returns nothing. On
`quality`, tool rounds may fail or lose their reasoning, and refused turns
count as answers. The failures are silent where they used to be loud.

## Proposed fix / investigation path

1. **Per-alias request settings, applied per call.** Give an Anthropic alias
   the `provider_config` route Gemini has, for example `thinking` and
   `effort`, and apply it in `LLMClient` on each call rather than when the
   provider is built. A lane on the persona's own vendor reuses the persona's
   provider (`_provider_for_alias` in
   [`llm_client.py`](../../agents/llm_client.py)), so a setting fixed at
   construction never reaches it. Where thinking cannot be turned off (Fable,
   Opus 5.5), lower the effort and give the small calls room. On Opus 5,
   Anthropic's guidance prefers low effort over disabling thinking.
2. **Replay thinking blocks unchanged.** Keep the response's content blocks on
   `LLMResponse`, and have `append_tool_round` send them back in their
   original order.
3. **Give a refusal its own stop reason**, and decide what each caller does
   with it. Arm A's reading is frozen in the harness choices, so a change
   there goes through an amendment.

Confirm with live calls before building: a 64-token salience bid on
`claude-opus-5` and on `claude-sonnet-5`, and a two-round tool loop on
`claude-opus-5`.

## Slot

Not slotted. No release plan is open: ruling (a) of the
[sequencing Amendment 2026-09-12](../v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens)
opens none before EXP-001 reports and the first strategy review logs its
result. EXP-001's arms run on `claude-sonnet-4-6`, which this does not touch.
Its judge is `claude-opus-5` at default settings, so it thinks: harness PR 6,
which builds the judge, has to leave room for that in `max_tokens` and decide
how it reads a refusal.

## Notes

> 2026-09-25 — filed from the review of
> [#995](https://github.com/mkhomutov/Persatrix/pull/995) (findings F-4 and
> F-6), outside that PR's scope. Each mechanism was shown with the SDK mocked:
> a thinking-only reply to a 64-token bid ends in `parse_failure`, a tool
> round replays only its `tool_use` block, and a refusal comes back as a
> completed step. The live effects are not yet measured.
