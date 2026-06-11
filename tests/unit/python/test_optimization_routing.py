"""Tests for the RFC 0033 routing-default accessors in ``agents.optimization``.

PR 3 migrates ``default.model_routing.defaults`` and the
``context_management.summarization.model`` field from raw vendor IDs to
model aliases. ``model_routing_defaults()`` and ``sub_agent_default_model()``
are the read side the sub-agent ``None``-default resolution (RFC 0033 §J.3)
sits on; both are profile-aware (active profile, then ``default``).

Kept in a module separate from ``test_optimization.py`` to stay under the
repo's per-file line cap.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agents import optimization
from agents.optimization import (
    model_routing_defaults,
    reset_cache,
    sub_agent_default_model,
    summarization_model,
)


@pytest.fixture()
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point ``optimization.yaml`` resolution at a per-test tmp file and clear
    the cache on both sides so a stale parse cannot leak across tests."""
    path = tmp_path / "optimization.yaml"
    monkeypatch.setenv("PERSATRIX_OPTIMIZATION_CONFIG", str(path))
    reset_cache()
    yield path
    reset_cache()


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


class TestModelRoutingDefaults:
    """``model_routing_defaults()`` exposes ``<profile>.model_routing.defaults``.

    PR 3 migrates these values from raw vendor IDs to alias names
    (``task_agents`` / ``sub_agents`` → ``quality``, ``evaluators`` →
    ``fast``); the accessor is profile-aware (active profile, then
    ``default``) so the resolution chain is uniform.
    """

    def test_missing_file_returns_empty_dict(self, config_path: Path) -> None:
        assert model_routing_defaults() == {}

    def test_profile_without_model_routing_returns_empty_dict(
        self, config_path: Path,
    ) -> None:
        _write_yaml(config_path, "default:\n  caching:\n    exact:\n      enabled: true\n")
        assert model_routing_defaults() == {}

    def test_routing_without_defaults_returns_empty_dict(
        self, config_path: Path,
    ) -> None:
        _write_yaml(
            config_path,
            "default:\n  model_routing:\n    something_else:\n"
            "      foo: [bar]\n",
        )
        assert model_routing_defaults() == {}

    def test_default_profile_resolves(self, config_path: Path) -> None:
        _write_yaml(
            config_path,
            "default:\n  model_routing:\n    defaults:\n"
            "      task_agents: quality\n"
            "      sub_agents: quality\n"
            "      evaluators: fast\n",
        )
        assert model_routing_defaults() == {
            "task_agents": "quality",
            "sub_agents": "quality",
            "evaluators": "fast",
        }

    def test_active_profile_overrides_default(self, config_path: Path) -> None:
        _write_yaml(
            config_path,
            "active_profile: experimental\n"
            "default:\n  model_routing:\n    defaults:\n      sub_agents: quality\n"
            "experimental:\n  model_routing:\n    defaults:\n      sub_agents: fast\n",
        )
        assert model_routing_defaults() == {"sub_agents": "fast"}

    def test_non_str_values_filtered(self, config_path: Path) -> None:
        # A scalar that is not a string (e.g. a list typo) is dropped rather
        # than surfaced — the resolver only ever resolves string references.
        _write_yaml(
            config_path,
            "default:\n  model_routing:\n    defaults:\n"
            "      sub_agents: quality\n"
            "      evaluators: [fast]\n",
        )
        assert model_routing_defaults() == {"sub_agents": "quality"}


class TestSubAgentDefaultModel:
    """``sub_agent_default_model()`` is the alias a code-spawned sub-agent
    routes to when its ``SubAgentRequest`` carries no explicit model
    (RFC 0033 §J.3) — the ``sub_agents`` routing default.

    There is **no hardcoded fallback**: an absent routing default is a loud
    ``SystemExit``, not a code-baked model the operator never chose."""

    def test_reads_sub_agents_routing_default(self, config_path: Path) -> None:
        _write_yaml(
            config_path,
            "default:\n  model_routing:\n    defaults:\n      sub_agents: some-alias\n",
        )
        assert sub_agent_default_model() == "some-alias"

    def test_missing_config_raises_loud(self, config_path: Path) -> None:
        # No config → no routing default → fail loud naming the missing key,
        # rather than silently routing to a code-baked default.
        with pytest.raises(SystemExit) as exc:
            sub_agent_default_model()
        assert "sub_agents" in str(exc.value)

    def test_defaults_without_sub_agents_raises_loud(
        self, config_path: Path,
    ) -> None:
        _write_yaml(
            config_path,
            "default:\n  model_routing:\n    defaults:\n      task_agents: quality\n",
        )
        with pytest.raises(SystemExit) as exc:
            sub_agent_default_model()
        assert "sub_agents" in str(exc.value)


