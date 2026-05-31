"""ISSUE-0053 — ``_coerce_event_timeout`` lives in its own submodule.

``agents/persona_runtime/__init__.py`` sat at the 500-line review cap with
no headroom, so the next symbol added to it would trip
``scripts/checks/file_size.py --strict``. The fix extracts the
self-contained ``_coerce_event_timeout`` helper into a dedicated
``event_timeout`` submodule (mirroring the existing
``conversation_window`` / ``summarize_close`` extraction precedent) and
re-exports it from the package root so every existing importer is
byte-compatible.

These tests pin three things:

1. The helper now lives in ``agents.persona_runtime.event_timeout`` (the
   extraction actually happened — this fails before the move).
2. The package root **and** ``agents.persona`` still re-export the *same*
   object (the back-compat contract every caller depends on —
   ``agents/persona.py``, ``test_persona_agent_factory``,
   ``test_persona_state``).
3. ``agents/persona_runtime/__init__.py`` is back under the code cap with
   real headroom (the regression this issue exists to prevent).
"""

from __future__ import annotations

from pathlib import Path

from scripts.checks.file_size import DEFAULT_MAX_CODE_LINES

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INIT_PATH = _REPO_ROOT / "agents" / "persona_runtime" / "__init__.py"


class TestEventTimeoutSubmodule:
    """The helper has its own home and a stable re-export surface."""

    def test_helper_lives_in_dedicated_submodule(self) -> None:
        from agents.persona_runtime.event_timeout import _coerce_event_timeout

        assert callable(_coerce_event_timeout)

    def test_package_root_reexports_same_object(self) -> None:
        from agents.persona_runtime import _coerce_event_timeout as from_root
        from agents.persona_runtime.event_timeout import (
            _coerce_event_timeout as from_submodule,
        )

        assert from_root is from_submodule

    def test_persona_module_reexports_same_object(self) -> None:
        # agents/persona.py re-exports the helper for backward-compatible
        # imports; the late import must resolve to the extracted object.
        from agents.persona import _coerce_event_timeout as from_persona
        from agents.persona_runtime.event_timeout import (
            _coerce_event_timeout as from_submodule,
        )

        assert from_persona is from_submodule

    def test_helper_stays_in_package_all(self) -> None:
        import agents.persona_runtime as pkg

        assert "_coerce_event_timeout" in pkg.__all__

    def test_submodule_defines_all(self) -> None:
        # Every extracted persona_runtime submodule that re-exports a symbol
        # through the package root declares its own ``__all__`` (F-64-DR5-06;
        # mirrors the ``memory_context`` helper-extraction precedent), so the
        # re-export surface is explicit rather than incidental.
        import agents.persona_runtime.event_timeout as mod

        assert mod.__all__ == ["_coerce_event_timeout"]


class TestEventTimeoutBehaviourParity:
    """The move preserves coercion semantics exactly."""

    def test_passthrough_float(self) -> None:
        from agents.persona_runtime.event_timeout import _coerce_event_timeout

        assert _coerce_event_timeout(300.0, 100.0, "test") == 300.0

    def test_coerces_str_and_int(self) -> None:
        from agents.persona_runtime.event_timeout import _coerce_event_timeout

        assert _coerce_event_timeout("300", 100.0, "test") == 300.0
        assert _coerce_event_timeout(60, 100.0, "test") == 60.0

    def test_falls_back_on_garbage(self) -> None:
        from agents.persona_runtime.event_timeout import _coerce_event_timeout

        assert _coerce_event_timeout("not-a-number", 100.0, "test") == 100.0
        assert _coerce_event_timeout(None, 100.0, "test") == 100.0

    def test_min_value_floor_rejects_non_positive(self) -> None:
        from agents.persona_runtime.event_timeout import _coerce_event_timeout

        # PR-3 review #19: a value at/below the floor falls back to default.
        assert _coerce_event_timeout(
            0.0, 600.0, "test", min_value=0.0,
            setting_name="interaction_idle_timeout_sec",
        ) == 600.0
        assert _coerce_event_timeout(
            5.0, 600.0, "test", min_value=0.0,
        ) == 5.0


class TestInitHeadroomRestored:
    """ISSUE-0053: the extraction must leave real headroom under the cap."""

    def test_init_is_under_cap_with_margin(self) -> None:
        line_count = len(_INIT_PATH.read_text(encoding="utf-8").splitlines())
        # Not merely <= 500 (which the file already satisfied at the
        # ceiling) — the point of the extraction is genuine headroom so the
        # next contributor does not trip --strict on a one-line addition.
        assert line_count <= DEFAULT_MAX_CODE_LINES - 15, (
            f"persona_runtime/__init__.py is {line_count} lines; "
            f"expected <= {DEFAULT_MAX_CODE_LINES - 15} after the "
            f"event_timeout extraction (ISSUE-0053)"
        )
