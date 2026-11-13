"""``make validate`` checks the ``cost:`` block of optimization.yaml (ISSUE-0211).

A spending limit of zero means no limit, and the schema used to say
nothing about ``cost``.  So a misspelled key under it, or a misspelled
``cost`` itself, passed validation and loaded every cap as unlimited.

The schema now describes the block, closed at every level, and requires
it.  Each case below starts from the shipped ``config/optimization.yaml``,
changes one thing, and runs the validator ``make validate`` runs.  The
orchestrator's loader refuses the same keys
(``internal/cost/cost_config_strict_test.go`` holds the matching table).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from agents.validate import validate_config_dir

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMAS_DIR = REPO_ROOT / "schemas"
SHIPPED = REPO_ROOT / "config" / "optimization.yaml"

#: Every optimization.yaml the repository ships: the root config and one
#: per demo.  ``make validate`` runs over each of these folders.
SHIPPED_DIRS = [REPO_ROOT / "config", *sorted(
    p.parent for p in (REPO_ROOT / "config" / "demo").glob("*/optimization.yaml")
)]

Doc = dict[str, Any]


def _shipped() -> Doc:
    doc = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def _validate(tmp_path: Path, doc: Doc) -> tuple[bool, list[str]]:
    (tmp_path / "optimization.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    workflows = tmp_path / "workflows"  # empty: only this file is checked
    workflows.mkdir()
    ok, errors, checked = validate_config_dir(
        str(tmp_path), str(SCHEMAS_DIR), workflow_dir=str(workflows),
    )
    assert checked == 1
    return ok, [f"{e.path}: {e.message}" for e in errors]


def _rename(path: list[str], old: str, new: str) -> Callable[[Doc], None]:
    """Rename one key, as a typo would, in the mapping at *path*."""
    def change(doc: Doc) -> None:
        node: Any = doc
        for key in path:
            node = node[key]
        assert old in node, f"{old} is not under {path}: the case would change nothing"
        node[new] = node.pop(old)
    return change


def _set(path: list[str], key: str, value: Any) -> Callable[[Doc], None]:
    def change(doc: Doc) -> None:
        node: Any = doc
        for step in path:
            node = node[step]
        node[key] = value
    return change


def _drop(path: list[str], key: str) -> Callable[[Doc], None]:
    def change(doc: Doc) -> None:
        node: Any = doc
        for step in path:
            node = node[step]
        del node[key]
    return change


def _rename_a_price_key(doc: Doc) -> None:
    models = doc["cost"]["pricing"]["models"]
    first = next(iter(models.values()))
    first["input_per_1m_token"] = first.pop("input_per_1m_tokens")


BUDGETS = ["cost", "budgets"]

#: (what changed, the change, text the validator's message must hold)
REFUSED: list[tuple[str, Callable[[Doc], None], str]] = [
    ("budgets misspelled", _rename(["cost"], "budgets", "budgetz"), "budgetz"),
    ("cost misspelled", _rename([], "cost", "costs"), "'cost' is a required property"),
    ("no cost block", _drop([], "cost"), "'cost' is a required property"),
    ("no budgets under cost", _drop(["cost"], "budgets"), "'budgets' is a required property"),
    ("pricing misspelled", _rename(["cost"], "pricing", "pricings"), "pricings"),
    ("models misspelled", _rename(["cost", "pricing"], "models", "model"), "model"),
    ("a price key misspelled", _rename_a_price_key, "input_per_1m_token"),
    ("global misspelled", _rename(BUDGETS, "global", "globl"), "globl"),
    ("per_workflow misspelled", _rename(BUDGETS, "per_workflow", "per_workflows"), "per_workflows"),
    ("per_agent misspelled", _rename(BUDGETS, "per_agent", "per_agnet"), "per_agnet"),
    ("max_daily_usd misspelled",
     _rename([*BUDGETS, "global"], "max_daily_usd", "max_daily_usdd"), "max_daily_usdd"),
    ("a default_max_usd misspelled",
     _rename([*BUDGETS, "per_agent"], "default_max_usd", "default_max"), "default_max"),
    ("alert_at_percent misspelled",
     _rename([*BUDGETS, "global"], "alert_at_percent", "alert_at_percents"), "alert_at_percents"),
    ("on_exceed misspelled",
     _rename([*BUDGETS, "global"], "on_exceed", "on_exceeded"), "on_exceeded"),
    ("an extra key beside pricing and budgets", _set(["cost"], "enabled", True), "enabled"),
    ("a negative limit", _set([*BUDGETS, "global"], "max_daily_usd", -1), "-1"),
    ("a limit written as text", _set([*BUDGETS, "per_agent"], "default_max_usd", "5"), "'5'"),
    ("an on_exceed the orchestrator does not know",
     _set([*BUDGETS, "global"], "on_exceed", "shutdown"), "shutdown"),
    ("budgets with no value", _set(["cost"], "budgets", None), "None"),
]

ACCEPTED: list[tuple[str, Callable[[Doc], None]]] = [
    ("the shipped file", lambda doc: None),
    ("an empty budgets block", _set(["cost"], "budgets", {})),
    ("every limit at zero", lambda doc: doc["cost"].__setitem__("budgets", {
        "global": {"max_daily_usd": 0},
        "per_workflow": {"default_max_usd": 0},
        "per_agent": {"default_max_usd": 0},
    })),
    ("one limit left out", _drop(BUDGETS, "per_agent")),
    ("no pricing block", _drop(["cost"], "pricing")),
    ("a profile under a name of the operator's",
     _set([], "aggressive", {"caching": {"exact": {"enabled": False}}})),
]


@pytest.mark.parametrize(("change", "names"), [c[1:] for c in REFUSED], ids=[c[0] for c in REFUSED])
def test_a_wrong_cost_block_fails_validation(
    tmp_path: Path, change: Callable[[Doc], None], names: str,
) -> None:
    doc = _shipped()
    before = copy.deepcopy(doc)
    change(doc)
    assert doc != before, "the case changed nothing"

    ok, messages = _validate(tmp_path, doc)

    assert not ok
    assert any(names in m for m in messages), messages


@pytest.mark.parametrize("change", [c[1] for c in ACCEPTED], ids=[c[0] for c in ACCEPTED])
def test_a_right_cost_block_passes_validation(
    tmp_path: Path, change: Callable[[Doc], None],
) -> None:
    doc = _shipped()
    change(doc)

    ok, messages = _validate(tmp_path, doc)

    assert ok, messages


def test_every_shipped_config_is_listed() -> None:
    """Seven demos and the root config: a new one joins the list by itself."""
    assert len(SHIPPED_DIRS) == 8
    assert all((d / "optimization.yaml").is_file() for d in SHIPPED_DIRS)


@pytest.mark.parametrize("config_dir", SHIPPED_DIRS, ids=lambda d: d.name)
def test_every_shipped_config_passes_validation(config_dir: Path, tmp_path: Path) -> None:
    """The check ``make validate`` makes of each folder, on that folder's file."""
    doc = yaml.safe_load((config_dir / "optimization.yaml").read_text(encoding="utf-8"))

    ok, messages = _validate(tmp_path, doc)

    assert ok, messages
    budgets = doc["cost"]["budgets"]
    assert budgets["global"]["max_daily_usd"] > 0
    assert budgets["per_workflow"]["default_max_usd"] > 0
    assert budgets["per_agent"]["default_max_usd"] > 0


