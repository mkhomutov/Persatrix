---
id: ISSUE-0220
summary: "The Gemini and watsonx SDKs are optional extras that no CI job installs. They are not pinned in the constraints file, not type-checked, not licence-checked and not in the notices file, and their adapters are tested only against stand-in modules. The demo images install them unpinned at build time."
status: resolved
severity: medium
area: ci
created: 2026-11-10
closed: 2026-11-14
closed_pr: 1118
refs:
  - .github/python-constraints-providers.txt
  - .github/workflows/ci.yml
  - .github/workflows/python-constraints-refresh.yml
  - tests/unit/python/test_llm_sdk_boundary_gemini.py
  - tests/unit/python/test_llm_sdk_boundary_watsonx.py
  - tests/unit/python/test_provider_sdk_gates.py
  - scripts/generate_third_party_notices.py
  - scripts/checks/allowed_licenses.txt
  - docs/issues/ISSUE-0219-agent-image-is-not-what-ci-tests.md
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
  SDKs at the newest version inside the declared range that day.

[ISSUE-0144](ISSUE-0144-anthropic-sdk-pinned-below-1x.md) records what
this failure looks like: an Anthropic SDK release dropped a parameter the
adapter passes, the mocked tests stayed green, and the SDK is held below
1.x. That issue already lists the Gemini provider as outside the SDK
boundary test. This one takes that over and adds watsonx and the other
gates.

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

> 2026-11-14 — in progress: every gate but one is in place
> ([#1118](https://github.com/mkhomutov/Persatrix/pull/1118)). What changed:
>
> - **Pinned.** `make python-constraints` writes a second file,
>   `.github/python-constraints-providers.txt`, with the same uv and flags
>   as the first plus the `providers` extra. It is compiled against the
>   first file, so the two agree on every package they share: 84 pins in
>   both, 24 more for the SDKs. `make python-constraints-check` and the
>   weekly refresh cover both.
> - **Installed and type-checked.** The last three steps of CI's Python
>   job install the extra from that file (`make build-agents-providers`),
>   then run mypy over the two adapters with no config
>   (`make lint-providers`), so a missing or untyped SDK is an error. The
>   `type: ignore` comments on the SDK imports are gone.
> - **Tested against the real SDKs.** `make test-providers` runs both
>   adapters with `google-genai` and `ibm-watsonx-ai` imported for real
>   and the network replaced. The tests skip where the SDKs are
>   absent and are made to fail there instead of skip.
> - **In the notices.** `THIRD_PARTY_NOTICES.md` is regenerated from an
>   install with every extra, and the generator refuses any other.
>
> What the real SDKs showed: **no fault in either adapter.** Every keyword
> the two adapters pass is accepted by `google-genai` 2.34.0 and
> `ibm-watsonx-ai` 1.9.1, and every response field they read is there.
> Three things the stand-in modules could not have shown:
>
> - The watsonx SDK cannot build its client without the network: it asks
>   for an IAM token, the project and the model list first. The tests
>   answer those three calls themselves.
> - The Gemini SDK refuses an empty message list ("contents are
>   required"); the stand-ins accepted one. Every caller read in
>   `agents/` starts from a user message, so none sends one.
> - On Vertex AI the Gemini SDK looks for Google credentials on the
>   network at the first request, so only building that client is tested.
>
> **What remains, and why this issue is not resolved:**
>
> - **The license check with the SDKs installed.** It refuses one
>   package: `cffi` 2.1.1, license `MIT-0`, which `google-genai` installs
>   through `google-auth` and `cryptography`. `MIT-0` is not on
>   `scripts/checks/allowed_licenses.txt`. Adding it, or excepting the
>   package, is the maintainer's choice, so CI does not run
>   `make check-licenses-python` with the SDKs installed yet. Once the
>   choice is made it is one more step after `make test-providers`. The
>   regenerated notices file marks the row `!MIT-0`.
> - **The demo images** still install the SDKs unpinned through
>   `AGENT_EXTRAS`. That half is left with
>   [ISSUE-0219](ISSUE-0219-agent-image-is-not-what-ci-tests.md), which
>   holds the choices about the image; the second constraints file is
>   there for its install to use.

> 2026-11-14 — resolved. The maintainer allowed `MIT-0`, so the one gate
> that was missing is in place:
>
> - `MIT-0` is on `scripts/checks/allowed_licenses.txt`, with its reason.
>   It is the MIT license without the condition that the notice travel
>   with copies. `deny.toml` does not mirror it, because no Rust crate
>   needs it: two (`aws-lc-sys`, `dunce`) offer it as one choice beside
>   licenses the Rust list already allows. The same list feeds the Go
>   check, where no module uses it.
> - A fourth step after `make test-providers`, in CI's Python job and in
>   the weekly refresh, runs `make check-licenses-python` where the SDKs
>   are installed: 100 packages, all on the allow-list. Before the choice
>   the same command failed on `cffi` alone.
> - The notices file no longer marks the `cffi` row.
>
> The "What remains" list in the note above is now down to the demo
> images, which stay with
> [ISSUE-0219](ISSUE-0219-agent-image-is-not-what-ci-tests.md).

> 2026-11-14 — after the review of #1118. Nothing above is undone. Three
> statements are narrowed, three gaps have issues of their own, and three
> things changed:
>
> - "Type-checked" means mypy sees three calls: the two that build a
>   Gemini client and the one that builds the watsonx credentials. Both
>   clients are untyped to it after that, so a wrong keyword on a later
>   call is caught by the tests, not by mypy.
> - "The network replaced" is not "as the product runs" for Gemini: the
>   tests send through the SDK's httpx client and a running agent sends
>   through aiohttp
>   ([ISSUE-0252](ISSUE-0252-gemini-sdk-tests-do-not-run-the-aiohttp-path-the-agents-use.md)).
> - "No fault in either adapter" is about successful calls. Neither
>   adapter is tested on an error reply or a retry
>   ([ISSUE-0253](ISSUE-0253-provider-sdk-errors-and-retries-are-untested.md)).
> - The Python job now builds three source-only packages with a build
>   tool the pins do not reach
>   ([ISSUE-0254](ISSUE-0254-the-python-job-builds-three-ibm-packages-with-an-unpinned-build-tool.md)).
> - Changed: a third test that needs the Gemini SDK (it asks the SDK to
>   accept a tool's schema) skipped in every CI run. It now runs under
>   `make test-providers`, and a test holds that every such test does.
> - Changed: the fourth step is `make check-licenses-providers`, the same
>   check under its own name, so a failure line says which run failed.
> - Changed: the notices generator refuses any Python but 3.11. Some pins
>   differ by Python version, so another version lists other releases.

