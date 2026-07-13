"""
Guards the RFC 0053 provider-SDK extras in ``agents/pyproject.toml``.

RFC 0053 ships each optional provider SDK — Google Gemini
(``google-genai``) and IBM watsonx.ai (``ibm-watsonx-ai``) — as a
pyproject *extra* (OQ #4), so a single-provider deployment carries no
new runtime dependency and the ``create_provider`` branch surfaces an
actionable ``ImportError → SystemExit`` install hint when the extra is
absent (``anthropic`` / ``openai`` are always-on base ``dependencies``;
Ollama reuses the base ``openai`` SDK; the offline ``mock`` needs
nothing — so the *only* optional provider-SDK extras are the two RFC
0053 clouds).

RFC 0053 PR 3 (closeout) adds a **combined** ``providers`` extra so an
operator standing up the four-vendor demo can pull every optional
provider SDK in one step (``pip install 'persatrix-agents[providers]'``)
instead of naming each (``[gemini,watsonx]``). It is a
*self-referencing* extra — ``Persatrix-agents[gemini,watsonx]`` — which
re-exports the individual provider extras by name rather than copying
their pins, so a bumped SDK pin is inherited automatically and can never
drift. This test pins two invariants:

  1. each individual provider extra pins its native SDK, and
  2. the combined ``providers`` extra self-references *every* individual
     provider extra — so the only thing a contributor must keep in sync
     is the set of extra *names* in the bracket: a future provider extra
     not folded in here fails the check rather than silently shipping a
     combined bundle that omits a vendor.

Mirrors the ``EXCLUDE_DIRS`` waiver pattern in
``test_pyproject_packages.py``: an extra that is *not* an optional
provider-SDK bundle is an explicit entry in ``_NON_PROVIDER_EXTRAS``,
not an oversight.

Those two invariants are *static* string guards on the TOML. The final
test (``test_providers_extra_resolves_to_every_provider_sdk``) is a live
end-to-end smoke: it shells out to ``pip install --dry-run --report``
(pip's real resolver — ``--dry-run`` installs nothing, ``--report`` writes
a JSON plan) to confirm ``Persatrix-agents[providers]`` actually *expands*
into the native SDKs — the whole point of the self-reference, which no
string check can prove. It is **opt-in** (network + ~30s, non-hermetic):
set ``PERSATRIX_PROVIDERS_RESOLUTION_SMOKE=1`` to run it. This mirrors the
repo's other live-external tests (e.g. ``requires_anthropic``) — the
hermetic static guards run in the default suite, this one runs only when
explicitly enabled, so a PyPI hiccup can never redden an unrelated push.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

import pytest

# The combined extra an operator installs to get every optional provider SDK.
_COMBINED_EXTRA = "providers"

# Extras that are NOT optional provider-SDK bundles, so they are excluded when
# computing "every individual provider extra". Keep this list tight — anything
# added here is an explicit waiver, not an oversight (mirrors
# ``test_pyproject_packages.EXCLUDE_DIRS``).
_NON_PROVIDER_EXTRAS = {
    "dev",  # lint/type/test tooling, not a runtime provider SDK
    _COMBINED_EXTRA,  # the combined bundle under test, not an individual provider
}

_NAME_NORMALIZE_RE = re.compile(r"[-_.]+")


def _pyproject_path() -> Path:
    return Path(__file__).resolve().parents[3] / "agents" / "pyproject.toml"


def _pyproject() -> dict:
    with _pyproject_path().open("rb") as fh:
        return tomllib.load(fh)


def _optional_dependencies() -> dict[str, list[str]]:
    return _pyproject()["project"]["optional-dependencies"]


def _distribution_name() -> str:
    return _pyproject()["project"]["name"]


def _provider_extras(extras: dict[str, list[str]]) -> dict[str, list[str]]:
    """The individual provider extras — every extra minus the waivers."""
    return {
        name: reqs
        for name, reqs in extras.items()
        if name not in _NON_PROVIDER_EXTRAS
    }


def _normalize(name: str) -> str:
    """PEP 503 normalized distribution name (case- and ``-_.``-insensitive)."""
    return _NAME_NORMALIZE_RE.sub("-", name).strip().lower()


def _requirement_name(requirement: str) -> str:
    """Best-effort distribution name from a PEP 508 requirement string.

    Enough to recover the distribution name for the simple pins we ship
    (``name>=x,<y`` with no URLs/extras): strip an environment marker,
    then the first version/extras delimiter.
    """
    head = requirement.split(";", 1)[0].strip()
    for sep in ("<", ">", "=", "!", "~", "[", " "):
        head = head.split(sep, 1)[0]
    return head.strip().lower()


def _self_reference_extras(requirement: str, dist_name: str) -> set[str]:
    """Extras named in a self-referencing requirement.

    ``Persatrix-agents[gemini,watsonx]`` → ``{"gemini", "watsonx"}``.
    Returns an empty set when the requirement is not a bracketed
    self-reference to ``dist_name`` (name matched PEP 503-normalized, so
    ``persatrix-agents`` and ``Persatrix-agents`` are equivalent), so the
    caller's assertion fails with a clear message rather than a KeyError.
    """
    head = requirement.split(";", 1)[0].strip()
    if "[" not in head or not head.endswith("]"):
        return set()
    name, _, bracket = head.partition("[")
    if _normalize(name) != _normalize(dist_name):
        return set()
    inner = bracket[:-1]  # drop the trailing ``]``
    return {extra.strip().lower() for extra in inner.split(",") if extra.strip()}


def test_gemini_extra_pins_the_native_google_genai_sdk() -> None:
    extras = _optional_dependencies()
    assert "gemini" in extras, "the RFC 0053 'gemini' extra is missing"
    names = {_requirement_name(r) for r in extras["gemini"]}
    assert "google-genai" in names, (
        f"the 'gemini' extra must pin the native google-genai SDK; got {names}"
    )


def test_watsonx_extra_pins_the_native_ibm_watsonx_sdk() -> None:
    extras = _optional_dependencies()
    assert "watsonx" in extras, "the RFC 0053 'watsonx' extra is missing"
    names = {_requirement_name(r) for r in extras["watsonx"]}
    assert "ibm-watsonx-ai" in names, (
        f"the 'watsonx' extra must pin the native ibm-watsonx-ai SDK; got {names}"
    )


def test_combined_providers_extra_exists_and_is_nonempty() -> None:
    """RFC 0053 PR 3: a one-step install for every optional provider SDK."""
    extras = _optional_dependencies()
    assert _COMBINED_EXTRA in extras, (
        f"agents/pyproject.toml must declare a combined '{_COMBINED_EXTRA}' "
        "extra so `pip install 'persatrix-agents[providers]'` pulls every "
        "optional provider SDK in one step (RFC 0053 PR 3 four-vendor demo)."
    )
    assert extras[_COMBINED_EXTRA], (
        f"the combined '{_COMBINED_EXTRA}' extra is declared but empty"
    )


def test_combined_providers_extra_self_references_every_provider_extra() -> None:
    """Anti-drift: ``providers`` re-exports every individual provider extra.

    ``providers`` is a *self-referencing* extra
    (``Persatrix-agents[gemini,watsonx]``) rather than a hand-copied union
    of pins, so a bumped SDK pin is inherited automatically and cannot
    drift. The one thing that must stay in sync is the *set of extra names*
    in the bracket: a newly added provider extra not folded in here would
    silently ship a combined bundle that omits a vendor. This asserts the
    bracket equals the set of every individual provider extra.
    """
    extras = _optional_dependencies()
    dist_name = _distribution_name()

    combined = extras.get(_COMBINED_EXTRA, [])
    assert len(combined) == 1, (
        f"the combined '{_COMBINED_EXTRA}' extra must be a single "
        f"self-reference (e.g. '{dist_name}[gemini,watsonx]'); got {combined}"
    )

    referenced = _self_reference_extras(combined[0], dist_name)
    assert referenced, (
        f"the combined '{_COMBINED_EXTRA}' extra must be a self-reference to "
        f"'{dist_name}' naming the individual provider extras in brackets "
        f"(e.g. '{dist_name}[gemini,watsonx]'); got {combined[0]!r}"
    )

    expected = set(_provider_extras(extras))
    assert referenced == expected, (
        f"the combined '{_COMBINED_EXTRA}' extra must self-reference every "
        "individual provider extra so it cannot drift. Missing from "
        f"'{_COMBINED_EXTRA}': {sorted(expected - referenced)}; unexpected in "
        f"'{_COMBINED_EXTRA}': {sorted(referenced - expected)}. Fold new "
        f"provider extras into the '{_COMBINED_EXTRA}' self-reference (or waive "
        "a non-provider extra in _NON_PROVIDER_EXTRAS)."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Live resolution smoke (opt-in) — RFC 0053 PR 3 (closeout).
#
# The guards above are static: they assert the ``providers`` extra is a
# well-formed self-reference *string*. They cannot prove a real resolver
# actually expands ``Persatrix-agents[providers]`` → ``[gemini,watsonx]`` → the
# two native SDKs — the whole point of the self-reference. This smoke does, by
# shelling out to ``pip install --dry-run --report`` (pip's resolution planner;
# ``--dry-run`` installs nothing, ``--report`` writes a JSON plan we parse).
#
# It is OPT-IN because it needs live PyPI (network, ~30s) and its result depends
# on PyPI state, so — like the repo's other non-hermetic live-external tests
# (``requires_anthropic``) — it must stay out of the default ``make test-python``
# suite (which is exactly the CI Python-unit job, .github/workflows/ci.yml).
# Enable it with ``PERSATRIX_PROVIDERS_RESOLUTION_SMOKE=1``. When enabled it
# genuinely runs: a broken self-reference (pip cannot expand it → resolution
# error) or a missing SDK FAILS; it is *skipped* only for environmental reasons
# (offline / pip too old / timeout), never to paper over a real defect.
#
#   PERSATRIX_PROVIDERS_RESOLUTION_SMOKE=1 \
#       python -m pytest tests/unit/python/test_pyproject_provider_extras.py -v

_RESOLUTION_SMOKE_ENV = "PERSATRIX_PROVIDERS_RESOLUTION_SMOKE"

# A cold resolution of the full provider closure over the network is ~30s here;
# 300s leaves ample headroom on a slow runner. Unlike the repo's other
# subprocess-backed tests (which set no timeout), a network call MUST cap itself
# so a stalled PyPI can never wedge the suite.
_RESOLUTION_TIMEOUT_S = 300

_requires_resolution_smoke = pytest.mark.skipif(
    not os.environ.get(_RESOLUTION_SMOKE_ENV),
    reason=(
        "live PyPI resolution smoke is opt-in (network, ~30s, non-hermetic) — "
        f"set {_RESOLUTION_SMOKE_ENV}=1 to run it"
    ),
)


def _expected_provider_sdk_names() -> set[str]:
    """The PEP 503-normalized SDK distribution names ``providers`` must pull.

    Derived from the individual provider extras — the same source of truth the
    drift guard above uses — so a newly added provider extra is automatically
    expected here too, with no hand-maintained list to fall out of sync.
    ``{"google-genai", "ibm-watsonx-ai"}`` today.
    """
    extras = _optional_dependencies()
    return {
        _normalize(_requirement_name(req))
        for reqs in _provider_extras(extras).values()
        for req in reqs
    }


def _pip_supports_dry_run_report() -> bool:
    """``pip install --dry-run --report`` requires pip >= 22.2."""
    try:
        from importlib.metadata import version

        raw = version("pip")
    except Exception:  # noqa: BLE001 — unknown pip; let the run surface it
        return True
    nums = re.findall(r"\d+", raw)
    if len(nums) < 2:
        return True
    return (int(nums[0]), int(nums[1])) >= (22, 2)


def _pypi_reachable(dist_names: set[str], timeout: float = 8.0) -> bool:
    """Fast reachability precheck for the given distributions on PyPI.

    Lets a genuine outage *skip* (environmental) rather than fail, and cleanly
    separates "network down" from "resolution broken": only when PyPI is
    reachable do we treat a non-zero ``pip`` exit as a real defect.
    """
    for name in dist_names:
        try:
            request = urllib.request.Request(
                f"https://pypi.org/pypi/{name}/json", method="HEAD"
            )
            with urllib.request.urlopen(request, timeout=timeout):
                pass
        except Exception:  # noqa: BLE001 — any network/HTTP error → unreachable
            return False
    return True


@_requires_resolution_smoke
def test_providers_extra_resolves_to_every_provider_sdk(tmp_path: Path) -> None:
    """``pip`` dry-run of ``[providers]`` resolves to every provider SDK.

    The live counterpart to the static drift guard: proof that the
    self-referencing ``providers`` extra expands, through pip's real resolver,
    into the native SDKs the individual extras pin. Nothing is installed.
    """
    if not _pip_supports_dry_run_report():
        pytest.skip("pip too old for `--dry-run --report` (needs >= 22.2)")

    expected = _expected_provider_sdk_names()
    assert expected, (
        "no individual provider extras found — the resolution smoke would be "
        "vacuous; the static guards above should have caught this first"
    )

    if not _pypi_reachable(expected):
        pytest.skip("PyPI unreachable — skipping live resolution smoke (offline)")

    agents_dir = _pyproject_path().parent
    report_path = tmp_path / "resolution-report.json"
    target = f"{agents_dir}[{_COMBINED_EXTRA}]"
    # --ignore-installed forces a full plan, so the report lists the entire
    # closure regardless of what the runner's venv already holds (an already
    # -satisfied SDK would otherwise be absent from ``install`` and falsely read
    # as "missing"). --dry-run installs nothing; --report writes the plan.
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--ignore-installed",
        "--quiet",
        "--report",
        str(report_path),
        target,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_RESOLUTION_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        pytest.skip(
            f"pip resolution exceeded {_RESOLUTION_TIMEOUT_S}s — skipping (slow network)"
        )

    # PyPI was reachable moments ago, so a non-zero exit is a real resolution
    # failure — most likely a self-reference pip cannot expand — not an outage.
    assert result.returncode == 0, (
        "`pip install --dry-run` of the combined `providers` extra failed to "
        "resolve; the self-referencing extra may be broken.\n"
        f"command: {' '.join(cmd)}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert report_path.exists(), (
        "pip reported success but wrote no --report file:\n"
        f"stdout:\n{result.stdout}"
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    resolved = {
        _normalize((entry.get("metadata") or {}).get("name", ""))
        for entry in report.get("install", [])
    }
    missing = expected - resolved
    assert not missing, (
        f"`pip install '{target}'` resolved to {sorted(resolved)}, but the "
        f"combined `{_COMBINED_EXTRA}` extra must pull every provider SDK — "
        f"missing {sorted(missing)}. The self-reference did not expand into the "
        "individual provider extras."
    )