def test_the_cost_block_is_closed_at_every_level() -> None:
    """No level under ``cost`` takes a key the schema does not name.

    Reads the schema itself, so a level added later without
    ``additionalProperties: false`` fails here whether or not a sample
    document happens to reach it.
    """
    schema = json.loads((SCHEMAS_DIR / "optimization.schema.json").read_text(encoding="utf-8"))
    cost = schema["properties"]["cost"]
    open_levels: list[str] = []

    def walk(node: dict[str, Any], where: str) -> None:
        if node.get("type") != "object":
            return
        extra = node.get("additionalProperties")
        if isinstance(extra, dict):
            # A map keyed by a name the operator chooses (a model id): its
            # values are checked, and are objects walked in turn.
            walk(extra, f"{where}.*")
        elif extra is not False:
            open_levels.append(where)
        for name, child in node.get("properties", {}).items():
            walk(child, f"{where}.{name}")

    walk(cost, "cost")

    assert open_levels == []
    assert schema["required"] == ["cost"]
    assert cost["required"] == ["budgets"]
    # The top level stays open: it holds one block per profile, under
    # names the operator chooses.
    assert "additionalProperties" not in schema


def test_make_validate_covers_the_demo_configs() -> None:
    """The ``validate`` recipe runs the validator over each demo folder."""
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    recipe = makefile.split("\nvalidate:", 1)[1].split("\n\n", 1)[0]

    assert "agents/validate.py config/" in recipe
    assert "config/demo/*/" in recipe
