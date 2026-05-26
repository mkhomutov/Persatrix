"""Tests for the RFC 0033 PR 4 missing-price guard.

Split out of ``test_model_aliases.py`` to keep that module under the repo
file-size cap. The guard (``agents.model_aliases.validate_alias_pricing``)
closes *the one genuine safety regression the "Any Model, Any Provider"
theme makes reachable* (provider-parity amendment item 1): the Go cost
path's ``EstimateCost`` returns ``$0`` for any model absent from the
pricing table, which silently disables the RFC 0023 pre-call lease /
budget gate for that agent. A non-local alias with no price is a loud
``SystemExit``; a local ($0-by-design) alias is distinguishable from an
unpriced one and passes silently; the raw-ID fall-through (§E) is left
untouched (that back-compat half-step is Phase 3's to close).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from agents import optimization
from agents.model_aliases import resolve, use_alias_map, validate_alias_pricing

# A priced, self-contained alias map for the seam — mirrors the shape the
# shipped config/optimization.yaml carries without depending on the file.
_SAMPLE_ALIASES: dict[str, dict[str, object]] = {
    "quality": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "input_per_1m_tokens": 3.00,
        "output_per_1m_tokens": 15.00,
    },
    "fast": {
        "provider": "anthropic",
        "model": "claude-haiku-4-5-20251001",
        "input_per_1m_tokens": 0.80,
        "output_per_1m_tokens": 4.00,
    },
    "quality-openai": {
        "provider": "openai",
        "model": "gpt-4o",
        "input_per_1m_tokens": 2.50,
        "output_per_1m_tokens": 10.00,
    },
    "local-fast": {
        "provider": "ollama",
        "model": "llama3.1",
        "input_per_1m_tokens": 0,
        "output_per_1m_tokens": 0,
        "provider_config": {"base_url": "http://localhost:11434/v1"},
    },
}


@pytest.fixture()
def isolated_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Point the optimization loader at an absent file so the raw-ID
    fall-through uses the resolver's deterministic module fallbacks,
    independent of the developer's checked-out config. Cache cleared on
    both sides so this test and its neighbours never leak a stale parse."""
    monkeypatch.setenv(
        "PERSATRIX_OPTIMIZATION_CONFIG", str(tmp_path / "absent.yaml"),
    )
    optimization.reset_cache()
    yield
    optimization.reset_cache()


