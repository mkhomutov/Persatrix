"""RFC 0052 §B — the convene forced-turn marker's cross-language drift pin.

Per-topic split of ``test_cross_language_interaction_wire_drift.py`` (the
file-size-cap routing this family already uses); the shared parse helpers
are imported from there. This is the pin the marker shipped WITHOUT
(PR #718 review: the second forced-turn marker copied chair_escalation's
four wire sites but not its drift pin, so a fourth marker copying convene's
precedent would have inherited the same blind spot).
"""

from __future__ import annotations

import re
from pathlib import Path

from .test_cross_language_interaction_wire_drift import (
    _gate_forced_turn_registry_pin,
    _parse_miss,
)

_TASK_PROTO = Path("proto/task.proto")
# The in-process→wire translation split out of grpc_dispatcher.go at the
# 500-line cap (PR #718 review); the envelope lift the convene pin needs
# lives in the proto-translation half, like the sibling markers'.
_GRPC_DISPATCHER_GO = Path("internal/channels/grpc_dispatcher_proto.go")
_CHANNEL_WIRE_METADATA_PY = Path("agents/channel_wire_metadata.py")
_PROMPT_ASSEMBLY_PY = Path("agents/persona_runtime/prompt_assembly.py")


def test_convene_marker_agrees() -> None:
    """The RFC 0052 §B convene forced-turn marker MUST agree across the
    proto field, the Go dispatcher lift, the Python payload lift, and both
    strict consumers — the sibling markers' one-sided-rename guard. A
    drifted ``convene`` leaves the orchestrator convening into a marker
    nobody reads: the convener's gate runs the ordinary bid on the opening
    directive, the prompt loses the §B framing, and every autonomous
    channel stalls unopened with both suites green.
    """
    proto_src = _TASK_PROTO.read_text(encoding="utf-8")
    if not re.search(r"^\s*bool convene = 27;", proto_src, re.MULTILINE):
        _parse_miss("`bool convene = 27;`", _TASK_PROTO)

    go_src = _GRPC_DISPATCHER_GO.read_text(encoding="utf-8")
    if not re.search(r"Convene:\s+env\.Convene", go_src):
        _parse_miss(
            "the dispatcher lift `Convene: env.Convene`", _GRPC_DISPATCHER_GO,
        )

    lift_src = _CHANNEL_WIRE_METADATA_PY.read_text(encoding="utf-8")
    if 'payload["convene"] = True' not in lift_src:
        _parse_miss(
            'the conditional payload lift `payload["convene"] = True`',
            _CHANNEL_WIRE_METADATA_PY,
        )

    pa_src = _PROMPT_ASSEMBLY_PY.read_text(encoding="utf-8")
    if 'payload.get("convene") is True' not in pa_src:
        _parse_miss(
            'a strict `…get("convene") is True` read', _PROMPT_ASSEMBLY_PY,
        )
    _gate_forced_turn_registry_pin("convene")
