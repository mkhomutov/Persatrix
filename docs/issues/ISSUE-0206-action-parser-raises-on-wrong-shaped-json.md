---
id: ISSUE-0206
summary: "The persona's action parser falls back to plain text only when the model's reply is not JSON. Valid JSON of the wrong shape, such as a single action object inside a json fence, a list of strings, or a payload that is a string or null, raises `AttributeError` or `TypeError`, and the turn is dropped after the model call was paid for."
status: resolved
severity: medium
area: agents/persona_runtime
created: 2026-11-10
closed: 2026-11-13
closed_pr: 1105
refs:
  - tests/unit/python/test_action_parser_wrong_shapes.py
  - tests/unit/python/test_action_parser_json_sweep.py
  - docs/issues/ISSUE-0238-action-payload-values-of-the-wrong-type-fail-after-the-parser.md
  - agents/persona_runtime/action_parser.py
  - agents/persona_runtime/action_validation.py
  - agents/persona_runtime/channel_reply.py
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
  list (the same object with no fence falls back correctly);
- a list of strings or numbers;
- a `send_channel_message`, `delegate` or `spawn_sub_agent` action whose
  `payload` is a string or `null`.

Two neighbours of these:

- For the other action types the parser passes a non-object payload on,
  and the failure comes later, in reply synthesis
  (`agents/persona_runtime/channel_reply.py`).
- A fenced number or a fenced `null` raises `TypeError`.

The error leaves the persona's event handler. The channel path logs it at
its outer boundary and publishes nothing, and the inbound message is not
stored as an episode.

## Impact

The person or persona that wrote the message gets no reply, the model
call is already paid for, and the turn leaves no memory. Smaller and
local models produce these shapes more often than the large hosted ones.

## Proposed fix / investigation path

Check that the parsed value is a list, then `isinstance(..., dict)` for
each item and each payload, and fall back to the raw-text `COMPLETE_TASK`
the docstring promises. Add the shapes above to the parser's tests.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; reproduced with a scratch probe that calls the parser.

> 2026-11-12 — fixed. Every shape listed above was reproduced on `main`
> at `0f3eb3ac` and raised as described, with one limit on the payload
> shapes: a payload that is not an object raised for five action types
> (`send_channel_message`, `delegate` and `spawn_sub_agent` in the
> validator, `complete_task` and `end_interaction_vote` later, in reply
> synthesis). For `do_nothing`, `use_tool` and the three approval types
> it did not raise: the turn did nothing, so a group channel got no
> message and a DM the `…` placeholder. Shapes this text does not
> list raised too: a fenced `true` or a fenced JSON string; a list
> holding a good action beside a string, a number, `null` or another
> list; `Infinity` or `1e999` in a `spawn_sub_agent` limit
> (`OverflowError`); and JSON nested deeper than the decoder follows
> (`RecursionError`, at 100,000 levels). An `action_type` that is not a
> string never raised: it was already skipped as unknown.
>
> The rule now: a reply that is not a list, or a list with no usable
> action, becomes the raw-text `COMPLETE_TASK`. Inside a list, an item
> that is not an object, or whose payload is not an object, is dropped
> with a warning and the other items are kept, as an item with an
> unknown `action_type` always was. One exception, added after review:
> a `do_nothing` the model wrote out is kept as silence whatever its
> payload holds, because the first version of this fix dropped it and
> posted the raw JSON where `main` had stayed silent. The tool and
> approval types with such a payload are dropped like any other
> unusable item, which for a reply holding nothing else means its text
> is posted where `main` did nothing. The validator replaces a
> non-object payload with `DO_NOTHING` for every action type, so it
> cannot raise for another caller.
>
> Dropping one item and keeping the rest has a cost that only the log
> line records: the words of a dropped message are lost, and a vote can
> be published without the synthesis meant to go with it. One action object in a fence is not
> wrapped into a list: the same object with no fence is raw text, and
> a fence should not change what a reply means.
>
> Not fixed here: an object payload whose values have the wrong type,
> such as a `result` that is a number. It passes the parser and fails
> in two consumers. That is
> [ISSUE-0238](ISSUE-0238-action-payload-values-of-the-wrong-type-fail-after-the-parser.md).

> 2026-11-13 — seen during review and filed apart, because it is on
> `main` and does not depend on the reply's shape: a persona's workflow
> step stores no episode at all
> ([ISSUE-0241](ISSUE-0241-a-personas-workflow-step-stores-no-episode.md)).
