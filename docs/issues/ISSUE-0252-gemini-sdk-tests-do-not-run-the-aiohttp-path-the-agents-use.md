---
id: ISSUE-0252
summary: "The tests that run the Gemini adapter against the real `google-genai` SDK switch the SDK to its httpx client, because that is the client a canned reply can be put behind. A running agent sends through aiohttp. A break in the SDK's aiohttp path, or between it and the `aiohttp` release the weekly refresh moves, passes every check."
status: open
severity: low
area: tests
created: 2026-11-14
refs:
  - https://github.com/mkhomutov/Persatrix/pull/1118
  - tests/unit/python/test_llm_sdk_boundary_gemini.py
  - tests/unit/python/_sdk_boundary_helpers.py
  - agents/llm_gemini.py
  - agents/pyproject.toml
  - .github/python-constraints-providers.txt
  - docs/issues/ISSUE-0220-gemini-and-watsonx-sdks-sit-outside-every-gate.md
---

# ISSUE-0252: Gemini SDK tests do not run the aiohttp path the agents use

## Summary

`make test-providers` runs the Gemini adapter against the real
`google-genai` SDK with the network replaced
([ISSUE-0220](ISSUE-0220-gemini-and-watsonx-sdks-sit-outside-every-gate.md)).
To replace the network the tests switch the SDK to its httpx client. A
running agent does not use that client: the SDK sends async requests
through aiohttp whenever aiohttp is installed, and the agents always
install it.

So the part of the SDK that puts a request on the wire in production, and
reads the reply off it, is run by no test.

## Context

Found in the review of #1118, and checked again there.

- `google-genai` 2.34.0 decides in `_use_aiohttp()` (`_api_client.py`):
  aiohttp is used when it can be imported and the caller gave no httpx
  async client.
- `aiohttp` is a base dependency of the agents (`agents/pyproject.toml`),
  pinned to 3.14.5 in the constraints files on 2026-11-14.
- The adapter builds `genai.Client(api_key=…)` with no HTTP options, so
  every agent takes the aiohttp path.
- The test helper `_gemini_against` sets
  `api._http_options.httpx_async_client` on the client the adapter built.
  With sockets refused, `_use_aiohttp()` answers `True` for the adapter as
  built and `False` after that line.

What the tests do hold: the SDK builds the request and parses the reply in
code shared by both paths, so a renamed keyword or a missing response
field, the fault ISSUE-0220 names, goes red.

What they do not hold: with aiohttp's own request method made to raise on
every call, all ten Gemini SDK tests still pass. The review did the same
inside the SDK and saw mypy and the stand-in tests stay green as well.

## Impact

Proposed low. The Gemini SDK is an optional extra, and the fault needs a
release of `google-genai` or `aiohttp` that breaks their meeting point.
But the weekly refresh moves both pins, and it would offer such a pair as
green. The first sign would be a Gemini agent failing its first model
call. The severity is proposed here, not ruled.

## Proposed fix / investigation path

Either way the reply has to be given to aiohttp, not to httpx:

1. **A session of the test's own.** The SDK takes a caller's session
   (`HttpOptions.aiohttp_client`). A test can hand it an
   `aiohttp.ClientSession` whose connector answers from memory. The
   adapter gives no HTTP options today, so the test would set the option
   on the built client, as it sets the httpx one now.
2. **A server on the loopback address.** Point the SDK's base URL at a
   small server started by the test. This runs the whole aiohttp path,
   connector included. The network guard in `_sdk_boundary_helpers.py`
   refuses every lookup and connection, so it would have to let the
   loopback address through, and nothing else.

Keep the httpx tests or move them: the request and reply shapes they pin
do not depend on the transport.

## Notes

> 2026-11-14 — filed from the review of #1118. The test file's docstring,
> the Makefile comment and the enforcement matrix name this gap.
