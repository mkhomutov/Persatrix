"""Cross-language drift pins for the reply echo (ISSUE-0200).

The orchestrator (Go) hands an agent's reply to a waiting request only
when the reply's ``in_reply_to`` is the id of that request's message.  The
id makes a round trip through four places in two languages, and a rename
on one side alone leaves every reply unmatched: each chat request then
times out and each floor turn waits its full timeout, with every other
suite green.  So the four are pinned here as text:

1. Go registers the wait under the id of the message it is about to
   dispatch (``PublishAndAwait`` and the floor turn).
2. Go puts that id on the event (``MessageId: msg.ID``).
3. Python keeps it as the event's ``message_id`` and takes it into the
   dispatch context (``DispatchContext.for_event``).
4. Python stamps it on a same-channel publish under the key Go reads
   (``replyToMetadataKey``).

Same posture as the sibling files: sources pinned as text so the test
runs anywhere the Python unit suite runs — no Go toolchain dependency.
"""

from __future__ import annotations

from pathlib import Path

from .test_cross_language_interaction_wire_drift import _go_const, _parse_miss

_WAITER_GO = Path("internal/channels/waiter.go")
_PUBLISH_AND_AWAIT_GO = Path("internal/channels/publish_and_await.go")
_FLOOR_CONTROL_GO = Path("internal/channels/floor_control.go")
_GRPC_DISPATCHER_GO = Path("internal/channels/grpc_dispatcher_proto.go")
_DISPATCH_CONTEXT_PY = Path("agents/dispatch_context.py")
#: The channel-message handler builds the event.  It has lived in the
#: servicer module and in a module of its own; whichever holds it is read.
_EVENT_BUILDERS_PY = (Path("agents/channel_receive.py"), Path("agents/server_servicers.py"))


def _require(text: str, path: Path) -> None:
    if text not in path.read_text(encoding="utf-8"):
        _parse_miss(f"`{text}`", path)


def test_the_reply_echo_key_agrees() -> None:
    """Go's ``replyToMetadataKey`` and the one Python producer's literal
    MUST be equal."""
    key = _go_const(_WAITER_GO, "replyToMetadataKey")

    assert key == "in_reply_to"
    _require(f'claim["{key}"] = self.origin_message_id', _DISPATCH_CONTEXT_PY)


def test_go_waits_under_the_id_it_dispatches() -> None:
    """Both waits are registered with the id of the message that is then
    dispatched, and the dispatcher puts that id on the event."""
    _require(
        "r.waiter.Register(msg.ChannelID, awaitFromAgentID, msg.ID)", _PUBLISH_AND_AWAIT_GO,
    )
    _require(
        "r.waiter.Register(msg.ChannelID, speaker.ParticipantID, msg.ID)", _FLOOR_CONTROL_GO,
    )
    src = _GRPC_DISPATCHER_GO.read_text(encoding="utf-8")
    if "MessageId:" not in src or "msg.ID," not in src.split("MessageId:", 1)[1].split("\n", 1)[0]:
        _parse_miss("the dispatcher lift `MessageId: msg.ID`", _GRPC_DISPATCHER_GO)


def test_python_carries_the_event_id_into_the_reply() -> None:
    """The event keeps the wire id, and the dispatch context takes it."""
    builders = [p for p in _EVENT_BUILDERS_PY if p.exists()]
    lift = "message_id=request.message_id,"
    if not any(lift in p.read_text(encoding="utf-8") for p in builders):
        _parse_miss(f"`{lift}`", builders[0])
    _require('origin_message_id=event.message_id or "",', _DISPATCH_CONTEXT_PY)
