# Model Request Settings

Current models think before they answer, and every provider can cache a prompt
it has seen recently. Both change what a call costs and how long it takes. A
model alias's `provider_config` can set them, so each role gets the trade-off
it needs: a cheap bid thinks little, a hard task thinks more, and a prompt that
repeats is read from the cache instead of being paid for again.

This guide lists those **request settings** per provider, explains when the
prompt cache saves money and when it costs extra, and says how to check that it
works. Aliases themselves are in [Model Providers & Aliases](model-providers.md).

---

## Where the settings live

Request settings are keys of an alias's `provider_config`, next to the alias's
model and price:

```yaml
# config/optimization.yaml (or a config/demo/<provider>/ overlay)
models:
  aliases:
    quality:
      provider: anthropic
      model: claude-sonnet-5-5
      input_per_1m_tokens: 2.00
      output_per_1m_tokens: 10.00
      provider_config:
        effort: low          # think briefly; skip it on simple requests
        prompt_cache: true   # write prompts to Anthropic's cache
```

Each call uses the settings of the alias it names. That matters because an
agent holds one provider, built from its own alias, and the shared lanes ride
it when they are on the same vendor: the salience bid and the reflexion critic
(`fast`), the close summary and memory compression (`summarizer`). So
`effort: low` on `quality` never reaches a Claude Haiku bid on `fast`, which
would reject it, and `thinking_level: minimal` on a Gemini `fast` alias does
reach the bid made through a `quality` agent. A call through the agent's own
alias also keeps any setting the agent entry adds in `agents.yaml`.

An alias with no request settings sends none of these fields, exactly as
before. `make validate` checks the values against
[the schema](../../schemas/optimization.schema.json).

---

## The settings, per provider

| Provider | Key | Values | What it does |
|----------|-----|--------|--------------|
| `anthropic` | `effort` | `low` · `medium` · `high` · `xhigh` · `max` | How much the model thinks and writes. Claude Sonnet 5.5 defaults to `high` and thinks before almost every reply from `medium` up; at `low` it skips thinking on most simple requests. Claude Haiku 4.5 rejects it. |
| `anthropic` | `thinking` | `adaptive` · `disabled` · `between_tools` | The thinking type. Leave it unset on current models, which think adaptively by default; Claude Sonnet 5.5 and Opus 5.5 reject `disabled`, and `between_tools` is Sonnet 5.5's lowest setting. |
| `anthropic` | `prompt_cache` | `true` | Writes each prompt to Anthropic's cache (see below). |
| `openai` | `reasoning_effort` | `none` · `minimal` · `low` · `medium` · `high` · `xhigh` · `max` | How much the model reasons. GPT-6 Sol and Luna answer tool calls on Chat Completions only at `none`, and only a model reasoning at `none` takes a temperature, so the demo sets `none`. Not every model takes every value. |
| `ollama` | `reasoning_effort` | as above | Ollama's OpenAI-compatible endpoint takes it too: `none` turns thinking off on a thinking model such as `qwen3.5`. |
| `gemini` | `thinking_level` | `minimal` · `low` · `medium` · `high` | How much a Gemini 3.x model thinks. It cannot be turned off; `minimal` is the least, and `gemini-3.8-flash` rejects it. Wins over `thinking_budget`. |
| `gemini` | `thinking_budget` | an integer | The Gemini 2.5 control: a cap on thinking tokens, `0` turning thinking off on 2.5 Flash. |

Thinking tokens are billed as output and come out of the same `max_tokens` as
the reply. A side call sized tightly around its answer, such as a 64-token
compression, can spend all of it thinking and return nothing. That is why the
demos give the `fast` and `summarizer` roles the least thinking their model
allows.

Three things follow from the models, with no setting:

- **Thinking blocks go back unchanged.** When a Claude model thinks before a
  tool call, the next request of the tool loop carries its thinking blocks back
  as they came, signatures included ([ISSUE-0169](../issues/ISSUE-0169-anthropic-adapter-ignores-default-thinking.md)).
- **Temperature goes only where it is accepted.** Claude models from Opus 4.7
  and Sonnet 5 on, OpenAI's reasoning models (unless reasoning at `none`) and
  Gemini 3.x get none; the adapter logs that once per model.
- **OpenAI's reply cap is `max_completion_tokens`**, which reasoning models
  require. A server that only speaks the OpenAI format (any `base_url`:
  Ollama, vLLM, LM Studio) still gets `max_tokens`.

---

## The prompt cache: when it pays

A provider that caches keeps the start of a recent prompt. A later request that
begins with the same bytes reads that part at about a tenth of the input price.
Writing it costs more than a plain read: a quarter more on Anthropic and on
OpenAI from GPT-5.6. So caching pays when prompts repeat within the cache's
lifetime (5 minutes on Anthropic, 30 on OpenAI) and costs extra when they do
not.

| Provider | How it caches | What to set |
|----------|---------------|-------------|
| Anthropic | Only where a request marks it. | `prompt_cache: true` marks the end of the tool definitions and of the system prompt, and the whole request. |
| OpenAI | By itself, on prompts of 1 024 tokens or more. | Nothing. |
| Gemini | By itself, on prompts of 4 096 tokens or more on the 3.x Flash models, with no write charge. | Nothing. |
| watsonx.ai | Not at all. | — |

`prompt_cache: true` saves most on agents that make several calls on one
prompt, such as a task agent's tool loop, where each round reads everything
the round before it wrote. A persona's turn shares less: its system prompt
starts with fixed instructions but carries the current time and that turn's
recalled memory, so the next turn reads only the tool definitions back. By a
rough estimate, with a tool round in about one turn in ten the reads and the
extra write cost about the same. Anthropic does not cache a prompt shorter than the model's minimum,
512 tokens on Claude Sonnet 5.5 and 4 096 on Claude Haiku 4.5, and charges no
write for it.

**Check that it works.** Every call's usage counts cache reads and writes
apart from the uncached input, for Anthropic, OpenAI and Gemini, and the
[call log](../ai-glossary.md#call-log) records them per call. Reads that stay
at zero over calls that should share a prefix mean something at the front of
the prompt changes between them.

The orchestrator's budget does not price the cache: it charges every input
token at the input price, reads and writes alike.

---

## Related

- [Model Providers & Aliases](model-providers.md) — aliases, providers, pricing.
- [ISSUE-0169](../issues/ISSUE-0169-anthropic-adapter-ignores-default-thinking.md) — why the settings are applied per call.
- [Prompt prefix](../ai-glossary.md#prompt-prefix) — the operator's cached text, which every persona turn carries first.
