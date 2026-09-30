"""Shared pieces of the EXP-001 judge's tests: the practice series, the
rubric's prompts, a judge's answer and one line of the judge's call log.

Extracted so ``test_exp001_judge.py`` (the prompts, reading an answer, the
log) and ``test_exp001_judge_batch.py`` (judging a batch) share them without
either file passing the 500-line review cap. The pattern mirrors
``_persona_test_helpers.py``: a private module beside the test files,
imported by name.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evaluators.exp001.judge import JUDGE_MODEL, load_prompts
from evaluators.exp001.materials import Series, load_materials

__all__ = [
    "CONTROL",
    "MATERIALS",
    "PLAN",
    "PRACTICE",
    "PROMPTS",
    "RECALL",
    "RUBRIC",
    "_RECALL_JSON",
    "_judge_line",
    "_memo_json",
]

MATERIALS = Path(__file__).resolve().parents[3] / "evaluators" / "experiments" / "EXP-001"
RUBRIC = MATERIALS / "rubric.yaml"
PROMPTS = load_prompts(RUBRIC)
PRACTICE: Series = load_materials(MATERIALS).practice
PLAN, CONTROL, RECALL = PRACTICE.meetings[1], PRACTICE.meetings[2], PRACTICE.meetings[3]


def _memo_json(c2: int | None = 2, **overrides: Any) -> str:
    reply: dict[str, Any] = {
        "C1": 1, "C1_problems_found": ["K1"], "C2": c2, "C2_facts_used": ["F1", "F2"],
        "C3": 2, "C4": 1, "C5": 2,
        "reasons": {c: "Because." for c in ("C1", "C2", "C3", "C4", "C5")},
    }
    reply.update(overrides)
    return json.dumps(reply)


_RECALL_JSON = json.dumps({"R1": "right", "R2": "right", "R3": "wrong"})


def _judge_line(**overrides: Any) -> dict[str, Any]:
    """One judge call as the runtime logs it; *overrides* replace its fields."""
    line = {
        "tags": {"rater": "judge", "batch": "scored", "packet": "ab12cd34", "kind": "plan",
                 "try": "1"},
        "agent_id": "", "purpose": "judge", "provider": "anthropic", "model": JUDGE_MODEL,
        "model_alias": None, "started_at": "2036-10-06T10:00:00+00:00",
        "input_tokens": 10, "output_tokens": 2, "cache_write_tokens": 0,
        "cache_read_tokens": 0, "cache_prefix_sha256": None, "error": None,
    }
    line.update(overrides)
    return line
