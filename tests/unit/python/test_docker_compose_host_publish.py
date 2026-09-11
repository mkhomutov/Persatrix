"""
Guards the compose files against publishing the orchestrator on every host
interface.

A compose ``ports`` entry without a host address (``"8080:8080"``) is
published on every interface of the host — ``0.0.0.0`` and ``[::]`` — so
anyone on the same network can reach it, not just the developer's own
machine. Both ports the orchestrator publishes are unauthenticated under the
shipped defaults:

- 8080 carries the REST API and, because the stack passes ``--enable-ui``,
  the web console. ``config/security.yaml`` ships ``auth.mode: disabled``.
- 9090 carries the gRPC LogService, which has no authentication at all.

Inside the compose network the orchestrator has to listen on ``0.0.0.0``,
because the agents dial ``orchestrator:8080`` and ``orchestrator:9090`` from
their own containers. That makes the host-side publish the only thing keeping
these ports on the developer's machine, so every entry must name a loopback
address. The overlays are checked too: compose merges ``ports`` lists, so an
overlay that adds a bare mapping would publish the port on every interface
again.

Only the orchestrator is checked. The agents' and the observability
services' publishes still name no address; they, and this check's blind
spots, are docs/issues/ISSUE-0153-compose-ports-open-on-every-interface.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILES = sorted(REPO_ROOT.glob("docker-compose*.yaml"))
LOOPBACK_ADDRESSES = {"127.0.0.1", "::1"}

# A compose variable such as ``${ORCH_HTTP_PORT:-8080}`` can contain a colon,
# so variables are masked before an entry is split on ":" and put back after.
_VARIABLE = re.compile(r"\$\{[^}]*\}")
_MASK = "\0"


def _orchestrator_ports(path: Path) -> list[object]:
    """Return the orchestrator's ``ports`` entries in one compose file."""
    with path.open("rb") as fh:
        compose = yaml.safe_load(fh) or {}
    orchestrator = (compose.get("services") or {}).get("orchestrator") or {}
    return list(orchestrator.get("ports") or [])


def _host_address(entry: object) -> str | None:
    """Return the host address a ``ports`` entry is published on, or None.

    Short syntax is ``[HOST_IP:][HOST_PORT:]CONTAINER_PORT[/PROTOCOL]``, with
    an IPv6 host address in brackets. Long syntax is a mapping with an
    optional ``host_ip`` key. None means the entry names no address, so
    Docker publishes it on every interface. A host address given as a
    variable comes back as written.
    """
    if isinstance(entry, dict):
        host_ip = entry.get("host_ip")
        return str(host_ip) if host_ip else None
    text = str(entry)
    if text.startswith("["):
        return text[1 : text.index("]")]
    variables = iter(_VARIABLE.findall(text))
    parts = _VARIABLE.sub(_MASK, text).split(":")
    if len(parts) != 3:
        return None
    return re.sub(_MASK, lambda _: next(variables), parts[0])


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("127.0.0.1:8080:8080", "127.0.0.1"),
        ("127.0.0.1::8080", "127.0.0.1"),
        ("[::1]:8080:8080", "::1"),
        ("127.0.0.1:${ORCH_HTTP_PORT:-8080}:8080", "127.0.0.1"),
        ("${HOST_IP:-127.0.0.1}:8080:8080", "${HOST_IP:-127.0.0.1}"),
        ("0.0.0.0:8080:8080", "0.0.0.0"),
        ("[::]:8080:8080", "::"),
        ("8080:8080", None),
        ("8080:8080/tcp", None),
        ("8080", None),
        (8080, None),
        ({"target": 8080, "published": 8080}, None),
        ({"target": 8080, "published": 8080, "host_ip": "127.0.0.1"}, "127.0.0.1"),
    ],
)
def test_host_address_reads_compose_port_syntax(entry: object, expected: str | None) -> None:
    """The parser behind the loopback check, pinned case by case.

    A variable host address such as ``${HOST_IP:-127.0.0.1}`` comes back as
    written, so the check fails closed on it: its default is loopback, but the
    environment could still set it to 0.0.0.0.
    """
    assert _host_address(entry) == expected


def test_base_compose_publishes_orchestrator_ports() -> None:
    """Keeps the loopback check below from passing on an empty list."""
    ports = _orchestrator_ports(REPO_ROOT / "docker-compose.yaml")
    assert ports, "docker-compose.yaml no longer publishes any orchestrator port"


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_orchestrator_host_publishes_are_loopback_only(path: Path) -> None:
    """Every orchestrator publish must be bound to a loopback address."""
    exposed = [
        entry
        for entry in _orchestrator_ports(path)
        if _host_address(entry) not in LOOPBACK_ADDRESSES
    ]
    assert not exposed, (
        f"{path.name} publishes orchestrator port(s) {exposed} without a "
        "loopback host address, so Docker opens them on every interface. "
        "Under the shipped auth.mode: disabled that puts the unauthenticated "
        "REST API, the web console and the gRPC LogService on the local "
        "network. Prefix the mapping with 127.0.0.1: (for example "
        '"127.0.0.1:8080:8080"). To serve the console beyond localhost, put an '
        "authenticating TLS proxy in front instead — see "
        "docs/guides/web-console.md, section Security."
    )
