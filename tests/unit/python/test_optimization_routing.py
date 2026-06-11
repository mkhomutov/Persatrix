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


class TestConfigPathDeterminism:
    """Container path resolution: the Docker images pip-install the
    agents tree as ``persatrix_agents``, so the package-relative default
    (``Path(__file__)…/config/optimization.yaml``) points into
    site-packages, where no config exists — every accessor silently
    returned its default (``summarization_model() == ""`` → the
    close-path summary degraded with a per-close WARN).  The fix lives
    at the deployment layer: ``Dockerfile.agent`` pins
    ``PERSATRIX_OPTIMIZATION_CONFIG=/app/config/optimization.yaml`` (the
    in-image ``COPY config/`` / compose bind-mount location).  Library
    resolution stays deterministic — env override, else the
    package-relative default, else built-in defaults.  A CWD-relative
    fallback was considered and rejected (PR 607 review finding 6): it
    made model selection depend on whatever ``config/optimization.yaml``
    happens to exist in the process working directory, for every
    non-repo install."""

    def test_missing_package_path_yields_defaults_not_cwd_pickup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("PERSATRIX_OPTIMIZATION_CONFIG", raising=False)
        monkeypatch.setattr(
            optimization, "_DEFAULT_CONFIG_PATH",
            tmp_path / "no-such-dir" / "optimization.yaml",
        )
        # A config lurking in the CWD must NOT be picked up.
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
            assert summarization_model() == ""
        finally:
            reset_cache()

    def test_env_pinned_missing_file_warns_with_the_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An EXPLICITLY pinned config path that does not exist (a typo'd
        ENV in a derived image, a dropped ``COPY config/``) must warn and
        name the path — a DEBUG-only degrade re-enters the exact silent
        ``summarization_model() == ""`` arc the env pin exists to kill
        (PR 607 second-pass review).  The package-default path staying
        DEBUG is deliberate: absent-by-default is the normal repo case."""
        import logging

        missing = tmp_path / "nope" / "optimization.yaml"
        monkeypatch.setenv("PERSATRIX_OPTIMIZATION_CONFIG", str(missing))
        reset_cache()
        try:
            with caplog.at_level(logging.WARNING, logger="agents.optimization"):
                assert summarization_model() == ""
            assert any(
                "PERSATRIX_OPTIMIZATION_CONFIG" in r.getMessage()
                and str(missing) in r.getMessage()
                for r in caplog.records
            ), f"no WARN naming the pinned path in {[r.getMessage() for r in caplog.records]!r}"
        finally:
            reset_cache()

    def test_package_default_missing_stays_quiet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No env pin + no package config is the ordinary non-container
        default — it must not WARN on every boot."""
        import logging

        monkeypatch.delenv("PERSATRIX_OPTIMIZATION_CONFIG", raising=False)
        monkeypatch.setattr(
            optimization, "_DEFAULT_CONFIG_PATH",
            tmp_path / "no-such-dir" / "optimization.yaml",
        )
        reset_cache()
        try:
            with caplog.at_level(logging.WARNING, logger="agents.optimization"):
                assert summarization_model() == ""
            assert not caplog.records
        finally:
            reset_cache()

    def test_empty_env_override_falls_through_to_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty ``PERSATRIX_OPTIMIZATION_CONFIG`` (e.g. ``ENV VAR=``
        cleared in a derived image) means "unset", not "open the empty
        path"."""
        monkeypatch.setenv("PERSATRIX_OPTIMIZATION_CONFIG", "")
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
        reset_cache()
        try:
            assert summarization_model() == "from-package-path"
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
