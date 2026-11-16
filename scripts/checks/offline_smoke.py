#!/usr/bin/env python3
"""Give the offline demo a verdict: did the personas actually talk?

``make demo-autonomous`` builds the product images, starts the whole stack on
the mock provider and convenes the roundtable with no person in it. It is the
release sweep's "offline smoke". By itself it answers one question, whether
the stack came up: its last step only warns when the convening did not land.

This check answers the other one. It reads the roundtable's history over the
orchestrator's REST API and waits until at least two different agents have
posted there, three messages in all. For that to happen the two images must
build, the orchestrator must start and serve agents, each agent must register,
take a wallet lease, call the (mock) model and publish, and the orchestrator
must hand each message on to the next agent. No key is needed and nothing is
spent.

    python scripts/checks/offline_smoke.py

It exits 0 when the messages are there, and 1 when they are not there after
``--timeout`` seconds, with what it last saw. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_SERVER = "http://localhost:8080"
DEFAULT_CHANNEL = "group:roundtable"
DEFAULT_TIMEOUT = 240.0
MIN_MESSAGES = 3
MIN_SPEAKERS = 2
#: The check names itself to the orchestrator, so its reads have a budget of
#: their own and do not count as anonymous calls.
CALLER_ID = "offline-smoke"


def verdict(messages: list[dict[str, Any]], agents: set[str]) -> tuple[bool, str]:
    """Whether *messages* show the roundtable talking, and a sentence saying so.

    Only messages from registered agents count: the demo has no person in it,
    so a message from anyone else is not what this check is about.
    """
    by_agent = [str(m.get("sender_id", "")) for m in messages if m.get("sender_id") in agents]
    speakers = sorted(set(by_agent))
    seen = f"{len(by_agent)} message(s) from {len(speakers)} agent(s) {speakers}"
    ok = len(by_agent) >= MIN_MESSAGES and len(speakers) >= MIN_SPEAKERS
    return ok, seen


def _get(url: str) -> Any:
    request = urllib.request.Request(url, headers={"X-Agent-ID": CALLER_ID})  # noqa: S310
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def read_once(server: str, channel: str) -> tuple[bool, str]:
    """One look at the channel: the verdict, or why there is none yet."""
    try:
        agents = {str(a["id"]) for a in _get(f"{server}/api/v1/agents")}
        history = _get(f"{server}/api/v1/channels/{channel}/messages?limit=100")
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
        return False, f"could not read the orchestrator: {exc}"
    return verdict(list(history.get("messages") or []), agents)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="seconds to wait for the messages (default %(default)s)")
    parser.add_argument("--interval", type=float, default=3.0)
    args = parser.parse_args(argv)

    deadline = time.monotonic() + args.timeout
    seen = "nothing read yet"
    while True:
        ok, seen = read_once(args.server, args.channel)
        if ok:
            print(f"[OK] {args.channel}: {seen}")
            return 0
        if time.monotonic() >= deadline:
            break
        time.sleep(args.interval)
    print(f"[FAIL] {args.channel} after {args.timeout:.0f} s: {seen}; "
          f"wanted at least {MIN_MESSAGES} messages from at least {MIN_SPEAKERS} agents")
    return 1


if __name__ == "__main__":
    sys.exit(main())
