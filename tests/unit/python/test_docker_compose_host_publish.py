"""
Guards the compose files against putting any service on every host interface.

A compose ``ports`` entry without a host address (``"8080:8080"``) is
published on every interface of the host — ``0.0.0.0`` and ``[::]`` — so
anyone on the same network can reach it, not just the developer's own
machine. Nothing the stack publishes authenticates under the shipped
defaults:

- The orchestrator's 8080 carries the REST API and, because the stack passes
  ``--enable-ui``, the web console; ``config/security.yaml`` ships
  ``auth.mode: disabled``. Its 9090 carries the gRPC LogService, which has no
  authentication at all.
- The agents' gRPC service has no authentication either: anyone who can reach
  it can run persona turns and spend the operator's provider budget. So the
  agents publish nothing; the orchestrator dials each one at its
  ``--advertise-address`` over the compose network.
- Jaeger, Prometheus and Loki serve traces, metrics and logs, which can carry
  conversation content, and the OTLP Collector accepts writes from anyone.

Inside the compose network each service listens on ``0.0.0.0`` so the others
can dial it, which leaves the host-side publish as the only thing keeping a
port on the developer's machine. So every entry, in every service, must name
a loopback address. The overlays are checked too: compose merges ``ports``
lists, so an overlay that adds a bare mapping would publish the port on every
interface again. ``network_mode: host`` is rejected outright, because a
service on the host's own network listens on every interface without any
``ports`` entry. The history is in
docs/issues/ISSUE-0153-compose-ports-open-on-every-interface.md.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILES = sorted(REPO_ROOT.glob("docker-compose*.yaml"))
LOOPBACK_ADDRESSES = {"127.0.0.1", "::1"}

# A compose variable such as ``${ORCH_HTTP_PORT:-8080}`` can contain a colon,
# so variables are masked before an entry is split on ":" and put back after.
_VARIABLE = re.compile(r"\$\{[^}]*\}")
_MASK = "\0"


def _load(path: Path) -> dict[str, Any]:
    """Return one compose file as parsed YAML."""
    with path.open("rb") as fh:
        compose: dict[str, Any] = yaml.safe_load(fh) or {}
    return compose


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


def _exposures(compose: dict[str, Any]) -> list[str]:
    """Return each way a compose document puts a service on every interface.

    That is a ``ports`` entry that names no loopback address, or a service on
    the host's own network. Each comes back as ``"<service>: <what>"``.
    """
    found: list[str] = []
    for name, service in (compose.get("services") or {}).items():
        service = service or {}
        if service.get("network_mode") == "host":
            found.append(f"{name}: network_mode: host")
        found.extend(
            f"{name}: {entry!r}"
            for entry in service.get("ports") or []
            if _host_address(entry) not in LOOPBACK_ADDRESSES
        )
    return found


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


@pytest.mark.parametrize(
    ("services", "expected"),
    [
        (
            {
                "orchestrator": {"ports": ["127.0.0.1:8080:8080"]},
                "jaeger": {"ports": ["16686:16686"]},
            },
            ["jaeger: '16686:16686'"],
        ),
        ({"loki": {"ports": ["0.0.0.0:3100:3100"]}}, ["loki: '0.0.0.0:3100:3100'"]),
        ({"orchestrator": {"network_mode": "host"}}, ["orchestrator: network_mode: host"]),
        (
            {
                "sidecar": {"network_mode": "service:jaeger"},
                "jaeger": {"ports": ["[::1]:16686:16686"]},
            },
            [],
        ),
        ({"agent-planner": None, "agent-coder": {}}, []),
    ],
    ids=["later-service", "wildcard-address", "host-network", "shared-network", "no-publish"],
)
def test_exposures_checks_every_service(services: dict[str, Any], expected: list[str]) -> None:
    """The check itself, on small compose documents.

    It must look past the first service, and ``network_mode: host`` must count
    even though such a service has no ``ports`` entry.
    """
    assert _exposures({"services": services}) == expected


def test_compose_files_are_read() -> None:
    """Keeps the loopback check below from passing on nothing.

    An empty file list would turn the check into a skipped test, and a file
    read as having no services would pass it without checking anything.
    """
    assert REPO_ROOT / "docker-compose.yaml" in COMPOSE_FILES
    for path in COMPOSE_FILES:
        assert _load(path).get("services"), f"{path.name} declares no services"


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_no_service_listens_on_every_host_interface(path: Path) -> None:
    """Every publish, in every service, must be bound to a loopback address."""
    exposed = _exposures(_load(path))
    assert not exposed, (
        f"{path.name} puts {'; '.join(exposed)} on every interface of the "
        "host, so anyone on the same network can reach it, and nothing the "
        "stack publishes authenticates under the shipped defaults. Prefix the "
        'mapping with 127.0.0.1: (for example "127.0.0.1:16686:16686"), or drop '
        "a publish that nothing on the host uses, and never use network_mode: "
        "host. To reach a service from another machine, tunnel over SSH or put "
        "an authenticating proxy in front instead of widening the publish — for "
        "the web console, see docs/guides/web-console.md, section Security."
    )
