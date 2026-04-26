# Persona Prompt Sections

Externalized assets for the persona system prompt.

## Current files

| File                          | Loaded by                                                           | Purpose                                                                |
| ----------------------------- | ------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `behavior-dimensions.yaml`    | [`agents/prompt_loader.py`](../../../../agents/prompt_loader.py) (`load_dimension_descriptions`) — re-exposed via [`agents/persona_behavior.py`](../../../../agents/persona_behavior.py) | Natural-language descriptions of each persona behavioral dimension/value pair, rendered into the persona system prompt. |

## Reserved for follow-up

The bulk of the persona system prompt (identity / background / goals /
current-state) is still assembled inline in
[`agents/persona_runtime/prompt_assembly.py`](../../../../agents/persona_runtime/prompt_assembly.py).
Templated section files (`identity.md`, `background.md`, `goals.md`,
`current-state.md`) live behind PR C of the prompt-externalization
follow-up plan, which needs an RFC for the templating syntax. See
[`docs/prompt-organization.md`](../../../../docs/prompt-organization.md)
for the migration rules.

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
