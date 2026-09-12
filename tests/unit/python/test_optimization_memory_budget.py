"""v0.3.16 PR K1 — ``MEMORY_BUDGET_TOKENS`` is read from ``optimization.yaml``.

The per-event memory budget (RFC 0017 §B / OQ1) was a module constant
from v0.2.2 to v0.3.15.  The sequencing Amendment 2026-09-12 makes it
the knob of the pre-registered experiment EXP-001 — and the first thing
a long-context operator changes — so it becomes a key in
``config/optimization.yaml``:

.. code-block:: yaml

    memory_budget:
      tokens: 1500

Three contracts, pinned here:

* the key overrides the constant, and ``0`` is a legal override (the
  zero-budget harness in ``agents/tests/test_inject_memory_context.py``
  is what an operator disabling injection would set);
* an absent key yields ``None`` from the accessor, so the persona falls
  through to the module constant (1 500) — every existing test stays
  byte-identical, including the harnesses that monkeypatch the
  constant by ``memory_context``'s name;
* a present-but-bad value is rejected loudly, the contract every
  ``memory.*`` knob shares (:mod:`agents.persona_runtime.memory_knobs`).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import jsonschema  # type: ignore[import-untyped]
import pytest
import yaml

from agents.optimization import memory_budget_tokens, reset_cache
from agents.persona_runtime.memory_budget import MEMORY_BUDGET_TOKENS
from agents.persona_runtime.memory_knobs import resolve_memory_knobs

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point ``optimization.yaml`` resolution at a per-test tmp file."""
    path = tmp_path / "optimization.yaml"
    monkeypatch.setenv("PERSATRIX_OPTIMIZATION_CONFIG", str(path))
    reset_cache()
    yield path
    reset_cache()


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


# ─── The accessor ──────────────────────────────────────────


class TestMemoryBudgetTokensAccessor:
    def test_absent_block_yields_none(self, config_path: Path) -> None:
        """No ``memory_budget`` block → ``None`` (the constant applies)."""
        _write(config_path, "schema_version: '0.3'\n")
        assert memory_budget_tokens() is None

    def test_missing_file_yields_none(self, config_path: Path) -> None:
        assert not config_path.exists()
        assert memory_budget_tokens() is None

    def test_block_without_tokens_yields_none(self, config_path: Path) -> None:
        _write(config_path, "memory_budget: {}\n")
        assert memory_budget_tokens() is None

    def test_key_overrides_the_constant(self, config_path: Path) -> None:
        _write(config_path, "memory_budget:\n  tokens: 300\n")
        assert memory_budget_tokens() == 300
        assert memory_budget_tokens() != MEMORY_BUDGET_TOKENS

    def test_zero_is_a_legal_override(self, config_path: Path) -> None:
        """``0`` disables injection — the zero-budget retune, now in config."""
        _write(config_path, "memory_budget:\n  tokens: 0\n")
        assert memory_budget_tokens() == 0

    @pytest.mark.parametrize(
        "body",
        [
            "memory_budget:\n  tokens: -1\n",
            "memory_budget:\n  tokens: true\n",
            "memory_budget:\n  tokens: '300'\n",
            "memory_budget:\n  tokens: 1.5\n",
            "memory_budget:\n  tokens: null\n",
            "memory_budget: 300\n",
        ],
        ids=["negative", "bool", "string", "float", "null", "scalar-block"],
    )
    def test_bad_value_is_rejected_loudly(self, config_path: Path, body: str) -> None:
        """A present-but-bad value raises and names the key (never degrades)."""
        _write(config_path, body)
        with pytest.raises(ValueError, match="memory_budget.tokens"):
            memory_budget_tokens()


# ─── The persona knob ──────────────────────────────────────


class TestMemoryKnobsBudget:
    def test_knob_carries_the_configured_value(self, config_path: Path) -> None:
        _write(config_path, "memory_budget:\n  tokens: 300\n")
        assert resolve_memory_knobs({}).budget_tokens == 300

    def test_knob_is_none_when_absent(self, config_path: Path) -> None:
        _write(config_path, "schema_version: '0.3'\n")
        assert resolve_memory_knobs({}).budget_tokens is None

    def test_bad_value_fails_persona_start(self, config_path: Path) -> None:
        _write(config_path, "memory_budget:\n  tokens: -5\n")
        with pytest.raises(ValueError, match="memory_budget.tokens"):
            resolve_memory_knobs({})


# ─── The shipped config and its schema ─────────────────────


def _schema() -> dict:
    return json.loads(
        (REPO_ROOT / "schemas" / "optimization.schema.json").read_text("utf-8"))


class TestShippedConfigAndSchema:
    def test_shipped_config_leaves_the_key_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The shipped file documents the key but does not set it.

        Deliberate: the integration harnesses tighten the budget by
        monkeypatching the constant, and a persona built from the shipped
        config must still see that patch.  Set the key and those tests
        would silently run at 1 500 — flip this test only with them.
        """
        monkeypatch.setenv(
            "PERSATRIX_OPTIMIZATION_CONFIG",
            str(REPO_ROOT / "config" / "optimization.yaml"))
        reset_cache()
        try:
            assert memory_budget_tokens() is None
        finally:
            reset_cache()

    def test_shipped_config_documents_the_key(self) -> None:
        text = (REPO_ROOT / "config" / "optimization.yaml").read_text("utf-8")
        assert "memory_budget:" in text
        assert "tokens: 1500" in text

    def test_schema_default_matches_the_constant(self) -> None:
        block = _schema()["properties"]["memory_budget"]
        assert block["additionalProperties"] is False
        assert block["properties"]["tokens"]["default"] == MEMORY_BUDGET_TOKENS
        assert block["properties"]["tokens"]["minimum"] == 0

    @pytest.mark.parametrize(
        ("body", "valid"),
        [
            ("memory_budget:\n  tokens: 1500\n", True),
            ("memory_budget:\n  tokens: 0\n", True),
            ("memory_budget:\n  tokens: -1\n", False),
            ("memory_budget:\n  tokens: '1500'\n", False),
            ("memory_budget:\n  token: 1500\n", False),
        ],
        ids=["default", "zero", "negative", "string", "typo"],
    )
    def test_schema_validates_the_block(self, body: str, valid: bool) -> None:
        validator = jsonschema.Draft7Validator(_schema())
        errors = list(validator.iter_errors(yaml.safe_load(body)))
        assert (not errors) is valid, [e.message for e in errors]