class TestMissingPriceGuard:
    """RFC 0033 PR 4 / amendment item 1 — the missing-price guard.

    ``EstimateCost`` returns ``$0`` for any model absent from the Go
    pricing table, which silently disables the RFC 0023 pre-call lease
    gate for that agent. "Any Model" makes that reachable through the
    alias surface (an operator-added cloud alias, or a real Ollama tag,
    with no price). :func:`validate_alias_pricing` closes it **for the
    alias surface**: a non-local alias with no pricing is a loud
    ``SystemExit``; a local ($0-by-design) alias is distinguishable from
    an unpriced one and passes silently; the raw-ID fall-through (§E) is
    left untouched.
    """

    # ── non-local, unpriced → fail closed ──
    def test_unpriced_anthropic_alias_fails_closed(self) -> None:
        with use_alias_map({"q": {
            "provider": "anthropic", "model": "claude-sonnet-4-6",
        }}), pytest.raises(SystemExit) as exc:
            validate_alias_pricing()
        assert "q" in str(exc.value)
        assert "anthropic" in str(exc.value)

    def test_unpriced_openai_alias_fails_closed(self) -> None:
        with use_alias_map({"qo": {
            "provider": "openai", "model": "gpt-4o",
        }}), pytest.raises(SystemExit) as exc:
            validate_alias_pricing()
        assert "qo" in str(exc.value)
        assert "openai" in str(exc.value)

    def test_partially_priced_non_local_alias_fails_closed(self) -> None:
        # Input priced, output absent → still a budget hole; require both.
        with use_alias_map({"half": {
            "provider": "anthropic", "model": "claude-sonnet-4-6",
            "input_per_1m_tokens": 3.0,
        }}), pytest.raises(SystemExit) as exc:
            validate_alias_pricing()
        assert "half" in str(exc.value)

    def test_non_numeric_price_non_local_fails_closed(self) -> None:
        # A present-but-non-numeric price is as good as absent for the Go
        # cost table — a non-local alias with it fails closed.
        with use_alias_map({"bad": {
            "provider": "anthropic", "model": "claude-sonnet-4-6",
            "input_per_1m_tokens": "free", "output_per_1m_tokens": "free",
        }}), pytest.raises(SystemExit):
            validate_alias_pricing()

    def test_bool_price_non_local_fails_closed(self) -> None:
        # bool is an int subclass; a stray YAML true/false is not a real
        # price, so a non-local alias carrying it fails closed (mirrors
        # _coerce_price's bool exclusion).
        with use_alias_map({"booly": {
            "provider": "anthropic", "model": "claude-sonnet-4-6",
            "input_per_1m_tokens": True, "output_per_1m_tokens": False,
        }}), pytest.raises(SystemExit):
            validate_alias_pricing()

    def test_guard_logs_loudly_before_failing(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.ERROR, logger="agents.model_aliases"):
            with use_alias_map({"q": {
                "provider": "anthropic", "model": "claude-sonnet-4-6",
            }}), pytest.raises(SystemExit):
                validate_alias_pricing()
        assert any("q" in r.getMessage() for r in caplog.records)

    # ── priced non-local → passes ──
    def test_priced_non_local_alias_passes(self) -> None:
        with use_alias_map(_SAMPLE_ALIASES):
            validate_alias_pricing()  # quality / fast / quality-openai priced

    def test_zero_priced_non_local_alias_passes_silently(self) -> None:
        # An explicit 0 (simulation price) on a cloud alias is a deliberate
        # choice, not a forgotten price — present-and-numeric, so it passes.
        with use_alias_map({"sim": {
            "provider": "anthropic", "model": "claude-sonnet-4-6",
            "input_per_1m_tokens": 0, "output_per_1m_tokens": 0,
        }}):
            validate_alias_pricing()

    # ── local providers → $0-by-design, distinguishable from unpriced ──
    def test_ollama_alias_without_pricing_passes(self) -> None:
        with use_alias_map({"local": {
            "provider": "ollama", "model": "llama3.1",
        }}):
            validate_alias_pricing()

    def test_mock_alias_without_pricing_passes(self) -> None:
        with use_alias_map({"sim": {
            "provider": "mock", "model": "mock-model",
        }}):
            validate_alias_pricing()

    def test_localhost_base_url_openai_alias_without_pricing_passes(self) -> None:
        # An OpenAI-compatible provider pointed at a local endpoint is a
        # local ($0-real) model even though provider == "openai".
        for base_url in (
            "http://localhost:1234/v1",
            "http://127.0.0.1:1234/v1",
            "http://[::1]:1234/v1",
        ):
            with use_alias_map({"local-oai": {
                "provider": "openai", "model": "local-model",
                "provider_config": {"base_url": base_url},
            }}):
                validate_alias_pricing()

    def test_remote_base_url_openai_alias_without_pricing_fails(self) -> None:
        # A remote base_url does not make the alias local — still fail closed.
        with use_alias_map({"remote-oai": {
            "provider": "openai", "model": "gpt-4o",
            "provider_config": {"base_url": "https://api.openai.com/v1"},
        }}), pytest.raises(SystemExit):
            validate_alias_pricing()

    def test_non_loopback_host_is_not_local_must_be_explicit(self) -> None:
        # Locality is loopback-only by design: a self-hosted model reached
        # over a LAN/remote address (a private IP or a hostname) is NOT
        # auto-detected as local — only the loopback hosts in _LOCALHOST_HOSTS
        # are. The operator must be explicit on a non-loopback endpoint: carry
        # real pricing, or an explicit 0 for a $0-real box (the SystemExit
        # message documents that escape hatch). An unpriced non-loopback alias
        # therefore fails closed, like any other non-local alias. Pinning this
        # guards against a future change quietly auto-exempting RFC 1918 /
        # hostname endpoints without revisiting the escape-hatch story.
        for base_url in (
            "http://192.168.1.50:8000/v1",  # private LAN IP (vLLM / LM Studio)
            "http://10.0.0.5:11434/v1",     # private LAN IP (OpenAI-compatible)
            "http://gpu-box.local:8000/v1",  # LAN hostname
        ):
            with use_alias_map({"selfhosted": {
                "provider": "openai", "model": "local-model",
                "provider_config": {"base_url": base_url},
            }}), pytest.raises(SystemExit):
                validate_alias_pricing()

    def test_empty_map_passes(self) -> None:
        with use_alias_map({}):
            validate_alias_pricing()

    # ── raw-ID fall-through is NOT subject to the guard (§E) ──
    def test_raw_id_fall_through_not_failed_closed(
        self, isolated_config: None,
    ) -> None:
        # A valid (all-priced) map; resolving a raw vendor ID still degrades
        # to a $0 record rather than tripping the fail-closed guard — that
        # back-compat half-step is Phase 3's to close, not PR 4's.
        with use_alias_map(_SAMPLE_ALIASES):
            resolved = resolve("claude-sonnet-4-20250514")
        assert resolved.raw is True
        assert resolved.input_per_1m_tokens == 0.0

    # ── the guard runs on the first config-backed resolve (not at load) ──
    def test_guard_fires_on_first_resolve_from_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Resolving an unpriced non-local alias from the on-disk config must
        # fail closed — proving the guard is wired into the config-backed
        # resolve path, not just reachable as an explicit validator. The guard
        # fires per-resolve, the first time the alias is resolved (there is no
        # eager load-time hook — that whole-config gap is ISSUE-0071).
        config = tmp_path / "optimization.yaml"
        config.write_text(
            'schema_version: "0.2"\n'
            "models:\n"
            "  aliases:\n"
            "    quality:\n"
            "      provider: anthropic\n"
            "      model: claude-sonnet-4-6\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("PERSATRIX_OPTIMIZATION_CONFIG", str(config))
        optimization.reset_cache()
        try:
            with pytest.raises(SystemExit) as exc:
                resolve("quality")
            assert "quality" in str(exc.value)
        finally:
            optimization.reset_cache()

    def test_priced_config_load_resolves_normally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = tmp_path / "optimization.yaml"
        config.write_text(
            'schema_version: "0.2"\n'
            "models:\n"
            "  aliases:\n"
            "    quality:\n"
            "      provider: anthropic\n"
            "      model: claude-sonnet-4-6\n"
            "      input_per_1m_tokens: 3.00\n"
            "      output_per_1m_tokens: 15.00\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("PERSATRIX_OPTIMIZATION_CONFIG", str(config))
        optimization.reset_cache()
        try:
            assert resolve("quality").model == "claude-sonnet-4-6"
        finally:
            optimization.reset_cache()

    def test_unrelated_unpriced_alias_does_not_break_a_good_resolve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # RFC 0033 acceptance is scoped to the *resolved* alias ("a resolved
        # non-local alias with no price fails closed"). The per-resolve guard
        # must therefore check only the alias being resolved — an unrelated,
        # unused misconfigured alias elsewhere in the config must NOT break a
        # well-formed resolve. Whole-map validation stays available via
        # validate_alias_pricing() for an explicit startup/CI check. Without
        # scoping, resolving the good 'quality' alias trips on the unpriced
        # 'quality-openai' and — via summarize_close's `except SystemExit`
        # (ISSUE-0070's "correct handling" precedent) — silently, permanently
        # degrades summarisation for an all-local society over an unused typo.
        config = tmp_path / "optimization.yaml"
        config.write_text(
            'schema_version: "0.2"\n'
            "models:\n"
            "  aliases:\n"
            "    quality:\n"
            "      provider: anthropic\n"
            "      model: claude-sonnet-4-6\n"
            "      input_per_1m_tokens: 3.00\n"
            "      output_per_1m_tokens: 15.00\n"
            "    quality-openai:\n"  # unused; operator forgot pricing
            "      provider: openai\n"
            "      model: gpt-4o\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("PERSATRIX_OPTIMIZATION_CONFIG", str(config))
        optimization.reset_cache()
        try:
            # The well-priced alias resolves despite the unrelated bad entry.
            assert resolve("quality").model == "claude-sonnet-4-6"
            # The misconfigured alias still fails closed when actually used.
            with pytest.raises(SystemExit) as exc:
                resolve("quality-openai")
            assert "quality-openai" in str(exc.value)
        finally:
            optimization.reset_cache()

    def test_shipped_config_passes_guard(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The shipped config/optimization.yaml must satisfy its own guard:
        # every non-local alias is priced; local-fast (ollama) is $0-by-design.
        monkeypatch.delenv("PERSATRIX_OPTIMIZATION_CONFIG", raising=False)
        optimization.reset_cache()
        try:
            if not optimization.model_aliases():
                pytest.skip("config/optimization.yaml absent in this checkout")
            validate_alias_pricing()
        finally:
            optimization.reset_cache()