class TestShippedYamlRoutingMigration:
    """Pin the PR 3 migration in the on-disk ``config/optimization.yaml``:
    the routing defaults and the summarization model reference aliases, not
    raw vendor IDs (mirrors ``test_optimization.TestShippedYamlModelAliases``)."""

    def test_shipped_routing_defaults_reference_aliases(self) -> None:
        import os

        os.environ.pop("PERSATRIX_OPTIMIZATION_CONFIG", None)
        reset_cache()
        try:
            defaults = model_routing_defaults()
        finally:
            reset_cache()
        if not defaults:
            pytest.skip("config/optimization.yaml absent in this checkout")
        assert defaults["task_agents"] == "quality"
        assert defaults["sub_agents"] == "quality"
        assert defaults["evaluators"] == "fast"

    def test_shipped_summarization_model_is_summarizer_alias(self) -> None:
        import os

        os.environ.pop("PERSATRIX_OPTIMIZATION_CONFIG", None)
        reset_cache()
        try:
            model = summarization_model()
        finally:
            reset_cache()
        # No hardcoded fallback: a config-less checkout yields "" (the close
        # path then degrades). With the shipped file it must be the alias.
        if not model:
            pytest.skip("config/optimization.yaml absent in this checkout")
        assert model == "summarizer"


class TestCwdConfigFallback:
    """Container path resolution: the Docker images pip-install the
    agents tree as ``persatrix_agents``, so the package-relative default
    (``Path(__file__)…/config/optimization.yaml``) points into
    site-packages, where no config exists — every accessor silently
    returned its default (``summarization_model() == ""`` → the
    close-path summary degraded with a per-close WARN).  The loader now
    falls back to the CWD-relative ``config/optimization.yaml`` (the
    compose bind-mount at WORKDIR /app) when the package-relative file
    is absent and no env override is set."""

    def test_falls_back_to_cwd_when_package_path_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("PERSATRIX_OPTIMIZATION_CONFIG", raising=False)
        monkeypatch.setattr(
            optimization, "_DEFAULT_CONFIG_PATH",
            tmp_path / "no-such-dir" / "optimization.yaml",
        )
        cwd_cfg = tmp_path / "config"
        cwd_cfg.mkdir()
        _write_yaml(
            cwd_cfg / "optimization.yaml",
            "default:\n"
            "  context_management:\n"
            "    summarization:\n"
            "      model: \"summarizer\"\n",
        )
        monkeypatch.chdir(tmp_path)
        reset_cache()
        try:
            assert summarization_model() == "summarizer"
        finally:
            reset_cache()

    def test_package_path_wins_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("PERSATRIX_OPTIMIZATION_CONFIG", raising=False)
        pkg_cfg = tmp_path / "pkg-config" / "optimization.yaml"
        pkg_cfg.parent.mkdir()
        _write_yaml(
            pkg_cfg,
            "default:\n"
            "  context_management:\n"
            "    summarization:\n"
            "      model: \"from-package-path\"\n",
        )
        monkeypatch.setattr(optimization, "_DEFAULT_CONFIG_PATH", pkg_cfg)
        cwd_cfg = tmp_path / "config"
        cwd_cfg.mkdir()
        _write_yaml(
            cwd_cfg / "optimization.yaml",
            "default:\n"
            "  context_management:\n"
            "    summarization:\n"
            "      model: \"from-cwd\"\n",
        )
        monkeypatch.chdir(tmp_path)
        reset_cache()
        try:
            assert summarization_model() == "from-package-path"
        finally:
            reset_cache()


def test_module_exports_routing_accessors() -> None:
    """Pin the new routing accessors on the public surface — the sub-agent
    default-model resolution (RFC 0033 §J.3) imports them."""
    assert "model_routing_defaults" in optimization.__all__
    assert "sub_agent_default_model" in optimization.__all__
