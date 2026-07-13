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
instead of naming each (``[gemini,watsonx]``). This test pins two
invariants:

  1. each individual provider extra pins its native SDK, and
  2. the combined ``providers`` extra is exactly the union of every
     individual provider extra — so it cannot silently drift out of
     sync when a future provider SDK is added or a pin is bumped (the
     contributor must fold the change into ``providers`` too, or the
     union check fails).

Mirrors the ``EXCLUDE_DIRS`` waiver pattern in
``test_pyproject_packages.py``: an extra that is *not* an optional
provider-SDK bundle is an explicit entry in ``_NON_PROVIDER_EXTRAS``,
not an oversight.
"""

from __future__ import annotations

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


def _pyproject_path() -> Path:
    return Path(__file__).resolve().parents[3] / "agents" / "pyproject.toml"


def _optional_dependencies() -> dict[str, list[str]]:
    with _pyproject_path().open("rb") as fh:
        data = tomllib.load(fh)
    return data["project"]["optional-dependencies"]


def _provider_extras(extras: dict[str, list[str]]) -> dict[str, list[str]]:
    """The individual provider extras — every extra minus the waivers."""
    return {
        name: reqs
        for name, reqs in extras.items()
        if name not in _NON_PROVIDER_EXTRAS
    }


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


def test_combined_providers_extra_is_the_union_of_every_provider_extra() -> None:
    """Anti-drift: ``providers`` == union of the individual provider extras.

    Enforced on the exact requirement strings, so a bumped pin (or a newly
    added provider extra) that is not mirrored into ``providers`` fails here
    rather than shipping a combined bundle that silently omits a SDK.
    """
    extras = _optional_dependencies()
    combined = set(extras.get(_COMBINED_EXTRA, []))
    expected = {
        req for reqs in _provider_extras(extras).values() for req in reqs
    }
    assert combined == expected, (
        f"the combined '{_COMBINED_EXTRA}' extra must equal the union of every "
        "individual provider extra so it cannot drift. Missing from "
        f"'{_COMBINED_EXTRA}': {sorted(expected - combined)}; unexpected in "
        f"'{_COMBINED_EXTRA}': {sorted(combined - expected)}. Fold new provider "
        f"SDKs into '{_COMBINED_EXTRA}' (or waive a non-provider extra in "
        "_NON_PROVIDER_EXTRAS)."
    )
