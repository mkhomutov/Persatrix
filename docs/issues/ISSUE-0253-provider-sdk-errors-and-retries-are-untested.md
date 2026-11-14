---
id: ISSUE-0253
summary: "No test shows what the Gemini or watsonx adapter does when the provider answers with an error. The real watsonx SDK retries a rate-limit reply ten times, about a minute, inside a worker thread the adapter cannot cancel, and the adapter never chose that number. The Gemini SDK raises at once."
status: open
severity: low
area: agents
created: 2026-11-14
refs:
  - https://github.com/mkhomutov/Persatrix/pull/1118
  - agents/llm_watsonx.py
  - agents/llm_gemini.py
  - tests/unit/python/test_llm_sdk_boundary_watsonx.py
  - tests/unit/python/test_llm_sdk_boundary_gemini.py
  - docs/issues/ISSUE-0220-gemini-and-watsonx-sdks-sit-outside-every-gate.md
---

# ISSUE-0253: Provider SDK errors and retries are untested

## Summary

The tests that run the Gemini and watsonx adapters against their real
SDKs answer every call with a success
([ISSUE-0220](ISSUE-0220-gemini-and-watsonx-sdks-sit-outside-every-gate.md)).
None shows what happens when the provider answers with an error: how many
requests the SDK sends, how long the call takes, and what the adapter's
caller gets.

The two SDKs behave very differently, and the watsonx behaviour is one
nobody chose.

## Context

Found in the review of #1118 and measured again there, with the real SDKs
and canned error replies (no network).

**watsonx** (`ibm-watsonx-ai` 1.9.1). `ModelInference` retries by itself.
Its own documentation gives the defaults: ten retries, for the statuses
429, 503, 504 and 520, waiting `0.5 s × 2^attempt` between them. The
adapter builds `ModelInference` without `max_retries`, so it takes them.

| The service answers | Chat requests sent | Time before the error surfaces |
|---|---|---|
| 429 (rate limit) | 11 | 63.4 s |
| 401 | 4 | 2.1 s |
| 500 | 4 | 2.1 s |

The adapter runs the blocking `chat` call through `asyncio.to_thread`. A
thread started that way cannot be stopped: if the awaiting turn times out
or is cancelled, the thread keeps retrying to the end. The threads come
from the event loop's default pool, which is small (twelve on the machine
the probe ran on).

**Gemini** (`google-genai` 2.34.0). One request, then
`google.genai.errors.ClientError` (429, 401) or `ServerError` (500, 503)
at once. Measured on the SDK's httpx path; the aiohttp path the agents
use is not covered by any test
([ISSUE-0252](ISSUE-0252-gemini-sdk-tests-do-not-run-the-aiohttp-path-the-agents-use.md)).

## Impact

Proposed low: both SDKs are optional extras, and no fault was observed in
a running system. What can go wrong:

- A rate-limited watsonx agent sends ten more requests into the limit and
  holds a worker thread for a minute per call. Several agents doing so at
  once can use up the pool, and other work that runs in threads waits.
- A turn that gave up still has a thread spending requests for it.
- A release of either SDK that changes its retry defaults or its error
  classes passes every check.

The severity is proposed here, not ruled.

## Proposed fix / investigation path

1. Decide the watsonx retry count on purpose. `ModelInference` takes
   `max_retries`, `delay_time` and `retry_status_codes`; pass what the
   agents should have, or keep the SDK's defaults and say so in the
   adapter.
2. Add error replies to both SDK test files: at least a rate limit and a
   server error each. Assert the number of requests sent and the error
   the caller gets.
3. Read what the callers of `create_message` do with each error, so the
   SDK's retries are not multiplied by a retry above them.

## Notes

> 2026-11-14 — filed from the review of #1118. Both SDK test files list
> errors and retries as not covered.
