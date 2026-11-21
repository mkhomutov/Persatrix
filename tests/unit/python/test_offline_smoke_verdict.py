"""Hold the offline smoke's verdict (ISSUE-0219).

``make smoke-offline`` is the offline demo with a verdict: it fails unless
at least two of the roundtable's agents post three messages **in this run**.
``scripts/checks/offline_smoke.py`` gives that verdict in two calls around
the convening, a mark and a count of what came after it.

The second half of that sentence was missing at first. The channel's
history is kept on a volume that outlives the stack, the check read all of
it, and so a stack that came up and convened nothing passed on an earlier
run's messages. These tests hold both halves:

- what counts: three messages, two agents, registered agents only;
- what does not: any message the channel held when the mark was taken;
- there is no verdict without a mark, and no mark without an orchestrator;
- every wait ends at the time it was given.

The order in which ``make smoke-offline`` takes the mark and convenes is
held with the workflow, in ``test_scheduled_image_smoke.py``.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from scripts.checks import offline_smoke

# ─── The verdict ──────────────────────────────────────────────────────────


def _message(sender: str, message_id: str = "") -> dict[str, Any]:
    return {"id": message_id, "sender_id": sender, "content": "…"}


@pytest.mark.parametrize(
    ("senders", "passes"),
    [
        (["nova-sparrow", "ember-owl", "iron-fox"], True),
        (["nova-sparrow", "ember-owl", "nova-sparrow"], True),
        (["nova-sparrow", "ember-owl"], False),                 # two messages
        (["nova-sparrow", "nova-sparrow", "nova-sparrow"], False),  # one agent talking to itself
        # A person's messages do not count.
        (["operator", "operator", "nova-sparrow", "operator"], False),
        ([], False),
    ],
)
def test_the_verdict_wants_three_messages_from_two_agents(
    senders: list[str], passes: bool,
) -> None:
    agents = {"nova-sparrow", "ember-owl", "iron-fox"}
    ok, seen = offline_smoke.verdict([_message(s) for s in senders], agents)
    assert ok is passes, seen
    assert (offline_smoke.MIN_MESSAGES, offline_smoke.MIN_SPEAKERS) == (3, 2)


def test_a_stack_that_does_not_answer_fails_within_the_time_given(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Nothing listens on port 1. The check gives up at its time-out and says what it saw."""
    mark = tmp_path / "mark.json"
    empty = {"channel": "group:roundtable", "message_ids": []}
    mark.write_text(json.dumps(empty), encoding="utf-8")
    code = offline_smoke.main(
        ["--since", str(mark), "--server", "http://127.0.0.1:1", "--timeout", "0.2",
         "--interval", "0.05"],
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "[FAIL] group:roundtable" in out and "could not read the orchestrator" in out


# ─── Only this run's messages count ───────────────────────────────────────
#
# The channel's history is kept on a volume that outlives the stack, so a
# checkout that has run the demo before holds an earlier run's messages. The
# check takes a mark before the convening and counts only what is not in it.


def test_messages_from_before_the_mark_do_not_count() -> None:
    agents = {"nova-sparrow", "ember-owl", "iron-fox"}
    earlier = [
        _message("nova-sparrow", "o1"), _message("ember-owl", "o2"), _message("iron-fox", "o3"),
    ]
    this_run = [
        _message("nova-sparrow", "n1"), _message("ember-owl", "n2"), _message("nova-sparrow", "n3"),
    ]

    ok, seen = offline_smoke.verdict(earlier, agents, before={"o1", "o2", "o3"})
    assert ok is False, "an earlier run's conversation is not this run's"
    assert "0 message(s)" in seen and "3 older message(s) not counted" in seen

    ok, seen = offline_smoke.verdict(earlier + this_run, agents, before={"o1", "o2", "o3"})
    assert ok is True, seen
    assert "3 message(s) from 2 agent(s)" in seen and "3 older message(s) not counted" in seen

    ok, _ = offline_smoke.verdict(earlier + this_run[:2], agents, before={"o1", "o2", "o3"})
    assert ok is False, "two new messages are not three, however many older ones there are"


class _Roundtable:
    """A stand-in orchestrator: the two routes the check reads, on a loopback
    port, serving whatever `messages` holds at the moment of the call."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        roundtable = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — the base class's name
                if self.path == "/api/v1/agents":
                    agents = ("nova-sparrow", "ember-owl", "iron-fox")
                    body: Any = [{"id": a} for a in agents]
                elif self.path.startswith("/api/v1/channels/group:roundtable/messages"):
                    body = {"messages": list(roundtable.messages)}
                else:
                    self.send_error(404)
                    return
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)


@pytest.fixture
def roundtable() -> Iterator[_Roundtable]:
    stand_in = _Roundtable()
    try:
        yield stand_in
    finally:
        stand_in.close()


#: Every wait of the check in these tests ends within a second.
_QUICK = ["--timeout", "0.3", "--interval", "0.05"]


def test_a_run_in_which_nobody_speaks_fails_whatever_an_earlier_run_left(
    roundtable: _Roundtable, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The fault this guards: the volume held seven messages of an earlier run,
    the stack came up, nothing convened, and the check answered OK."""
    roundtable.messages = [
        _message("nova-sparrow", "o1"), _message("ember-owl", "o2"), _message("iron-fox", "o3"),
    ]
    mark = tmp_path / "mark.json"

    assert offline_smoke.main(["--mark", str(mark), "--server", roundtable.url, *_QUICK]) == 0
    assert "holds 3 message(s) before this run's convening" in capsys.readouterr().out

    assert offline_smoke.main(["--since", str(mark), "--server", roundtable.url, *_QUICK]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] group:roundtable" in out and "3 older message(s) not counted" in out


def test_a_run_in_which_the_agents_speak_passes_on_its_own_messages(
    roundtable: _Roundtable, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    roundtable.messages = [_message("nova-sparrow", "o1"), _message("ember-owl", "o2")]
    mark = tmp_path / "mark.json"
    assert offline_smoke.main(["--mark", str(mark), "--server", roundtable.url, *_QUICK]) == 0

    roundtable.messages += [
        _message("nova-sparrow", "n1"), _message("ember-owl", "n2"), _message("iron-fox", "n3"),
    ]

    assert offline_smoke.main(["--since", str(mark), "--server", roundtable.url, *_QUICK]) == 0
    out = capsys.readouterr().out
    assert "[OK] group:roundtable: 3 message(s) from 3 agent(s)" in out
    assert "2 older message(s) not counted" in out


def test_there_is_no_verdict_without_a_mark(
    roundtable: _Roundtable, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing mark, or the mark of another channel, is a failure and never
    a count of the whole history."""
    roundtable.messages = [
        _message("nova-sparrow", "o1"), _message("ember-owl", "o2"), _message("iron-fox", "o3"),
    ]

    missing = tmp_path / "no-such-mark.json"
    assert offline_smoke.main(["--since", str(missing), "--server", roundtable.url, *_QUICK]) == 1
    assert "could not be used" in capsys.readouterr().out

    other = tmp_path / "other.json"
    other.write_text(json.dumps({"channel": "group:other", "message_ids": []}), encoding="utf-8")
    assert offline_smoke.main(["--since", str(other), "--server", roundtable.url, *_QUICK]) == 1
    assert "the mark is of group:other" in capsys.readouterr().out

    with pytest.raises(SystemExit) as refused:
        offline_smoke.main(["--server", roundtable.url, *_QUICK])
    assert refused.value.code == 2, "one of --mark and --since is required"


def test_a_mark_that_cannot_be_taken_fails_within_the_time_given(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Nothing listens on port 1: no mark is written, so no verdict can follow."""
    mark = tmp_path / "mark.json"
    assert offline_smoke.main(["--mark", str(mark), "--server", "http://127.0.0.1:1", *_QUICK]) == 1
    assert "[FAIL] no mark" in capsys.readouterr().out
    assert not mark.exists()
