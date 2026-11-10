---
id: ISSUE-0220
summary: "The Gemini and watsonx SDKs are optional extras that no CI job installs. They are not pinned in the constraints file, not type-checked, not licence-checked and not in the notices file, and their adapters are tested only against stand-in modules. The demo images install them unpinned at build time."
status: open
severity: medium
area: ci
created: 2026-11-10
refs:
  - Makefile
  - agents/pyproject.toml
  - .github/python-constraints.txt
  - agents/llm_gemini.py
  - agents/llm_watsonx.py
  - tests/unit/python/_gemini_test_helpers.py
  - tests/unit/python/_watsonx_test_helpers.py
  - tests/unit/python/test_llm_sdk_boundary.py
  - THIRD_PARTY_NOTICES.md
  - docker-compose.multivendor.yaml
  - docs/issues/ISSUE-0144-anthropic-sdk-pinned-below-1x.md
---

# ISSUE-0220: The Gemini and watsonx SDKs sit outside every gate

## Summary

`google-genai` and `ibm-watsonx-ai` are declared as the optional extras
`gemini`, `watsonx` and `providers` in `agents/pyproject.toml`. The
constraints file is compiled with `--extra dev` only (`UV_COMPILE` in the
Makefile), so neither SDK appears in `.github/python-constraints.txt` or
in any CI install.

## Context

What follows from that:

- **Types.** The adapters' SDK imports carry `type: ignore`; mypy never
  sees the SDKs.
- **Tests.** The adapter tests put `MagicMock` modules into
  `sys.modules` (`_gemini_test_helpers.py`, `_watsonx_test_helpers.py`).
  The two adapters are 768 lines that never meet a real SDK signature.
  The Anthropic and OpenAI adapters do: `test_llm_sdk_boundary.py` runs
  the real SDKs over a mock transport.
- **Licences and notices.** The licence check inspects installed
  packages only, and `THIRD_PARTY_NOTICES.md` names neither SDK.
- **Images.** The `demo-gemini`, `demo-watsonx` and multivendor compose
  files build the agent image with `AGENT_EXTRAS` set, which installs the
  SDKs at whatever version is newest that day.

[ISSUE-0144](ISSUE-0144-anthropic-sdk-pinned-below-1x.md) records what
this failure looks like for an SDK that is tested: a release dropped a
parameter and the adapter had to be pinned back.

## Impact

An SDK release that renames a keyword breaks the four-vendor demo while
CI and the weekly constraints refresh both stay green.

## Proposed fix / investigation path

Compile a second constraints set with the `providers` extra. Run mypy,
the licence check and the weekly refresh once with it installed, and
extend the SDK boundary test to the two adapters.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; confirmed by reading the Makefile, the manifests and the
> test helpers.
