#!/usr/bin/env python3
"""Give the offline demo a verdict: did the personas talk in this run?

``make demo-autonomous`` builds the product images, starts the whole stack on
the mock provider and convenes the roundtable with no person in it. It is the
release sweep's "offline smoke". By itself it answers one question, whether
the stack came up: its last step only warns when the convening did not land.

This check answers the other one, in two calls around the convening:

    python scripts/checks/offline_smoke.py --mark  <file>   # stack up, before the convening
    python scripts/checks/offline_smoke.py --since <file>   # after it

``--mark`` writes down which messages the roundtable already holds.
``--since`` then reads the roundtable's history and waits until at least two
different agents have posted, three messages in all, **not counting the
messages in the mark**. For that to happen the two images must build, the
orchestrator must start and serve agents, each agent must register, take a
wallet lease, call the (mock) model and publish, and the orchestrator must
hand each message on to the next agent. No key is needed and nothing is
spent.

Why a mark. The channel's history is kept on a volume that outlives the
stack, so a checkout that has run the demo before holds an earlier run's
messages. Without the mark those earned a pass for a run in which nobody
spoke. The mark is a list of message ids as the orchestrator reports them,
so no clock is compared: the host and the containers do not share one.

Each call exits 0 or 1. ``--since`` exits 1 when the messages are not there
after ``--timeout`` seconds, with what it last saw. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Collection
from pathlib import Path
from typing import Any

DEFAULT_SERVER = "http://localhost:8080"
DEFAULT_CHANNEL = "group:roundtable"
DEFAULT_TIMEOUT = 240.0
#: How long ``--mark`` waits for the orchestrator to answer. The stack is up
#: by then, so this covers only a route that is ready a moment after health.
MARK_TIMEOUT = 30.0
MIN_MESSAGES = 3
MIN_SPEAKERS = 2
#: How many of a channel's newest messages one read asks for. A mark and a
#: verdict read the same number, so a message too old for the mark is too old
#: for the verdict as well.
HISTORY_LIMIT = 100
#: The check names itself to the orchestrator, so its reads have a budget of
#: their own and do not count as anonymous calls.
CALLER_ID = "offline-smoke"

_READ_ERRORS = (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, AttributeError)


def verdict(
    messages: list[dict[str, Any]], agents: set[str], before: Collection[str] = (),
) -> tuple[bool, str]:
    """Whether *messages* show the roundtable talking, and a sentence saying so.

    Only messages from registered agents count: the demo has no person in it,
    so a message from anyone else is not what this check is about. A message
    whose id is in *before* does not count either: it was there before this
    run convened anything.
    """
    older = set(before)
    fresh = [m for m in messages if str(m.get("id", "")) not in older]
    by_agent = [str(m.get("sender_id", "")) for m in fresh if m.get("sender_id") in agents]
    speakers = sorted(set(by_agent))
    seen = f"{len(by_agent)} message(s) from {len(speakers)} agent(s) {speakers}"
    if len(fresh) != len(messages):
        seen += f"; {len(messages) - len(fresh)} older message(s) not counted"
    ok = len(by_agent) >= MIN_MESSAGES and len(speakers) >= MIN_SPEAKERS
    return ok, seen


def _get(url: str) -> Any:
    request = urllib.request.Request(url, headers={"X-Agent-ID": CALLER_ID})  # noqa: S310
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _history(server: str, channel: str) -> list[dict[str, Any]]:
    history = _get(f"{server}/api/v1/channels/{channel}/messages?limit={HISTORY_LIMIT}")
    return list(history.get("messages") or [])


def read_once(server: str, channel: str, before: Collection[str] = ()) -> tuple[bool, str]:
    """One look at the channel: the verdict, or why there is none yet."""
    try:
        agents = {str(a["id"]) for a in _get(f"{server}/api/v1/agents")}
        messages = _history(server, channel)
    except _READ_ERRORS as exc:
        return False, f"could not read the orchestrator: {exc}"
    return verdict(messages, agents, before)


def write_mark(server: str, channel: str, path: Path, timeout: float, interval: float) -> int:
    """Write the ids of the messages *channel* holds now to *path*."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            ids = [str(m["id"]) for m in _history(server, channel)]
            break
        except _READ_ERRORS as exc:
            if time.monotonic() >= deadline:
                print(f"[FAIL] no mark: could not read {channel} from the orchestrator "
                      f"after {timeout:.0f} s: {exc}")
                return 1
            time.sleep(interval)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"channel": channel, "message_ids": ids}), encoding="utf-8")
    print(f"[OK] mark: {channel} holds {len(ids)} message(s) before this run's convening")
    return 0


def read_mark(path: Path, channel: str) -> list[str]:
    """The message ids in the mark at *path*, which must be a mark of *channel*."""
    mark = json.loads(path.read_text(encoding="utf-8"))
    if mark["channel"] != channel:
        raise ValueError(f"the mark is of {mark['channel']}, not of {channel}")
    return [str(message_id) for message_id in mark["message_ids"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    step = parser.add_mutually_exclusive_group(required=True)
    step.add_argument("--mark", type=Path, metavar="FILE",
                      help="write down the messages the channel holds now, and stop")
    step.add_argument("--since", type=Path, metavar="FILE",
                      help="give the verdict, not counting the messages in this mark")
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument("--timeout", type=float, default=None,
                        help=f"seconds to wait: for the messages with --since (default "
                             f"{DEFAULT_TIMEOUT:.0f}), for the orchestrator with --mark "
                             f"(default {MARK_TIMEOUT:.0f})")
    parser.add_argument("--interval", type=float, default=3.0)
    args = parser.parse_args(argv)

    if args.mark is not None:
        timeout = MARK_TIMEOUT if args.timeout is None else args.timeout
        return write_mark(args.server, args.channel, args.mark, timeout, args.interval)

    try:
        before = read_mark(args.since, args.channel)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"[FAIL] no verdict: the mark {args.since} could not be used: {exc}. "
              f"Take it with --mark after the stack is up and before the convening.")
        return 1
    timeout = DEFAULT_TIMEOUT if args.timeout is None else args.timeout
    deadline = time.monotonic() + timeout
    while True:
        ok, seen = read_once(args.server, args.channel, before)
        if ok:
            print(f"[OK] {args.channel}: {seen}")
            return 0
        if time.monotonic() >= deadline:
            break
        time.sleep(args.interval)
    print(f"[FAIL] {args.channel} after {timeout:.0f} s: {seen}; wanted at least "
          f"{MIN_MESSAGES} messages from at least {MIN_SPEAKERS} agents since the mark")
    return 1


if __name__ == "__main__":
    sys.exit(main())
