---
id: ISSUE-0206
summary: "The persona's action parser falls back to plain text only when the model's reply is not JSON. Valid JSON of the wrong shape, such as a single action object, a list of strings, or a payload that is a string or null, raises `AttributeError`, and the turn is dropped after the model call was paid for."
status: open
severity: medium
area: agents/persona_runtime
created: 2026-11-10
refs:
  - agents/persona_runtime/action_parser.py
  - agents/persona_runtime/action_validation.py
---

# ISSUE-0206: The action parser raises on valid JSON of the wrong shape

## Summary

`parse_actions` (`agents/persona_runtime/action_parser.py`) expects a
JSON list of objects, each with an `action_type` and a `payload` object.
Its docstring promises that anything else becomes a `COMPLETE_TASK`
carrying the raw text. The fallback catches only a JSON decode error or a
`ValueError`. The loop then calls `raw.get(...)` on each item, and
`validate_action_payload` calls `p.get(...)` on the payload, with no check
that either is an object.

## Context

Replies that raised `AttributeError` in a probe:

- one action object inside a ```` ```json ```` fence, not wrapped in a
  list;
- a list of strings or numbers;
- an action whose `payload` is a string or `null`.

The error leaves the persona's event handler. The channel path logs it at
its outer boundary and publishes nothing, and the inbound message is not
stored as an episode.

## Impact

The person or persona that wrote the message gets no reply, the model
call is already paid for, and the turn leaves no memory. Smaller and
local models produce these shapes more often than the large hosted ones.

## Proposed fix / investigation path

Check `isinstance(..., dict)` for each item and each payload, and fall
back to the raw-text `COMPLETE_TASK` the docstring promises. Add the
three shapes above to the parser's tests.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; reproduced with a scratch probe that calls the parser.
