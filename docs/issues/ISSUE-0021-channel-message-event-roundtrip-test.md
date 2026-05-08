---
id: ISSUE-0021
summary: No proto round-trip serialization test for ChannelMessageEvent; field-number renumber accidents would not be caught
status: resolved
severity: low
area: build/proto
created: 2026-05-04
closed: 2026-05-08
refs:
  - proto/task.proto
  - tests/unit/python/test_receive_channel_message.py
  - tests/unit/python/test_channel_message_event_roundtrip.py
  - internal/channels/proto_roundtrip_test.go
---

## Summary

PR #246 added `ChannelMessageEvent` (8 fields) and `TaskAck` (2 fields)
to `proto/task.proto`. The accompanying test
[tests/unit/python/test_receive_channel_message.py](../../tests/unit/python/test_receive_channel_message.py)
pins the stub's `success=False` contract but does not exercise
serialization. Neither Python nor Go verifies that an event constructed
with all eight fields populated serializes and deserializes to an equal
value.

## Context

Captured during PR #246 deep review (Nice-to-have #1). Field-number
renumber or type-change accidents on future proto edits would survive
CI silently because no test reads the bytes back.

## Impact

- A wire-shape regression (e.g. swapping field numbers 4 and 5, or
  flipping `repeated string mentions` to `string`) ships green.
- Cross-language drift (Python emits a value, Go fails to parse it)
  surfaces only at integration time, possibly long after the offending
  proto edit.

## Proposed fix / investigation path

Add a 5–10 line test in `agents/tests/` (or `tests/unit/python/`):

```python
def test_channel_message_event_roundtrip():
    event = task_pb2.ChannelMessageEvent(
        message_id="msg-001",
        channel_id="group:eng",
        channel_type="group",
        sender_id="alice",
        recipient_id="bob",
        content="hello",
        mentions=["bob", "carol"],
        thread_id="t-1",
        timestamp="2026-05-04T12:00:00Z",
    )
    blob = event.SerializeToString()
    decoded = task_pb2.ChannelMessageEvent.FromString(blob)
    assert decoded == event
```

Mirror with a Go test in `internal/generated/taskpb/` (or a small
helper test in `internal/channels/`) using `proto.Marshal` /
`proto.Unmarshal` to catch cross-language renumber drift.

## Notes

> 2026-05-04 — initial capture during PR #246 deep review (NTH-1).
> Cheap to add; intentionally deferred to keep PR 3 strictly proto +
> stub.
>
> 2026-05-08 — resolved. Tests added at
> [`tests/unit/python/test_channel_message_event_roundtrip.py`](../../tests/unit/python/test_channel_message_event_roundtrip.py)
> and
> [`internal/channels/proto_roundtrip_test.go`](../../internal/channels/proto_roundtrip_test.go).
>
> Both files pair a Marshal/Unmarshal round-trip against a golden-bytes
> assertion that pins each field's wire-format tag byte. Symmetric
> renumbers (where both languages regenerate together) survive the
> round-trip equality check — that is the limitation noted in the
> proposed-fix sketch above — but the golden-bytes assertion fails on
> the regenerating side immediately, so cross-language drift cannot
> ship green. Mutation-tested by swapping `content = 5` ↔
> `timestamp = 6` in `proto/task.proto` + `make proto`: both Python
> (`assert b'2\x05hello' == b'*\x05hello'` on the `content` case) and
> Go (`expected []byte{0x32,...}` vs `actual []byte{0x2a,...}` on the
> `timestamp` case) failed loudly.
