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
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

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
