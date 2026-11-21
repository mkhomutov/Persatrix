"""Guards the observability services against the images compose pins for them.

Two things broke quietly when Dependabot moved these images forward, and
nothing in CI starts the stack, so only these checks catch them:

* Loki 3.7 and the OpenTelemetry Collector images carry no shell and no
  ``wget``. A healthcheck that runs a program inside one of them can never
  pass, so the service shows ``unhealthy`` for as long as it runs, while it
  works fine. Checked by hand on 2026-11-21: ``docker run --entrypoint sh``
  fails on both, and Loki's healthcheck said ``exec: "wget": executable file
  not found``. Loki has no health flag of its own to call instead.
* Collector 0.165 warns at start-up that two exporter type names are old
  aliases (``otlp`` is now ``otlp_grpc``, ``otlphttp`` is now ``otlp_http``).
  An alias still works until a release drops it; then the Collector refuses
  the whole config and no trace, metric or log reaches a backend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILES = sorted(REPO_ROOT.glob("docker-compose*.yaml"))
COLLECTOR_CONFIG = REPO_ROOT / "config" / "observability" / "otel-collector.yaml"

# Images with no shell and no wget: a healthcheck has nothing to run in them.
# Matched by repository, whatever registry, tag or digest names it, and by the
# service names the base file gives them, since an overlay file can add a
# healthcheck to a service without restating its image.
NO_SHELL_REPOSITORIES = {"grafana/loki", "otel/opentelemetry-collector-contrib"}
NO_SHELL_SERVICES = {"loki", "otel-collector"}

# Old exporter type name -> the name the pinned Collector asks for.
DEPRECATED_EXPORTER_TYPES = {"otlp": "otlp_grpc", "otlphttp": "otlp_http"}


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _runs_a_program(healthcheck: dict[str, Any] | None) -> bool:
    if not healthcheck or healthcheck.get("disable"):
        return False
    test = healthcheck.get("test")
    if isinstance(test, list):
        return bool(test) and test[0] != "NONE"
    return bool(test)


def _repository(image: str) -> str:
    """`docker.io/grafana/loki:3.7.10` or `grafana/loki@sha256:...` -> `grafana/loki`."""
    parts = image.split("@", 1)[0].split("/")
    parts[-1] = parts[-1].split(":", 1)[0]
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]
    return "/".join(parts)


def _broken_healthchecks(compose: dict[str, Any]) -> dict[str, str]:
    services = compose.get("services") or {}
    return {
        name: str(service.get("image", "(image set in another file)"))
        for name, service in services.items()
        if (
            name in NO_SHELL_SERVICES
            or _repository(str(service.get("image", ""))) in NO_SHELL_REPOSITORIES
        )
        and _runs_a_program(service.get("healthcheck"))
    }


def test_the_base_stack_runs_loki_and_the_collector() -> None:
    services = _load(REPO_ROOT / "docker-compose.yaml").get("services") or {}
    assert NO_SHELL_SERVICES <= set(services)
    assert {
        _repository(str(s.get("image", ""))) for s in services.values()
    } >= NO_SHELL_REPOSITORIES


_WGET = {"test": ["CMD", "wget", "-q", "--spider", "http://localhost:3100/ready"]}


@pytest.mark.parametrize(
    ("services", "broken"),
    [
        ({"logs": {"image": "grafana/loki@sha256:" + "0" * 64, "healthcheck": _WGET}}, {"logs"}),
        ({"logs": {"image": "docker.io/grafana/loki:3.7.10", "healthcheck": _WGET}}, {"logs"}),
        ({"logs": {"image": "localhost:5000/grafana/loki", "healthcheck": _WGET}}, {"logs"}),
        # An overlay file adds a check to the base file's service and names no image.
        ({"loki": {"healthcheck": _WGET}}, {"loki"}),
        ({"otel-collector": {"healthcheck": {"test": "true"}}}, {"otel-collector"}),
        # Negative controls: an image with wget, and checks that run nothing.
        ({"prometheus": {"image": "prom/prometheus:v3.16.0", "healthcheck": _WGET}}, set()),
        ({"loki": {"healthcheck": {"test": ["NONE"]}}}, set()),
        ({"loki": {"healthcheck": {**_WGET, "disable": True}}}, set()),
    ],
    ids=[
        "digest",
        "registry",
        "registry-port",
        "overlay",
        "collector",
        "wget-image",
        "none",
        "disabled",
    ],
)
def test_the_guard_sees_every_way_of_naming_the_image(
    services: dict[str, Any], broken: set[str]
) -> None:
    assert set(_broken_healthchecks({"services": services})) == broken


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_no_healthcheck_runs_a_program_in_an_image_without_one(path: Path) -> None:
    broken = _broken_healthchecks(_load(path))
    assert broken == {}, f"{path.name}: these healthchecks can never pass"


def test_the_collector_uses_no_deprecated_exporter_type() -> None:
    exporters = _load(COLLECTOR_CONFIG).get("exporters") or {}
    assert exporters
    old = {
        name: DEPRECATED_EXPORTER_TYPES[name.split("/", 1)[0]]
        for name in exporters
        if name.split("/", 1)[0] in DEPRECATED_EXPORTER_TYPES
    }
    assert old == {}, f"rename these exporters: {old}"


def test_every_pipeline_exporter_is_defined() -> None:
    config = _load(COLLECTOR_CONFIG)
    defined = set(config.get("exporters") or {})
    pipelines = (config.get("service") or {}).get("pipelines") or {}
    assert pipelines
    missing = {
        signal: sorted(set(pipeline.get("exporters") or []) - defined)
        for signal, pipeline in pipelines.items()
        if set(pipeline.get("exporters") or []) - defined
    }
    assert missing == {}
