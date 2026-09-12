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
  zero-budget retune an operator disabling injection would set);
* an absent block or key yields ``None`` from the accessor, and
  ``resolve_memory_knobs`` applies the module constant (1 500) — so the
  shipped config, which documents the key but does not set it, leaves
  every prompt byte-identical;
* a present-but-bad value — or an unknown key in the block, the
  misspelling that would otherwise read as "not configured" — is
  rejected loudly, the contract every ``memory.*`` knob shares
  (:mod:`agents.persona_runtime.memory_knobs`), and the message names
  the file that was actually loaded.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from agents.optimization import memory_budget_tokens, reset_cache
from agents.persona_runtime.memory_budget import MEMORY_BUDGET_TOKENS
from agents.persona_runtime.memory_knobs import resolve_memory_knobs
from agents.validate import validate_config_dir

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMAS_DIR = REPO_ROOT / "schemas"


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
            "memory_budget:\n  token: 0\n",
            "memory_budget:\n  tokens: 0\n  extra: 1\n",
        ],
        ids=[
            "negative", "bool", "string", "float", "null", "scalar-block",
            "misspelt-key", "unknown-key",
        ],
    )
    def test_bad_value_is_rejected_loudly(self, config_path: Path, body: str) -> None:
        """A present-but-bad value raises and names the key (never degrades).

        The two key-shape cases matter most: ``token: 0`` is the typo an
        operator makes while disabling injection, and a silent fall-through
        to 1 500 would run the wrong EXP-001 arm without a log line.
        """
        _write(config_path, body)
        with pytest.raises(ValueError, match="memory_budget.tokens"):
            memory_budget_tokens()

    def test_rejection_names_the_file_that_was_loaded(self, config_path: Path) -> None:
        """A demo overlay is bind-mounted over the pinned path under compose;
        the message must point at the file the operator actually edited."""
        _write(config_path, "memory_budget:\n  tokens: -1\n")
        with pytest.raises(ValueError, match=re.escape(str(config_path))):
            memory_budget_tokens()


# ─── The persona knob ──────────────────────────────────────


class TestMemoryKnobsBudget:
    def test_knob_carries_the_configured_value(self, config_path: Path) -> None:
        _write(config_path, "memory_budget:\n  tokens: 300\n")
        assert resolve_memory_knobs({}).budget_tokens == 300

    def test_knob_is_the_constant_when_absent(self, config_path: Path) -> None:
        """The default collapses at resolve time, like every other knob."""
        _write(config_path, "schema_version: '0.3'\n")
        assert resolve_memory_knobs({}).budget_tokens == MEMORY_BUDGET_TOKENS

    def test_zero_reaches_the_knob(self, config_path: Path) -> None:
        _write(config_path, "memory_budget:\n  tokens: 0\n")
        assert resolve_memory_knobs({}).budget_tokens == 0

    def test_bad_value_fails_persona_start(self, config_path: Path) -> None:
        _write(config_path, "memory_budget:\n  tokens: -5\n")
        with pytest.raises(ValueError, match="memory_budget.tokens"):
            resolve_memory_knobs({})


# ─── The shipped config and its schema ─────────────────────


def _schema() -> dict:
    return json.loads(
        (SCHEMAS_DIR / "optimization.schema.json").read_text("utf-8"))


class TestShippedConfigAndSchema:
    def test_shipped_config_resolves_to_the_constant(self) -> None:
        """The shipped file documents the key but does not set it, so a
        persona built from it runs the constant (every prompt byte-identical
        to v0.3.15).  ``PERSATRIX_OPTIMIZATION_CONFIG`` is unset here by the
        suite-wide ``isolate_optimization_config`` fixture."""
        assert memory_budget_tokens() is None
        assert resolve_memory_knobs({}).budget_tokens == MEMORY_BUDGET_TOKENS

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
    def test_schema_validates_the_block(
        self, tmp_path: Path, body: str, valid: bool,
    ) -> None:
        """Through the validator ``make validate`` runs, not a private copy."""
        _write(tmp_path / "optimization.yaml", body)
        assert yaml.safe_load(body)  # the body is a real document
        ok, errors, checked = validate_config_dir(str(tmp_path), str(SCHEMAS_DIR))
        assert checked == 1
        assert ok is valid, [e.message for e in errors]
