#!/usr/bin/env python3
"""Preflight gates for MT-MEMORY-GROUP-TENANT-001.

Split from the driver (``mt_group_tenant_001.py``) because these checks are
the part worth running on their own: every one of them guards a leg that
otherwise passes *vacuously*, and an operator wants to clear them before
spending a paid arc — not discover a silent no-op halfway through.

Each gate returns a :class:`Gate` naming the leg it protects and, when it
fails, the exact remedy. Nothing here mutates the stack: the driver applies
fixes, this module only reports. That separation is deliberate — a preflight
that quietly reconfigures the system under test is not a preflight.

The four vacuity traps, all recorded in the MT itself:

* ``floor_control: true`` on the room makes ``ChannelRouter.Publish`` suppress
  the re-fanout of a floor-turn reply, so agent publishes never reach
  ``Dispatch`` and **Leg 2 sees zero tenant-less hops** — indistinguishable
  from R-2 being fixed. Clearing it requires clearing ``escalation_chair_id``
  in the same PATCH, since the chair requires floor control.
* The collector tail-samples healthy traces at **1%**, so the
  ``channel.dispatch`` spans Leg 2 reads are simply absent.
* A low ``interaction_idle_timeout_seconds`` closes an interaction by accident
  before Leg 4 asks for it, changing which trigger fired — the variable Legs 4
  and 6 turn on.
* Zero registered agents produces a healthy, green-looking stack in which every
  dispatch is dropped. That cost a full live arc on 2026-08-07.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_SERVER = "http://127.0.0.1:8080"
DEFAULT_JAEGER = "http://localhost:16686"
COLLECTOR_CONFIG = REPO_ROOT / "config" / "observability" / "otel-collector.yaml"
SECURITY_CONFIG = REPO_ROOT / "config" / "security.yaml"
ROOM = "planning"
PERSONAS = ("ember-owl", "iron-fox", "nova-sparrow")


def _config_url(server: str, room: str) -> str:
    """Governance lives on the `/config` sub-resource, under a prefixed id.

    Two things bite here, and both cost a 404 the first time this ran against
    a live stack: the REST id carries the topology prefix (`group:planning`,
    not `planning`), and the channel resource itself returns members and
    classification — **not** governance. `/config` returns each knob as a
    ``{value, source}`` pair, where ``source`` separates a YAML-seeded default
    from a store override.
    """
    return f"{server}/api/v1/channels/group:{room}/config"

# Leg 4 needs the interaction still open when it asks for the close, and the
# arc's own pacing runs several minutes. 1800s is the MT's suggested value.
MIN_IDLE_TIMEOUT = 1800


@dataclass
class Gate:
    """One preflight check: what it protects, and how to fix it."""

    name: str
    leg: str
    ok: bool
    detail: str
    remedy: str = ""

    def render(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        line = f"  [{mark}] {self.name} (protects {self.leg}) — {self.detail}"
        if not self.ok and self.remedy:
            line += f"\n         remedy: {self.remedy}"
        return line


def _bearer_token() -> str:
    """The operator's stored CLI token, if they have logged in.

    Once `auth.mode: enabled` is live, most of the endpoints these gates read
    (`/api/v1/agents`, a channel's `/config`) answer **401** to an anonymous
    caller — so a preflight that reads them anonymously reports every gate as
    broken the moment auth starts working. The token lives where the CLI put
    it, keyed by server URL; missing or unreadable is not an error here, it
    just means the probes go out unauthenticated and say so.
    """
    path = Path.home() / ".persatrix" / "credentials"
    try:
        blob = json.loads(path.read_text())
    except (OSError, ValueError):
        return ""
    for entry in blob.values():
        if isinstance(entry, dict) and entry.get("token"):
            return str(entry["token"])
    return ""


def _get_json(url: str, timeout: float = 5.0) -> Any:
    req = urllib.request.Request(url)  # noqa: S310
    token = _bearer_token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def gate_orchestrator(server: str) -> Gate:
    """The orchestrator answers /healthz before anything publishes."""
    try:
        with urllib.request.urlopen(f"{server}/healthz", timeout=5) as resp:  # noqa: S310
            ok = resp.status == 200
        return Gate(
            "orchestrator healthy",
            "all legs",
            ok,
            f"/healthz -> {resp.status}",
            "start the stack: make demo-anthropic",
        )
    except (urllib.error.URLError, OSError) as exc:
        return Gate(
            "orchestrator healthy",
            "all legs",
            False,
            f"unreachable: {exc}",
            "start the stack: make demo-anthropic",
        )


def gate_agents_registered(server: str) -> Gate:
    """Zero registered agents = every dispatch dropped, silently.

    Since ISSUE-0125 the agents re-register themselves on any departure from
    READY and return, so this should self-heal — but the registry is still
    in-memory, and a stack that came up in the wrong order can sit empty. The
    2026-08-07 arc was lost to exactly this.
    """
    try:
        payload = _get_json(f"{server}/api/v1/agents")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Gate(
            "agents registered",
            "all legs",
            False,
            f"could not read /api/v1/agents: {exc}",
            "check the orchestrator is up",
        )
    agents = payload.get("agents", payload) if isinstance(payload, dict) else payload
    count = len(agents) if isinstance(agents, list) else 0
    missing = [p for p in PERSONAS if p not in json.dumps(payload)]
    ok = count > 0 and not missing
    detail = f"{count} registered"
    if missing:
        detail += f"; missing {', '.join(missing)}"
    return Gate(
        "agents registered",
        "all legs",
        ok,
        detail,
        "wait for the agents' healthchecks; ISSUE-0125 re-registration needs "
        "the orchestrator reachable first",
    )


def gate_floor_control(server: str, room: str = ROOM) -> Gate:
    """floor_control must be OFF or Leg 2 sees zero tenant-less dispatches."""
    try:
        payload = _get_json(_config_url(server, room))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Gate(
            "floor_control off",
            "Leg 2 (R-2)",
            False,
            f"could not read the channel config: {exc}",
            "",
        )
    floor = (payload.get("floor_control") or {}).get("value")
    chair = (payload.get("escalation_chair_id") or {}).get("value") or ""
    ok = floor is False and not chair
    detail = f"floor_control={floor!r}, escalation_chair_id={chair!r}"
    return Gate(
        "floor_control off",
        "Leg 2 (R-2)",
        ok,
        detail,
        "PATCH the channel clearing BOTH in one request — the chair requires "
        "floor control, so clearing only one is rejected at load",
    )


def gate_tail_sampling() -> Gate:
    """The collector samples healthy traces at 1% by default; Leg 2 needs 100."""
    if not COLLECTOR_CONFIG.exists():
        return Gate(
            "otel tail sampling",
            "Leg 2 (R-2)",
            False,
            f"missing {COLLECTOR_CONFIG.relative_to(REPO_ROOT)}",
            "",
        )
    text = COLLECTOR_CONFIG.read_text()
    ok = "sampling_percentage: 100" in text
    current = "100" if ok else "not 100 (default is 1)"
    return Gate(
        "otel tail sampling",
        "Leg 2 (R-2)",
        ok,
        f"sampling_percentage {current}",
        f"set sampling_percentage: 100 in "
        f"{COLLECTOR_CONFIG.relative_to(REPO_ROOT)}, restart the collector, "
        f"and REVERT after the run",
    )


def gate_auth_mode(expected: str = "enabled") -> Gate:
    """Legs 0-7 and 9 need auth enabled; Leg 8 deliberately turns it off."""
    if not SECURITY_CONFIG.exists():
        return Gate(
            f"auth.mode: {expected}",
            "Legs 0-7, 9",
            False,
            f"missing {SECURITY_CONFIG.relative_to(REPO_ROOT)}",
            "",
        )
    # PARSE, do not grep. The first version of this gate tested
    # `"mode: enabled" in text` and passed against a file whose auth block
    # read `mode: disabled` — because a COMMENT eleven lines above says "a
    # typo in `mode: enabled` must not silently boot an unauthenticated…".
    # It cost a live arc: the run completed green with every message stamped
    # `local` and every dispatch tenant-less, which reads exactly like an R-2
    # failure and is in fact auth being off. The gate that exists to stop a
    # leg passing vacuously was itself passing vacuously.
    try:
        import yaml  # noqa: PLC0415

        doc = yaml.safe_load(SECURITY_CONFIG.read_text()) or {}
        actual = str(((doc.get("auth") or {}).get("mode")) or "")
    except Exception as exc:  # noqa: BLE001
        return Gate(f"auth.mode: {expected}", "Legs 0-7, 9", False,
                    f"could not parse security.yaml: {exc}", "")
    ok = actual == expected
    return Gate(
        f"auth.mode: {expected}",
        "Legs 0-7, 9",
        ok,
        f"auth.mode resolves to {actual!r}",
        f"set auth.mode: {expected} in "
        f"{SECURITY_CONFIG.relative_to(REPO_ROOT)} and restart the orchestrator",
    )


def gate_auth_live(server: str, room: str = ROOM, expected: str = "enabled") -> Gate:
    """Prove auth is live on the RUNNING orchestrator, not just in the file.

    The config gate reads `config/security.yaml`; this reads the process that
    is actually serving. They disagree whenever the stack was started before
    the file was edited — which is the ordinary case, since enabling auth
    requires a restart. Both gates are needed: the file gate tells you what
    the next restart will do, this one tells you what the current process is
    doing, and the arc is spent against the latter.

    The probe is a read-only GET on a `policyAuthenticated` route. With auth
    enabled and no bearer token it must answer **401**; with auth disabled the
    policy matrix is not enforced and it answers anything else.
    """
    url = f"{server}/api/v1/channels/group:{room}/members/alice-person/history"
    try:
        req = urllib.request.Request(url)  # noqa: S310
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, OSError) as exc:
        return Gate("auth enforced live", "Legs 0-7, 9", False,
                    f"probe failed: {exc}", "")
    enforcing = status == 401
    ok = enforcing if expected == "enabled" else not enforcing
    return Gate(
        "auth enforced live",
        "Legs 0-7, 9",
        ok,
        f"unauthenticated probe -> HTTP {status} "
        f"({'enforcing' if enforcing else 'NOT enforcing'})",
        "restart the orchestrator after editing security.yaml — a running "
        "process keeps the mode it booted with",
    )


def gate_idle_timeout(server: str, room: str = ROOM) -> Gate:
    """A short idle timeout closes the interaction before Leg 4 asks."""
    try:
        payload = _get_json(_config_url(server, room))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Gate(
            "idle timeout raised",
            "Legs 3-4",
            False,
            f"could not read the channel config: {exc}",
            "",
        )
    raw = (payload.get("interaction_idle_timeout_seconds") or {}).get("value")
    value = raw if isinstance(raw, int) else 0
    ok = value >= MIN_IDLE_TIMEOUT
    return Gate(
        "idle timeout raised",
        "Legs 3-4",
        ok,
        f"interaction_idle_timeout_seconds={raw!r} (need >= {MIN_IDLE_TIMEOUT})",
        f"set interaction_idle_timeout_seconds: {MIN_IDLE_TIMEOUT} on "
        f"{room} so no leg closes an interaction by accident",
    )


def gate_jaeger(jaeger: str) -> Gate:
    """Leg 2 reads spans out of Jaeger; a dead UI means no evidence."""
    try:
        with urllib.request.urlopen(f"{jaeger}/api/services", timeout=5) as resp:  # noqa: S310
            ok = resp.status == 200
        return Gate("jaeger reachable", "Leg 2 (R-2)", ok, f"/api/services -> {resp.status}")
    except (urllib.error.URLError, OSError) as exc:
        return Gate(
            "jaeger reachable",
            "Leg 2 (R-2)",
            False,
            f"unreachable: {exc}",
            "the observability stack ships with the compose file",
        )


def gate_docker() -> Gate:
    """Leg 9 restarts the AGENTS via compose; Leg 8 restarts the orchestrator."""
    try:
        proc = subprocess.run(  # noqa: S603
            ["docker", "compose", "ps", "--format", "json"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Gate("docker compose", "Legs 8-9", False, f"unavailable: {exc}")
    ok = proc.returncode == 0
    running = proc.stdout.count('"State":"running"') if ok else 0
    return Gate(
        "docker compose",
        "Legs 8-9",
        ok and running > 0,
        f"{running} service(s) running",
        "Leg 9 restarts the agents and Leg 8 the orchestrator — both need compose",
    )


def run_gates(server: str, jaeger: str, auth_mode: str = "enabled") -> list[Gate]:
    """Run every preflight gate and return the results in report order."""
    return [
        gate_orchestrator(server),
        gate_agents_registered(server),
        gate_docker(),
        gate_jaeger(jaeger),
        gate_auth_mode(auth_mode),
        gate_auth_live(server, expected=auth_mode),
        gate_floor_control(server),
        gate_tail_sampling(),
        gate_idle_timeout(server),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--jaeger", default=DEFAULT_JAEGER)
    parser.add_argument("--auth-mode", default="enabled", choices=["enabled", "disabled"])
    args = parser.parse_args(argv)

    print("MT-MEMORY-GROUP-TENANT-001 — preflight")
    print("=" * 62)
    gates = run_gates(args.server, args.jaeger, args.auth_mode)
    for gate in gates:
        print(gate.render())
    failed = [g for g in gates if not g.ok]
    print("=" * 62)
    if failed:
        print(f"{len(failed)}/{len(gates)} gate(s) FAILED — fix before spending the arc.")
        print("Every failure above is a leg that would otherwise pass vacuously.")
        return 1
    print(f"All {len(gates)} gates pass. The arc can run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
