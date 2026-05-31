"""Eager startup alias-pricing validation tests (ISSUE-0071).

The missing-price guard (RFC 0033 PR 4) fires *per-resolve*, scoped to the
resolved alias — so an unpriced non-local alias that no agent ever resolves
is never caught at runtime (an all-local/offline society takes the
factory's force-flag early-return before ``resolve()``, never validating a
cloud alias). ISSUE-0071 closes that gap by running the whole-map validator
(``agents.model_aliases.validate_alias_pricing``) eagerly at server boot, so
a misconfigured registry fails fast and loud regardless of which provider
mode is active.

These tests drive ``agents.server_cli._validate_startup_config`` — the boot
seam that runs the whole-map check — through the ``use_alias_map`` test
seam, so they stay hermetic and never touch the shipped config cache.
"""

from __future__ import annotations

import pytest

from agents import server_cli
from agents.model_aliases import use_alias_map

# A priced non-local alias: passes the whole-map guard.
_PRICED = {
    "quality": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "input_per_1m_tokens": 3.0,
        "output_per_1m_tokens": 15.0,
    },
}

# A non-local alias with no pricing: the silent-$0 budget hole ISSUE-0071
# closes at boot.
_UNPRICED_NON_LOCAL = {
    "quality": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "input_per_1m_tokens": 3.0,
        "output_per_1m_tokens": 15.0,
    },
    "orphan": {
        "provider": "openai",
        "model": "gpt-4o",
        # no input_per_1m_tokens / output_per_1m_tokens — never resolved by
        # any agent, so the per-resolve guard never sees it.
    },
}

# A local ($0-by-design) alias with no pricing: legitimate, must boot clean.
_LOCAL_UNPRICED = {
    "quality": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "input_per_1m_tokens": 3.0,
        "output_per_1m_tokens": 15.0,
    },
    "local": {
        "provider": "ollama",
        "model": "llama3",
    },
}


def test_validate_startup_config_passes_on_priced_map() -> None:
    """A fully-priced map boots clean — no raise."""
    with use_alias_map(_PRICED):
        server_cli._validate_startup_config()  # must not raise


def test_validate_startup_config_fails_on_unpriced_non_local_alias() -> None:
    """An unpriced non-local alias fails the eager boot check loudly,
    even though no agent ever resolves it."""
    with use_alias_map(_UNPRICED_NON_LOCAL):
        with pytest.raises(SystemExit):
            server_cli._validate_startup_config()


def test_validate_startup_config_boot_error_names_offender() -> None:
    """The boot failure names the offending alias so the operator can find
    it in config/optimization.yaml."""
    with use_alias_map(_UNPRICED_NON_LOCAL):
        with pytest.raises(SystemExit, match="orphan"):
            server_cli._validate_startup_config()


def test_validate_startup_config_allows_local_unpriced_alias() -> None:
    """A local ($0-real) alias with no pricing is by design — it must not
    trip the eager boot check."""
    with use_alias_map(_LOCAL_UNPRICED):
        server_cli._validate_startup_config()  # must not raise
