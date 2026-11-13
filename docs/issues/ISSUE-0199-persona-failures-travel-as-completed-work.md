---
id: ISSUE-0199
summary: "The persona runtime reports its own failures as a completed task whose result is an English sentence. A workflow step served by a persona succeeds with the text `LLM provider error`, a persona that runs out of model calls posts that diagnostic into its channel, and a provider failure, a cut-off reply or a time-out on a DM publishes nothing, so the person gets a 504."
status: resolved
severity: high
area: agents/persona_runtime
created: 2026-11-10
closed: 2026-11-13
closed_pr: 1108
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
  - agents/persona_types.py
  - tests/unit/python/test_persona_turn_failure.py
  - internal/server/chat_handler.go
  - internal/channels/floor_control.go
  - docs/issues/ISSUE-0239-a-wallet-denial-posts-its-error-sentence-into-a-group-channel.md
  - docs/issues/ISSUE-0240-a-wallet-denial-on-a-personas-workflow-step-fails-as-internal-error.md
  - docs/issues/ISSUE-0244-a-dms-error-reply-is-an-ordinary-message-in-the-channel-record.md
  - agents/tick.py
  - tests/unit/python/test_persona_turn_failure_edges.py
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
| The provider returned nothing | `No LLM response` | `action_loop.py` |
| The agent config names no model | `Agent config missing required 'model' field` | `action_loop.py` |

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
   synthesis, so nothing is published. So do a cut-off reply and an event
   time-out. The chat request waits out its timeout and answers 504. A
   wallet denial on the same path publishes a structured error reply
   ([ISSUE-0065](ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md),
   [ISSUE-0066](ISSUE-0066-chat-rest-resource-exhausted-no-channel-reply.md)
   fixed those two cases only). Both were filed as high, which is why
   this one is.

## Impact

- A workflow keeps going on a failed step, with the error text fed to the
  next step as input.
- An internal diagnostic enters the channel record and fans out to every
  member.
- The most common first-run mistake, a wrong API key, shows as a timeout
  with no reason.

## Proposed fix / investigation path

- Give failure its own carrier, an exception or a distinct failure
  action, so `handle` can return FAILED.
- On a DM, publish the same `reply_status: error` reply the wallet-denial
  path publishes, for every failure in the table.
- Only then stop the budget-exhaustion sentence from being published. On
  a DM that sentence is what answers the chat request today, so removing
  it first would turn that case into a 504 as well.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; all three effects reproduced with scratch probes that drive
> the persona with a failing or looping model double.

> 2026-11-12 — the first two steps of the fix. All three effects were
> reproduced first on `main` at `0f3eb3ac`, on events that carry a
> `channel_id`: every cause in the table ended a workflow step
> `COMPLETED` with its sentence; a DM got nothing for five causes and
> the sentence as an ordinary reply for the sixth; a group channel got
> that sentence, mentioning the sender, when the model calls ran out.
>
> One row of the table is wrong. `No LLM response` is not "the provider
> returned nothing": a provider call returns a response or raises. It
> is what a persona with `max_llm_calls: 0` returns, having made no
> call. A task agent in that state says `Max LLM call iterations
> exceeded`, and so does the persona now.
>
> What changed. A failed turn returns one `DO_NOTHING` that carries the
> cause and a fixed sentence under a private payload key
> (`agents/persona_types.py`); it is not a new action type and never
> reaches the wire. `PersonaAgent.handle` returns `FAILED` with the
> result text `BaseAgent._run_llm_loop` gives for the same cause, which
> changes two of the sentences. On a DM the channel path publishes the
> `reply_status: error` reply a wallet denial gets, with the cause as
> its `error_reason`; the orchestrator's chat handler
> (`internal/server/chat_handler.go`, read and not changed) then answers
> HTTP 200 with `reply_status: "error"` where it answered 504. That
> holds for five of the six causes. For an event time-out the request
> still ends in a 504 under the shipped settings, because the persona's
> time-out (300 seconds) is never shorter than the request's wait (30
> by default, 300 at most), and the error reply is published after it
> ([ISSUE-0244](ISSUE-0244-a-dms-error-reply-is-an-ordinary-message-in-the-channel-record.md)).
> A tick whose turn failed is not an idle tick, as before, so a persona
> goes on trying through a provider outage; a tick that times out, or
> that the wallet denies, was already an idle one and still is. Whether
> a failed turn is recorded in memory is as it was: only one that ran
> out of model calls is, and now without the sentence standing in for a
> reply.
>
> What remains is the third step: on a group channel a persona that
> runs out of model calls still posts `Max LLM call budget exhausted
> after N iterations` as an ordinary message. The other five causes post
> nothing there, as before, and each now logs a WARNING with its cause.
>
> Two things seen beside it and filed:
> [ISSUE-0239](ISSUE-0239-a-wallet-denial-posts-its-error-sentence-into-a-group-channel.md)
> and
> [ISSUE-0240](ISSUE-0240-a-wallet-denial-on-a-personas-workflow-step-fails-as-internal-error.md).

> 2026-11-13 — the third step, and the issue is resolved. The maintainer
> ruled on the question the second note left open: a group channel is
> posted nothing when a persona's turn fails. Running out of model calls
> is now a failed turn on every surface, so the sentence it used to post
> to a group channel is gone; the turn is logged at WARNING with its
> cause like the other five, and is still recorded in memory, with no
> reply. Reproduced first on the step-two code: that one cause posted
> `Max LLM call budget exhausted after 3 iterations` to `general`,
> mentioning the sender.
>
> The cost, read in `internal/channels/` and not changed: in a
> floor-control round a speaker that posts nothing holds the floor until
> its turn times out (`floor_turn_timeout_seconds`, 45 by default),
> where the sentence used to end its turn at once and count as the
> round's reply. That was already so for the other five causes.
>
> Two more things change with the sentence, both run on the two trees.
> On the synthesis turn that closes a discussion, a chair that ran out
> of model calls used to post the sentence with the synthesis claim an
> ordinary reply carries there, so the orchestrator took it for the
> closing synthesis. It now posts nothing, and the close waits for the
> synthesis reply timeout (120 seconds,
> `internal/channels/synthesis_close.go`, read and not run) and then
> closes without one. And the turn no longer drains the persona's
> energy: energy is drained once for each action a turn ends with, the
> sentence was one, and a failed turn ends with none, as for the other
> five causes.
>
> The wallet-denial path still posts its sentence on a group channel.
> The same ruling covers it, and
> [ISSUE-0239](ISSUE-0239-a-wallet-denial-posts-its-error-sentence-into-a-group-channel.md)
> stays open for that change.
