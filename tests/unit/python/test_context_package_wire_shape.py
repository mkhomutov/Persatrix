"""RFC 0008 PR 1 — Python agent-side regression test for `_context_package`.

This test fulfils the PR-plan integration requirement that an agent receiving
the orchestrator's `_context_package` JSON payload (Open Question 2 — additive,
no proto changes) parses it without errors and that agents *without*
packaging-awareness ignore the key. PR 2 (`MemoryFacade`) and PR 3 (delegation
contract) rely on this wire-shape contract; without a producer/consumer
round-trip, a future renaming of any JSON tag would only break at integration
time.

Coverage:

1. A representative Go-produced JSON payload conforming to the v1 wire shape
   parses with `json.loads` and round-trips its required fields.
2. Unknown / forward-compatible fields are tolerated (the v1 contract is
   add-only; v2 may introduce additional keys that v1 consumers must ignore).
3. Absence of `_context_package` from a `TaskInput.context` does not raise.
4. Presence of `_context_package` in `TaskInput.context` is preserved verbatim
   for downstream consumers (the agent neither mutates nor strips it).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents.base import TaskInput, TaskInputConfig

# Reserved key — must match `internal/scheduler/context_package.go::ContextPackageKey`.
CONTEXT_PACKAGE_KEY = "_context_package"

# RFC 0008 PR 6a — wire-shape contract follow-up. The fixture is produced by
# `internal/scheduler/context_package_wire_shape_fixture_test.go` (run
# `PERSATRIX_REGEN_FIXTURES=1 go test ./internal/scheduler -run
# TestContextPackage_WriteWireShapeFixture` to refresh after wire-shape
# edits). Reading the Go-produced JSON here pins the cross-language contract:
# a unilateral tag rename on either side fails this test before integration.
_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2] / "fixtures" / "context_package_v1.json"
)


# NOTE: the value type is `Any` (not `object`) so spread expressions like
# `**_GO_PRODUCED_SAMPLE["metrics"]` (see
# `test_context_package_tolerates_unknown_fields`) type-check under strict
# mypy. The runtime contract is unchanged — every value is JSON-decoded.
def _load_fixture() -> dict[str, Any]:
    """Load the Go-produced v1 fixture; fall back to a minimal in-tree sample
    if the fixture is absent (e.g. fresh clone before the Go test has run).
    The fall-back is structurally identical so the test still asserts the
    contract; CI runs the Go test first via `make test`.

    PR 227 review follow-up (Should Fix #3): the literal type of
    ``compression_ratio`` MUST mirror what Go's ``encoding/json`` actually
    emits for ``float64(1.0)`` — namely the integer-form ``1`` (no decimal
    point). Python's ``json.loads`` decodes that as ``int``, while a literal
    ``1.0`` here would decode as ``float``. Keeping the fall-back as ``1``
    (with this comment) prevents the fixture-loaded and fall-back-loaded
    paths from disagreeing under a future strict ``isinstance(..., float)``
    assertion. The two paths must yield identical Python types.
    """
    if _FIXTURE_PATH.is_file():
        return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    return {
        "version": 1,
        "pinned_sections": [],
        "step_outputs": [
            {"id": "out1", "content": "first step output", "tokens": 4},
        ],
        "metrics": {
            "tokens_before": 4,
            "tokens_after": 4,
            # See docstring: integer form mirrors Go's `encoding/json`
            # rendering of `float64(1.0)`. Numeric-equality assertions below
            # still pass via Python's `1 == 1.0` coercion.
            "compression_ratio": 1,
            "candidates_dropped": 0,
        },
        "budget_memory_tokens": 0,
    }


_GO_PRODUCED_SAMPLE: dict[str, Any] = _load_fixture()


def test_context_package_v1_payload_parses() -> None:
    """A Go-produced v1 payload round-trips through json.loads with required fields intact."""
    encoded = json.dumps(_GO_PRODUCED_SAMPLE)
    decoded = json.loads(encoded)

    assert decoded["version"] == 1, "v1 wire-shape contract — version key must be present and == 1"
    # budget_memory_tokens is reserved for PR 2; v1 producers always emit 0.
    assert decoded["budget_memory_tokens"] == 0
    assert isinstance(decoded["step_outputs"], list)
    assert decoded["step_outputs"][0]["id"] == "out1"
    assert isinstance(decoded["pinned_sections"], list)
    assert isinstance(decoded["metrics"], dict)
    assert decoded["metrics"]["compression_ratio"] == 1.0
    assert decoded["metrics"]["candidates_dropped"] == 0


def test_context_package_tolerates_unknown_fields() -> None:
    """Forward-compat: a v2 producer adding new fields must not break a v1 parser."""
    forward_compat = {
        **_GO_PRODUCED_SAMPLE,
        "experimental_score_breakdown": {"out1": {"density": 0.42}},  # hypothetical v2 addition
        "metrics": {
            **_GO_PRODUCED_SAMPLE["metrics"],
            "future_field": "ignored",
        },
    }
    encoded = json.dumps(forward_compat)
    decoded = json.loads(encoded)

    # Required v1 fields still readable.
    assert decoded["version"] == 1
    assert decoded["budget_memory_tokens"] == 0
    # Unknown fields are present but a v1 consumer simply does not look at them.
    assert "experimental_score_breakdown" in decoded
    assert decoded["metrics"]["future_field"] == "ignored"


def test_task_input_without_context_package_does_not_raise() -> None:
    """Absence of `_context_package` (legacy passthrough) is the v0.2 default and must be safe."""
    task = TaskInput(
        task_id="t1",
        workflow_id="wf1",
        payload="hello",
        context={"out1": "first step output"},
        config=TaskInputConfig(),
    )
    # A packaging-unaware consumer iterates context keys; the legacy key set is intact.
    assert CONTEXT_PACKAGE_KEY not in task.context
    assert task.context["out1"] == "first step output"


def test_task_input_with_context_package_preserves_payload() -> None:
    """A packaging-unaware agent must neither mutate nor strip the reserved key."""
    encoded = json.dumps(_GO_PRODUCED_SAMPLE)
    task = TaskInput(
        task_id="t2",
        workflow_id="wf1",
        payload="hello",
        context={
            "out1": "first step output",
            CONTEXT_PACKAGE_KEY: encoded,
        },
        config=TaskInputConfig(),
    )

    # The key is preserved verbatim — downstream packaging-aware consumers
    # (PR 2's MemoryFacade) will read it without re-parsing.
    assert task.context[CONTEXT_PACKAGE_KEY] == encoded
    redecoded = json.loads(task.context[CONTEXT_PACKAGE_KEY])
    assert redecoded["version"] == 1
    assert redecoded["step_outputs"][0]["id"] == "out1"
