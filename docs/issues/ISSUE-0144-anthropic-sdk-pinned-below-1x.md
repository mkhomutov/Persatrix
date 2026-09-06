---
id: ISSUE-0144
summary: "The Anthropic SDK is capped below 1.x because `messages.create` dropped the `temperature` parameter that `AnthropicProvider` passes on every call — and the Dependabot rule that stops the monthly sweep re-proposing the widening also removes the only recurring prompt to do the port, so the tree sits on a superseded SDK line with nothing scheduled to move it"
status: open
severity: medium
area: agents
created: 2026-09-07
refs:
  - agents/llm_providers.py
  - agents/pyproject.toml
  - .github/dependabot.yml
---

## Summary

`agents/pyproject.toml` pins `anthropic>=0.40.0,<1`. The cap is load-bearing:
`AnthropicProvider.create_message` passes `temperature` on every call, and
`messages.create` in the 1.x line does not accept it. Porting the provider is
the work; the cap and the Dependabot `ignore` rule are only holding the line
until someone does it.

## Context

Found reviewing the first pip dependency sweep,
[PR #870](https://github.com/mkhomutov/Persatrix/pull/870), which proposed
widening the cap to `<2` along with five other upper bounds. Probing the
version that widening admits (1.4.0) directly:

```
AsyncMessages.create() got an unexpected keyword argument 'temperature'
```

`messages.create` no longer declares `temperature` and has no `**kwargs`, so
the call `AnthropicProvider.create_message` builds raises `TypeError` before a
request is sent. The kwargs dict is unconditional — `temperature` is not
behind a truthiness guard the way `system` and `tools` are:

```python
kwargs: dict[str, Any] = {
    "model": model,
    "messages": messages,
    "max_tokens": max_tokens,
    "temperature": temperature,
}
```

**CI reported the widening green.** That is the part worth recording, because
it will hold for the next SDK major too:
`tests/unit/python/test_llm_client.py` constructs `AnthropicProvider(...)`,
which still succeeds because the *constructor* is unchanged, and mocks the
client for everything after. No test in the suite reaches
`messages.create` against the real library, so a passing Python job carries no
information about this class of break. The same shape applies to
`OpenAIProvider`.

The other three widenings in that sweep were checked the same way and kept —
`openai` 3.8.0, `structlog` 26.1.0 and `google-genai` 2.22.0 all preserve the
surface this tree touches. `anthropic` was the only one that did not.

## Impact

Nothing is broken today; the cap holds and every Anthropic call works. Two
things degrade over time:

- **The runtime sits on a superseded SDK major.** Fixes and new API surface
  land on 1.x. The 0.x line will stop receiving them, and Anthropic is the
  default provider for the fleet, so this is not a peripheral dependency.
- **Nothing is scheduled to notice.** The `ignore` rule added in
  [PR #876](https://github.com/mkhomutov/Persatrix/pull/876) was the right call
  for the monthly sweep — refusing the same PR from memory every month is not a
  control — but it removes the recurring prompt along with the noise. Without
  this file, the only things pointing at the work are two config comments that
  a reader reaches only if they are already editing those files.

## Proposed fix / investigation path

One change, three edits that must land together:

1. Port `AnthropicProvider.create_message` (`agents/llm_providers.py`) to
   whatever 1.x replaced `temperature` with, and re-check `_normalize` against
   the 1.x response shape — it reads `response.content[].type/.text/.id/
   .name/.input`, `response.stop_reason`, and
   `response.usage.input_tokens/.output_tokens`, none of which were checked
   here beyond the constructor.
2. Lift the cap to `<2` in `agents/pyproject.toml`.
3. Drop the `anthropic` entry from `ignore:` in `.github/dependabot.yml`.

Worth pairing with it: a test that exercises a provider's `create_message`
against the real SDK rather than a mocked client. The gap this issue records
is not specific to `temperature` — it is that the boundary where the tree
meets a vendor SDK has no coverage at all, so any signature change in any
provider arrives silently and green.

## Notes

> 2026-09-07 — captured after [PR #876](https://github.com/mkhomutov/Persatrix/pull/876)
> merged. The `ignore` rule is deliberate and should stay until the port is
> done; this file exists so the port is discoverable somewhere other than a
> comment in the file that suppresses the reminder. Not yet slotted to a
> version — a candidate to lock or defer at the v0.3.16 plan opening.
