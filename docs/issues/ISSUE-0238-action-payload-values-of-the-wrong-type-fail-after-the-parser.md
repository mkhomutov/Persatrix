---
id: ISSUE-0238
summary: "A persona action whose payload is an object, but holds a value of the wrong type, passes the parser and goes wrong later. On a workflow step a `complete_task` whose `result` is a number, an object or a list raises `TypeError` when the task response is built, and one holding a lone surrogate raises `UnicodeEncodeError`. On a channel a `result` that is not a string is discarded without a log line: a DM gets the `…` placeholder and a group channel nothing. The legacy chat RPC raises on such a `result` and on `mentions` that is a number or `null`."
status: open
severity: medium
area: agents/persona_runtime
created: 2026-11-12
refs:
  - agents/persona_runtime/action_validation.py
  - agents/chat_reply.py
  - agents/persona.py
  - agents/server_servicers.py
  - agents/action_executor.py
  - agents/persona_runtime/channel_reply.py
  - docs/issues/ISSUE-0206-action-parser-raises-on-wrong-shaped-json.md
  - https://github.com/mkhomutov/Persatrix/pull/1105
---

# ISSUE-0238: Action payload values of the wrong type fail after the parser

## Summary

`validate_action_payload` (`agents/persona_runtime/action_validation.py`)
checks the fields of three action types and puts no constraint on the
rest. A `complete_task` may carry any JSON value as its `result` or
`metadata`, and a `send_channel_message` any value as its `mentions`.
Some of the code that reads those values assumes a string, a list or an
object.

## Context

Reproduced on `main` at `0f3eb3ac`, by parsing a reply and handing the
actions to each consumer, and by running whole turns:

| Reply | Where | Result |
|---|---|---|
| `complete_task` with `result` 42, `{"a": 1}` or `["x"]` | A workflow step: `task_pb2.TaskResponse(result=…)`, as `agents/server_servicers.py` builds it from `PersonaAgent.handle` | `TypeError: bad argument type for built-in operation` |
| `complete_task` whose `result` holds a lone surrogate such as `\ud800` | The same | `UnicodeEncodeError` |
| `complete_task` with `metadata` `"x"` | The same, at `.items()` | `AttributeError` |
| `complete_task` with `result` 42, `null`, `{"a": 1}` or `["x"]` | A channel message | No error and no log line: a DM gets the `…` placeholder, a group channel nothing |
| `complete_task` with `result` 42, `null`, `{"a": 1}` or `["x"]` | `extract_chat_reply` (`agents/chat_reply.py`) | `TypeError: expected string or bytes-like object` |
| `send_channel_message` with `mentions` 5 or `null` | `extract_chat_reply` | `TypeError: argument of type 'int' is not iterable` |

`extract_chat_reply` has one caller, the `SendChatMessage` RPC handler
in `agents/server_servicers.py`. The REST chat endpoint does not use
that RPC: a chat request travels as a channel message on a DM. So the
last two rows are reached only by a caller of that RPC, and what a
person chatting sees is the fourth row.

On a channel the other readers guard: reply synthesis
(`agents/persona_runtime/channel_reply.py`) ignores a `result` that is
not a string, the action executor (`agents/action_executor.py`) coerces
`mentions` that is not a list to an empty one, and the end-vote path
wraps its fields in `str()`. A `send_channel_message` whose `content` is
not a string is already replaced with `DO_NOTHING` by the validator.

One more shape reads as silence with no log line: a list of objects
none of which names an `action_type` becomes that many `do_nothing`
actions, because a missing `action_type` defaults to it.

`mentions` that is a string does not raise in `extract_chat_reply`, and
is wrong in a quieter way: `user_id in mentions` becomes a substring
test.

## Impact

The same as [ISSUE-0206](ISSUE-0206-action-parser-raises-on-wrong-shaped-json.md)
before its fix, one level down: the model call is paid for and the turn
fails on a workflow step, or is answered with a placeholder or with
nothing on a channel, where the model had written an answer. Rated
medium by likeness to ISSUE-0206; the severity is the maintainer's to
set.

## Proposed fix / investigation path

Decide, per field, between coercing and rejecting, and do it once in the
validator so every reader gets the type it assumes. A `result` that is
an object or a list is plausibly a structured answer that should become
its JSON text; `null` is plausibly an empty answer. Those are choices,
which is why this was filed and not folded into ISSUE-0206's fix.

## Notes

> 2026-11-12 — found while fixing
> [ISSUE-0206](ISSUE-0206-action-parser-raises-on-wrong-shaped-json.md),
> by running generated JSON replies through the parser's consumers.

> 2026-11-13 — corrected after review of the pull request that filed
> this. The table had every row as an error. Two of its rows are
> reached only through the `SendChatMessage` RPC; on a channel the same
> reply is discarded quietly; a lone surrogate was missing; and so was
> the list of objects with no `action_type`.
