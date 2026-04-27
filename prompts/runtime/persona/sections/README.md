# Persona Prompt Sections

Externalized assets for the persona system prompt. See
[RFC 0022](../../../../docs/rfcs/0022-persona-prompt-section-templating.md)
for the templating contract.

## Section templates

Each section is rendered by the composer in
[`agents/persona_runtime/prompt_assembly.py`](../../../../agents/persona_runtime/prompt_assembly.py)
through Python `str.format_map`. Templates carry no logic — conditional
inclusion is decided by the composer's per-section predicate.

| File                | Always rendered? | Placeholders                         |
| ------------------- | :--------------: | ------------------------------------ |
| `identity.md`       | Yes              | `{name}`, `{title_line}`, `{role}`   |
| `background.md`     | When set         | `{background}`                       |
| `behavior.md`       | When non-empty   | `{behavior}`                         |
| `quirks.md`         | When non-empty   | `{quirks}`                           |
| `goals.md`          | When populated   | `{goals}`                            |
| `current-state.md`  | When non-empty   | `{state}`                            |

`{title_line}` carries its own trailing newline (`"Title: <title>\n"`
or `""`) so the template can keep the placeholder on its own line
without producing a stray blank line when the title is absent.

## Behavioral-dimension descriptions

| File                          | Loaded by                                                           | Purpose                                                                |
| ----------------------------- | ------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `behavior-dimensions.yaml`    | [`agents/prompt_loader.py`](../../../../agents/prompt_loader.py) (`load_dimension_descriptions`) — re-exposed via [`agents/persona_behavior.py`](../../../../agents/persona_behavior.py) | Natural-language descriptions of each persona behavioral dimension/value pair, rendered into the `behavior.md` placeholder. |

## Adding a new section

1. Place the markdown template at `<name>.md` in this directory.
2. Add a `_Section` entry to `_SECTIONS` in
   [`agents/persona_runtime/prompt_assembly.py`](../../../../agents/persona_runtime/prompt_assembly.py)
   with the predicate and context builder.
3. Pin the template body in
   `TestShippedPersonaSectionsByteIdentity`
   ([`tests/unit/python/test_prompt_loader.py`](../../../../tests/unit/python/test_prompt_loader.py)).
4. Update the golden in `TestSystemPromptByteIdentity`
   ([`tests/unit/python/test_persona_section_composer.py`](../../../../tests/unit/python/test_persona_section_composer.py))
   if the new section appears for the canonical test persona.

## Contract for `behavior-dimensions.yaml`

- The YAML must be a mapping `dimension → value → description` where
  every leaf is a string. Other shapes are rejected by
  `load_dimension_descriptions` at import time.
- The set of dimensions and the per-dimension default values must
  match `_DIMENSION_DEFAULTS` in
  [`agents/persona_behavior.py`](../../../../agents/persona_behavior.py).
  Drift is caught at import time so a malformed asset fails loudly
  rather than silently producing an incomplete behavioral prompt.
- A bytes-identical regression guard pins each description in
  [`tests/unit/python/test_dimension_descriptions_loader.py`](../../../../tests/unit/python/test_dimension_descriptions_loader.py)
  under `TestShippedDimensionDescriptionsByteIdentity` — editing a
  string here is a user-visible LLM behavior change, review accordingly.
