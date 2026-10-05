---
id: ISSUE-0182
summary: "A persona turn's system prompt puts text that changes every turn (the persona's current state and the clock line, to the second) ahead of about 1 450 tokens of fixed instructions, and appends the turn's recalled memory after them. A provider's prompt cache reads a prompt back only up to its first changed byte, so a persona's next turn reads back only the tool definitions and identity sections, never the fixed instructions or the replayed transcript. Anthropic's prompt_cache, OpenAI's automatic cache and Gemini's implicit cache are all held back the same way, and on Anthropic and OpenAI from GPT-5.6 each turn pays a quarter more to write what is never read."
status: open
severity: low
area: persona
created: 2026-10-05
refs:
  - agents/persona_runtime/prompt_assembly.py
  - agents/persona_runtime/compose_prompt.py
  - agents/persona_types.py
  - docs/rfcs/0021-persona-temporal-awareness.md
  - docs/guides/model-request-settings.md
---

## Summary

A persona's system prompt changes near its start on every turn, so a
provider's prompt cache can serve only the small part before the change.

## Context

Found while adding per-alias prompt caching to the Anthropic adapter
([ISSUE-0169](ISSUE-0169-anthropic-adapter-ignores-default-thinking.md)).
A prompt cache works on prefixes: a request reads back the longest start it
shares, byte for byte, with a recent one, and everything after the first
difference is billed again. The provider renders the tool definitions first,
then the system prompt, then the messages.

`_build_system_prompt`
([`prompt_assembly.py`](../../agents/persona_runtime/prompt_assembly.py))
assembles, in order:

1. the persona sections: identity, grounding, background, behavior, quirks,
   goals — fixed for a persona;
2. the current-state section ([`PersonaState.to_prompt_section`](../../agents/persona_types.py)):
   mood, stress, energy below 0.5, the last five recent-context lines and goal
   progress — changes from turn to turn;
3. the clock line, `Current time: 2026-10-05T14:03:27+00:00 (Monday
   afternoon).` — changes every second;
4. eight fixed snippets, about 1 080 words (conversation window, message
   delimiters, external data, reply discretion, pacing, peer voice, end vote,
   memory tools).

[`build_compose_system_prompt`](../../agents/persona_runtime/compose_prompt.py)
then appends the turn's recalled memory and, under `mode: plan`, the plan.
The replayed transcript follows in `messages`.

So between two turns of one persona, the shared start ends inside item 2 at
the latest. The fixed snippets after it, and the transcript after those, are
never read back.

## Impact

- **Anthropic.** With `prompt_cache: true` a persona's single-call turn writes
  its prompt at 1.25 times the input price and the next turn reads back only
  the tool definitions (about 500 tokens of memory tools). By a rough
  estimate the setting pays for persona chat only when about one turn in ten
  makes a tool round; the shipped Anthropic demo turns it on anyway, because
  the same alias serves the task agents' tool loops.
- **OpenAI** caches by itself, and from GPT-5.6 bills writes at 1.25 times
  input, so each persona turn pays that premium on a prompt the next turn
  will not read.
- **Gemini** caches by itself at no write charge, but only from 4 096 tokens
  of shared start on the 3.x Flash models, which a persona's fixed part alone
  does not reach.

Nothing breaks; the cost is money a cache could save and does not.

## Proposed fix / investigation path

Order the system prompt by how often each part changes: the persona sections,
then the fixed snippets, then the current state and the clock line, then
recalled memory and the plan. The fixed part, about 2 000 tokens with the
tool definitions, would then be read back on every turn within the cache's
lifetime, across a persona's turns and its tool rounds alike.

Two constraints decide when:

- [RFC 0021 §C](../rfcs/0021-persona-temporal-awareness.md#c-now-anchor-in-the-system-prompt)
  places the clock line "between the dynamic-state block and the
  user-message-boundary instruction", so moving it takes an amendment.
- EXP-001's arms run on the persona prompt as it is, and the pre-registration
  fixes their build. The change waits until the experiment reports.

Moving the clock line and recalled memory after the transcript, as a
message of their own, would let the cache reach the transcript too. That is a
larger change to what the model reads and wants its own measurement.

## Notes

> 2026-10-05 — filed from the model refresh that added per-alias request
> settings (ISSUE-0169 items 1 and 2). The sizes above were measured on the
> shipped prompt files: the memory-tool definitions at about 1 970
> characters, the eight snippets at 1 079 words.
