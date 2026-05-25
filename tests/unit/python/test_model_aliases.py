"""Tests for the RFC 0033 model-alias resolver (``agents.model_aliases``).

The resolver is the single source of truth for model identity: it maps a
logical alias (``quality`` / ``fast`` / ``summarizer``) to a concrete
``(provider, model, pricing)`` record, and passes a raw vendor model ID
through unchanged during the cutover window (RFC 0033 §E). This module
pins that contract:

* an alias hit returns the configured :class:`ResolvedModel`;
* a recognised raw vendor ID falls through with ``alias=None, raw=True``;
* a string that is neither a declared alias nor a recognised vendor
  prefix is a loud ``SystemExit`` (the "clear up-front error" the RFC
  substitutes for ``_infer_provider``'s silent default-to-openai);
* the context-manager test seam registers a temporary map for the
  duration of a ``with`` block without mutating the process singleton.

PR 1 ships the resolver *unconsumed* — ``create_provider`` is not rewired
until PR 2 — so these unit tests are the only thing exercising it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agents import optimization
from agents.model_aliases import ResolvedModel, resolve, use_alias_map

# A self-contained alias map for the seam. Mirrors the shape PR 1 ships in
# config/optimization.yaml without depending on the on-disk file.
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
    """Point the optimization loader at an absent file.

    With no on-disk ``provider_inference`` block, ``provider_inference()``
    returns ``{}`` and the resolver's raw fall-through uses its module
    fallback prefixes — so the raw-ID tests are deterministic regardless
    of the developer's checked-out ``config/optimization.yaml``. The cache
    is cleared on both sides so neither this test nor its neighbours leak
    a stale parse.
    """
    monkeypatch.setenv(
        "PERSATRIX_OPTIMIZATION_CONFIG", str(tmp_path / "absent.yaml"),
    )
    optimization.reset_cache()
    yield
    optimization.reset_cache()


class TestAliasHit:
    def test_alias_returns_configured_record(self) -> None:
        with use_alias_map(_SAMPLE_ALIASES):
            resolved = resolve("quality")
        assert resolved == ResolvedModel(
            alias="quality",
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_per_1m_tokens=3.00,
            output_per_1m_tokens=15.00,
            provider_config={},
            raw=False,
        )

    def test_alias_is_not_raw(self) -> None:
        with use_alias_map(_SAMPLE_ALIASES):
            assert resolve("fast").raw is False
            assert resolve("fast").alias == "fast"

    def test_openai_alias_round_trips(self) -> None:
        with use_alias_map(_SAMPLE_ALIASES):
            resolved = resolve("quality-openai")
        assert resolved.provider == "openai"
        assert resolved.model == "gpt-4o"
        assert resolved.input_per_1m_tokens == 2.50
        assert resolved.output_per_1m_tokens == 10.00

    def test_provider_config_passes_through(self) -> None:
        with use_alias_map(_SAMPLE_ALIASES):
            resolved = resolve("local-fast")
        assert resolved.provider == "ollama"
        assert resolved.provider_config == {"base_url": "http://localhost:11434/v1"}

    def test_alias_pricing_coerced_to_float(self) -> None:
        # local-fast carries integer 0 pricing in the map; the resolved
        # record exposes it as float so downstream cost math is uniform.
        with use_alias_map(_SAMPLE_ALIASES):
            resolved = resolve("local-fast")
        assert resolved.input_per_1m_tokens == 0.0
        assert isinstance(resolved.input_per_1m_tokens, float)

    def test_bool_pricing_coerced_to_zero(self) -> None:
        # bool is an int subclass; the coercion deliberately excludes it so
        # a stray YAML ``true`` / ``false`` in a pricing field reads as $0
        # rather than leaking through as 1.0 / 0.0. Exercised through the
        # public resolver, not _coerce_price directly.
        with use_alias_map({"booly": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "input_per_1m_tokens": True,
            "output_per_1m_tokens": False,
        }}):
            resolved = resolve("booly")
        assert resolved.input_per_1m_tokens == 0.0
        assert resolved.output_per_1m_tokens == 0.0
        assert isinstance(resolved.input_per_1m_tokens, float)


class TestRawFallThrough:
    def test_raw_anthropic_id_falls_through(self, isolated_config: None) -> None:
        with use_alias_map(_SAMPLE_ALIASES):
            resolved = resolve("claude-sonnet-4-20250514")
        assert resolved.alias is None
        assert resolved.raw is True
        assert resolved.provider == "anthropic"
        assert resolved.model == "claude-sonnet-4-20250514"

    def test_raw_openai_prefix_id_falls_through(self, isolated_config: None) -> None:
        with use_alias_map(_SAMPLE_ALIASES):
            resolved = resolve("gpt-4o-mini")
        assert resolved.alias is None
        assert resolved.raw is True
        assert resolved.provider == "openai"
        assert resolved.model == "gpt-4o-mini"

    def test_raw_openai_exact_id_falls_through(self, isolated_config: None) -> None:
        with use_alias_map(_SAMPLE_ALIASES):
            resolved = resolve("o3")
        assert resolved.alias is None
        assert resolved.raw is True
        assert resolved.provider == "openai"

    def test_raw_fall_through_carries_no_pricing(self, isolated_config: None) -> None:
        # The resolver has no pricing for an arbitrary raw vendor ID; it
        # degrades to 0.0 (the Go cost path keys pricing off telemetry,
        # RFC 0033 §F). PR 4's missing-price guard leaves this path alone.
        with use_alias_map(_SAMPLE_ALIASES):
            resolved = resolve("claude-sonnet-4-20250514")
        assert resolved.input_per_1m_tokens == 0.0
        assert resolved.output_per_1m_tokens == 0.0


class TestUnknownString:
    def test_unknown_string_raises_systemexit(self, isolated_config: None) -> None:
        with use_alias_map(_SAMPLE_ALIASES), pytest.raises(SystemExit) as exc:
            resolve("totally-made-up-model")
        assert "totally-made-up-model" in str(exc.value)

    def test_alias_typo_raises_rather_than_silently_routing(
        self, isolated_config: None,
    ) -> None:
        # A typo'd alias name matches neither the map nor a vendor prefix —
        # fail loudly instead of defaulting to a provider.
        with use_alias_map(_SAMPLE_ALIASES), pytest.raises(SystemExit) as exc:
            resolve("qualtiy")
        assert "qualtiy" in str(exc.value)

    def test_empty_string_raises_systemexit(self, isolated_config: None) -> None:
        with use_alias_map(_SAMPLE_ALIASES), pytest.raises(SystemExit):
            resolve("")


class TestMalformedAliasEntry:
    """A *declared* alias whose entry is structurally broken is a loud
    ``SystemExit`` naming the alias — not a silently degraded record.

    The accessor (:func:`optimization.model_aliases`) drops non-dict
    *entries*, but a dict entry that is missing a required field or carries
    the wrong type reaches the resolver, where ``_from_alias_entry``'s
    guards catch it. These feed such entries straight through the seam
    (which bypasses the accessor's dict filter) to pin those guards.
    """

    def test_entry_missing_provider_raises(self) -> None:
        with use_alias_map({"broken": {
            "model": "claude-sonnet-4-6",
            "input_per_1m_tokens": 3.0,
            "output_per_1m_tokens": 15.0,
        }}), pytest.raises(SystemExit) as exc:
            resolve("broken")
        assert "broken" in str(exc.value)
        assert "provider" in str(exc.value)

    def test_entry_empty_provider_raises(self) -> None:
        # Present-but-falsy (empty string) is still a missing provider.
        with use_alias_map({"broken": {
            "provider": "",
            "model": "claude-sonnet-4-6",
            "input_per_1m_tokens": 3.0,
            "output_per_1m_tokens": 15.0,
        }}), pytest.raises(SystemExit) as exc:
            resolve("broken")
        assert "provider" in str(exc.value)

    def test_entry_missing_model_raises(self) -> None:
        with use_alias_map({"broken": {
            "provider": "anthropic",
            "input_per_1m_tokens": 3.0,
            "output_per_1m_tokens": 15.0,
        }}), pytest.raises(SystemExit) as exc:
            resolve("broken")
        assert "broken" in str(exc.value)
        assert "model" in str(exc.value)

    def test_entry_provider_config_not_mapping_raises(self) -> None:
        # A non-empty, non-dict provider_config is a config type error. The
        # value must be truthy to reach the guard — ``entry.get(...) or {}``
        # coerces an empty list/None to {} first; a string trips it.
        with use_alias_map({"broken": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "provider_config": "not-a-mapping",
            "input_per_1m_tokens": 3.0,
            "output_per_1m_tokens": 15.0,
        }}), pytest.raises(SystemExit) as exc:
            resolve("broken")
        assert "provider_config" in str(exc.value)


class TestTestSeam:
    def test_seam_map_is_visible_inside_block(self) -> None:
        with use_alias_map({"only": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "input_per_1m_tokens": 3.0,
            "output_per_1m_tokens": 15.0,
        }}):
            assert resolve("only").model == "claude-sonnet-4-6"

    def test_seam_restores_previous_map_on_exit(self) -> None:
        outer = {"outer": {
            "provider": "anthropic", "model": "claude-outer",
            "input_per_1m_tokens": 1.0, "output_per_1m_tokens": 2.0,
        }}
        inner = {"inner": {
            "provider": "anthropic", "model": "claude-inner",
            "input_per_1m_tokens": 1.0, "output_per_1m_tokens": 2.0,
        }}
        with use_alias_map(outer):
            assert resolve("outer").model == "claude-outer"
            with use_alias_map(inner):
                assert resolve("inner").model == "claude-inner"
            # Inner seam torn down — outer map is visible again.
            assert resolve("outer").model == "claude-outer"

    def test_seam_restores_on_exception(self) -> None:
        outer = {"outer": {
            "provider": "anthropic", "model": "claude-outer",
            "input_per_1m_tokens": 1.0, "output_per_1m_tokens": 2.0,
        }}
        with use_alias_map(outer):
            with pytest.raises(RuntimeError), use_alias_map({}):
                raise RuntimeError("boom")
            # The failed inner block still restored the outer map.
            assert resolve("outer").model == "claude-outer"

    def test_seam_does_not_mutate_optimization_singleton(
        self, isolated_config: None,
    ) -> None:
        # Entering the seam must not touch the optimization.yaml cache:
        # after the block, the config-backed map is whatever the loader
        # returns (empty here, since the file is absent), not the seam map.
        before = optimization.model_aliases()
        with use_alias_map(_SAMPLE_ALIASES):
            assert resolve("quality").model == "claude-sonnet-4-6"
        after = optimization.model_aliases()
        assert before == after == {}


class TestResolvedModel:
    def test_resolved_model_is_frozen(self) -> None:
        with use_alias_map(_SAMPLE_ALIASES):
            resolved = resolve("quality")
        with pytest.raises((AttributeError, TypeError)):
            resolved.model = "tampered"  # type: ignore[misc]


def test_module_exports_public_surface() -> None:
    import agents.model_aliases as mod

    for name in ("ResolvedModel", "resolve", "use_alias_map"):
        assert name in mod.__all__


def test_raw_fallback_constants_mirror_llm_client() -> None:
    """The raw-ID fall-through fallback prefixes are duplicated from
    ``agents.llm_client`` — they cannot be imported, because that module
    imports *this* one once the factory is rewired (RFC 0033 PR 2, a
    cycle). The module comment claims they mirror llm_client's constants;
    pin that here so the two cannot silently drift. They only bind in a
    config-less checkout (the shipped ``provider_inference`` block, pinned
    by test_optimization.py, otherwise overrides them), but in that path
    they — not the YAML — drive provider inference, so a drift would route
    raw IDs differently between the resolver and the legacy factory.
    """
    from agents import llm_client, model_aliases

    assert model_aliases._DEFAULT_OPENAI_EXACT == llm_client._OPENAI_EXACT_MODELS
    assert model_aliases._DEFAULT_OPENAI_PREFIXES == llm_client._OPENAI_PREFIX_MODELS
    # llm_client uses an inline ("claude",) literal for the anthropic
    # default (no named constant to pin against); lock ours to that value.
    assert model_aliases._DEFAULT_ANTHROPIC_PREFIXES == ("claude",)
