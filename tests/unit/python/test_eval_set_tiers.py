"""The eval tier vocabulary is read from the schema (RFC 0044 §F, v0.3.16 PR C2).

`test_eval_set_loader.py` sits at the 500-line cap, so the tier contract
lives here: `schemas/eval_set.schema.json` is the one source of the closed
tier set and its default, `evaluators.eval_set` exposes them, and the runner's
`--tier` choices import them — so the CLI and the loader cannot disagree about
which tiers exist. `parse_eval_set` is the file-less half of `load_eval_set`
the runner uses to read each recipe's YAML exactly once.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from evaluators.eval_set import DEFAULT_TIER, TIERS, load_eval_set, parse_eval_set
from evaluators.runner import _parse_args

REPO_ROOT = Path(__file__).resolve().parents[3]

_RECIPE = """\
id: EVAL-MEMORY-001
title: Tier contract
tier: stable
setup:
  persona: ember-owl
  user: alice
interactions:
  - id: i1
    turns:
      - user: "Hi Ember — I'm Alice."
      - assistant: {match: contains, value: "Alice"}
"""


def test_tiers_are_read_from_the_schema_enum() -> None:
    schema = REPO_ROOT / "schemas" / "eval_set.schema.json"
    spec = json.loads(schema.read_text(encoding="utf-8"))["properties"]["tier"]
    assert TIERS == tuple(spec["enum"])
    assert DEFAULT_TIER == spec["default"] == "experimental"
    assert "stable" in TIERS


def test_runner_tier_choices_are_the_schema_tiers() -> None:
    for tier in TIERS:
        assert _parse_args(["--tier", tier]).tier == tier
    with pytest.raises(SystemExit) as exc:
        _parse_args(["--tier", "golden"])
    assert exc.value.code == 2


def test_parse_eval_set_is_load_eval_set_without_the_file(tmp_path: Path) -> None:
    path = tmp_path / "EVAL-MEMORY-001.yaml"
    path.write_text(_RECIPE, encoding="utf-8")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert parse_eval_set(data) == load_eval_set(path)
    assert parse_eval_set(data).tier == "stable"


def test_parse_eval_set_rejects_a_non_mapping() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        parse_eval_set(["not", "a", "mapping"])
