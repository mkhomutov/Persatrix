"""Tests for ``SubAgentRequest`` model defaulting (RFC 0033 §J.3 / PR 3).

``SubAgentRequest.model`` was the *only* code-level vendor model literal in
the runtime. PR 3 drops the hardcoded ID to ``None`` and resolves it at
construction time to the ``sub_agents`` routing-default alias
(``default.model_routing.defaults.sub_agents`` — today ``quality``), so no
Python runtime code carries a literal vendor model ID. A caller may still
pass an explicit alias (``SubAgentRequest(..., model="fast")``); only the
``None`` sentinel is resolved.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agents import optimization
from agents.persona_types import SubAgentRequest


@pytest.fixture()
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the optimization loader at a per-test tmp file and clear the
    cache on both sides so the default resolution is deterministic."""
    path = tmp_path / "optimization.yaml"
    monkeypatch.setenv("PERSATRIX_OPTIMIZATION_CONFIG", str(path))
    optimization.reset_cache()
    yield path
    optimization.reset_cache()


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


class TestNoneDefaultResolution:
    def test_none_model_resolves_to_sub_agents_alias(self, config_path: Path) -> None:
        _write(
            config_path,
            "default:\n  model_routing:\n    defaults:\n      sub_agents: quality\n",
        )
        req = SubAgentRequest(role="helper", task="do a thing")
        assert req.model == "quality"

    def test_none_model_honours_a_distinct_routing_default(
        self, config_path: Path,
    ) -> None:
        # Prove the value comes from config, not a hardcoded "quality".
        _write(
            config_path,
            "default:\n  model_routing:\n    defaults:\n      sub_agents: persona-default\n",
        )
        req = SubAgentRequest(role="helper", task="do a thing")
        assert req.model == "persona-default"

    def test_none_model_raises_loud_when_routing_default_absent(
        self, config_path: Path,
    ) -> None:
        # No on-disk file → no `sub_agents` routing default. There is no
        # hardcoded model fallback (RFC 0033 — config owns model identity),
        # so construction fails loud naming the missing key rather than
        # routing the sub-agent to a code-baked default.
        with pytest.raises(SystemExit) as exc:
            SubAgentRequest(role="helper", task="do a thing")
        assert "sub_agents" in str(exc.value)


class TestExplicitModelHonoured:
    def test_explicit_alias_is_kept(self, config_path: Path) -> None:
        _write(
            config_path,
            "default:\n  model_routing:\n    defaults:\n      sub_agents: quality\n",
        )
        req = SubAgentRequest(role="helper", task="t", model="fast")
        assert req.model == "fast"

    def test_explicit_raw_id_is_kept(self, config_path: Path) -> None:
        # An explicit caller value of any kind is honoured untouched —
        # construction-time resolution only fills the None sentinel.
        req = SubAgentRequest(role="helper", task="t", model="claude-opus-4-1")
        assert req.model == "claude-opus-4-1"


def test_no_vendor_id_default_on_the_dataclass() -> None:
    """The dataclass default must be the ``None`` sentinel, not a vendor ID —
    this is the assertion that the last code-level model literal is gone."""
    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(SubAgentRequest)}
    assert fields["model"].default is None
