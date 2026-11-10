---
id: ISSUE-0199
summary: "The persona runtime reports its own failures as a completed task whose result is an English sentence. A workflow step served by a persona succeeds with the text `LLM provider error`, a persona that runs out of model calls posts that diagnostic into its channel, and a provider failure on a DM publishes nothing, so the person gets a 504."
status: open
severity: medium
area: agents/persona_runtime
created: 2026-11-10
refs:
  - agents/persona.py
  - agents/persona_runtime/action_loop.py
  - agents/persona_runtime/llm_call_errors.py
  - agents/persona_runtime/channel_reply.py
  - agents/persona_runtime/__init__.py
  - agents/chat_reply.py
  - tests/unit/python/test_persona_agent_factory.py
  - docs/issues/ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md
  - docs/issues/ISSUE-0066-chat-rest-resource-exhausted-no-channel-reply.md
---

# ISSUE-0199: A persona's own failures travel as completed work

## Summary

When the persona's action loop fails, it returns a `COMPLETE_TASK` action
whose `result` is a sentence describing the failure. Nothing downstream
can tell that sentence from a real answer. Three callers treat it as one.

## Context

The failure sentences, all carried as `COMPLETE_TASK`:

| Cause | Result text | Where |
|---|---|---|
| The provider call raised | `LLM provider error` | `llm_call_errors.py` |
| The reply was cut off | `Response truncated: max_tokens limit reached` | `action_loop.py` |
| The call budget ran out mid tool use | `Max LLM call budget exhausted after N iterations` | `action_loop.py` |
| The event timed out | `Event processing timed out after Ns` | `persona_runtime/__init__.py` |

What each caller then does:

1. **A workflow step.** `PersonaAgent.handle` (`agents/persona.py`) maps
   any `COMPLETE_TASK` to `TaskStatus.COMPLETED`. With the provider
   raising, a probe got `status=COMPLETED result='LLM provider error'`.
   The step succeeds, no retry fires, and the sentence becomes the step's
   output. `BaseAgent._run_llm_loop` returns FAILED for the same causes.
2. **A channel message.** On budget exhaustion the loop builds a response
   from the diagnostic and parses it as usual. `synthesize_channel_reply`
   then turns the `COMPLETE_TASK` into a channel publish. On a group
   channel with `max_llm_calls=3` the probe got a publish of that exact
   sentence, mentioning the sender. The existing test uses an event with
   no `channel_id`, so it never sees the publish.
3. **A DM.** A provider error that is not wallet back-pressure (a wrong
   API key, an unknown model, a network failure) returns before reply
   synthesis, so nothing is published. The chat request waits out its
   timeout and answers 504. A wallet denial on the same path publishes a
   structured error reply
   ([ISSUE-0065](ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md),
   [ISSUE-0066](ISSUE-0066-chat-rest-resource-exhausted-no-channel-reply.md)
   fixed those two cases only).

## Impact

- A workflow keeps going on a failed step, with the error text fed to the
  next step as input.
- An internal diagnostic enters the channel record and fans out to every
  member.
- The most common first-run mistake, a wrong API key, shows as a timeout
  with no reason.

## Proposed fix / investigation path

Give failure its own carrier, an exception or a distinct failure action,
so `handle` can return FAILED. Return before reply synthesis on budget
exhaustion, as the truncation branch already does. On a DM, publish the
same `reply_status: error` reply the wallet-denial path publishes.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; all three effects reproduced with scratch probes that drive
> the persona with a failing or looping model double.
