---
id: ISSUE-0075
summary: "`PERSATRIX_OLLAMA_MODEL` overrides the pulled model + every agent (applied in `create_provider`) but NOT the summarization-on-close model, which resolves on the separate `summarize_close` surface (RFC 0020) and follows the `summarizer` alias. So `PERSATRIX_OLLAMA_MODEL=<non-default> make demo-ollama` requests the un-pulled `summarizer`-alias tag and summarization-on-close degrades to its fallback. Lock-step is incomplete; the demo default (no override) is unaffected."
status: open
severity: low
area: agents/llm_factory
created: 2026-05-27
refs:
  - docs/rfcs/0033-model-alias-layer.md
  - docs/rfcs/0020-interaction-lifecycle.md
  - agents/llm_factory.py
  - agents/persona_runtime/summarize_close.py
  - config/demo/ollama/optimization.yaml
---

## Summary

The v0.3.4 knob-free provider-parity refactor (PR #440) replaced
`OllamaProvider`'s call-time `force_model` substitution with a model *override*
applied in `create_provider`: for an `ollama`-routed agent the factory reads
`PERSATRIX_OLLAMA_MODEL` and swaps it for the alias's `model:`. `make demo-ollama`
documents this as keeping the `ollama-pull` model **and every ollama-routed
surface in lock-step**.

It is incomplete. The override is applied **only on the `create_provider`
(agent) surface**. The summarization-on-close path
(`agents/persona_runtime/summarize_close.py`, RFC 0020) resolves its own model
(`summarization_model()` → `summarizer` → `resolve()`) and threads it straight
into `create_message` — it never passes through `create_provider`, so the
override does not reach it. With `PERSATRIX_OLLAMA_MODEL` set to a **non-default**
model, `ollama-pull` pulls that model and the agents use it, but
summarization-on-close requests the `summarizer` alias's `model:` (the demo
ships `llama3.2`), which was never pulled → Ollama 404 → `summarize_close`
degrades to `SUMMARY_UNAVAILABLE_TEXT` (logged, `_emit_summary_failed("llm_error")`,
$0).

This is latent for the shipped demo: the default (no `PERSATRIX_OLLAMA_MODEL`)
pulls `llama3.2` and all three aliases are `llama3.2`, so pull + agents +
summariser all agree. It surfaces only when an operator overrides the model.
It is also a regression of a property that held under the removed forced mode:
the old `OllamaProvider.create_message` substituted `force_model` at the
*provider* level, so the summariser call was covered.

## Detail

`agents/llm_factory.py::create_provider` (agent surface — override applied):

```python
model_override = os.environ.get("PERSATRIX_OLLAMA_MODEL", "").strip()
ollama_model = model_override or physical_model
return OllamaProvider(base_url=resolve_ollama_base_url(provider_config)), ollama_model
```

`agents/persona_runtime/summarize_close.py` (summarization surface — override NOT applied):

```python
summarization_model_ref = summarization_model()          # "summarizer"
resolved_summarization = resolve_model(summarization_model_ref)  # ollama, model="llama3.2"
...
await llm_client.create_message(model=resolved_summarization.model, ...)  # "llama3.2", un-pulled
```

(Sub-agents, the other surface the removed `force_model` docstring named, are
not affected today: `SPAWN_SUB_AGENT` is `not_implemented` in
`action_executor.py`.)

## Open question / candidate dispositions

1. **Apply the override on the ollama summarisation surface too** — a shared
   helper (`apply_ollama_model_override(model)` in `agents/llm_ollama.py`) called
   from both `create_provider` and `summarize_close` after `resolve()` when the
   resolved provider is `ollama`. Restores lock-step; cost: spreads the
   ollama-specific env read into `summarize_close`, which the refactor's
   single-selection-path intent argued against.
2. **Re-add a minimal env-based substitution inside `OllamaProvider.create_message`**
   (reads `PERSATRIX_OLLAMA_MODEL` directly — *not* the removed constructor
   `force_model`). Covers every ollama surface uniformly and keeps the
   `gen_ai.request.model` span honest (it records the actually-run tag), but
   partially reintroduces the in-provider substitution the refactor removed;
   the factory's returned model (RFC 0023 lease / cost key) must stay consistent
   with what the provider sends.
3. **Documentation-only** — scope the "lock-step" wording to the agent surface
   and tell operators that overriding the model also requires matching the
   `summarizer` alias's `model:` in `config/demo/ollama/optimization.yaml`.
   *Applied in PR #440 (this issue's docstrings / demo-config / compose-overlay
   notes).*
4. **Drop `PERSATRIX_OLLAMA_MODEL` as a factory override** and make a model
   change a one-line edit to the demo config's alias `model:` (the release's own
   "one-line config edit" theme), with `ollama-pull` reading the same config.
   Removes the second mechanism; larger change.

No production config is affected today (the shipped demo defaults to `llama3.2`
for the pull and all three aliases). Disposition 3 was applied to remove the
overclaim; whether to also close the behaviour gap (1 / 2 / 4) is left as an
ollama-provider design call. Surfaced by the PR #440 deep review.
