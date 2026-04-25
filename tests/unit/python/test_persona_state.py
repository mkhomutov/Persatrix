"""Tests for PersonaState, Mood enum, behavioral dimension rendering, and module imports."""

import pytest

from agents.persona_behavior import DIMENSION_DESCRIPTIONS, render_behavior
from agents.persona_types import Mood, PersonaState


# ─── Module Import Smoke Tests ──────────────────────────────
# Verify each extracted module is independently importable without
# circular-import errors.  All other tests go through persona.py
# re-exports, which would mask import-order issues.  (F-64-04)


class TestModuleImports:
    def test_persona_types_importable(self):
        from agents import persona_types

        assert hasattr(persona_types, "EventType")

    def test_persona_behavior_importable(self):
        from agents import persona_behavior

        assert hasattr(persona_behavior, "render_behavior")

    def test_dispatch_importable(self):
        from agents import dispatch

        assert hasattr(dispatch, "EventDispatcher")

    def test_tick_importable(self):
        from agents import tick

        assert hasattr(tick, "TickScheduler")

    def test_persona_runtime_importable(self):
        from agents import persona_runtime

        assert hasattr(persona_runtime, "_LLMPersonaAgent")

    def test_action_loop_submodule_importable(self):
        """Each persona_runtime submodule is independently importable.

        Guards against packaging errors (e.g. bad relative import in one
        submodule) that would be masked by the __init__.py import order.
        (PR #95 review: submodule-level import smoke tests.)
        """
        from agents.persona_runtime import action_loop

        assert hasattr(action_loop, "_ActionLoopMixin")

    def test_memory_context_submodule_importable(self):
        """See test_action_loop_submodule_importable."""
        from agents.persona_runtime import memory_context

        assert hasattr(memory_context, "_MemoryContextMixin")

    def test_state_persistence_submodule_importable(self):
        """See test_action_loop_submodule_importable."""
        from agents.persona_runtime import state_persistence

        assert hasattr(state_persistence, "_StatePersistenceMixin")

    def test_sub_agent_status_reexported(self):
        """SubAgentStatus must remain importable from persona.py (F-64-01)."""
        from agents.persona import SubAgentStatus

        assert hasattr(SubAgentStatus, "COMPLETED")

    def test_persona_runtime_symbols_reexported(self):
        """Private runtime symbols remain importable from persona.py via late import."""
        from agents.persona import (  # noqa: F401
            _LLMPersonaAgent,
            _coerce_event_timeout,
            _truncate_with_ellipsis,
        )

        assert _LLMPersonaAgent is not None
        assert callable(_coerce_event_timeout)
        assert callable(_truncate_with_ellipsis)

    def test_reexports_backward_compat(self):
        """Key symbols remain importable from persona.py via re-exports.

        Functional tests now import from the specific submodules directly.
        This test guards the re-export layer for external consumers that
        still use ``from agents.persona import X``.
        (PR #64 review: keep one test verifying re-export path.)
        """
        from agents.persona import (  # noqa: F401
            ActionExecutor,
            ActionType,
            AgentAction,
            AgentEvent,
            DIMENSION_DESCRIPTIONS,
            EventDispatcher,
            EventType,
            Mood,
            PersonaState,
            TickScheduler,
            render_behavior,
        )

        # Spot-check a symbol from each extracted module
        assert ActionExecutor is not None  # dispatch
        assert TickScheduler is not None  # tick
        assert PersonaState is not None  # persona_types
        assert render_behavior is not None  # persona_behavior

    def test_reexports_exhaustive(self):
        """All public symbols from new submodules are re-exported from persona.py.

        Programmatically verifies that every symbol in each submodule's
        ``__all__`` is importable from ``agents.persona``. Catches
        accidental omissions when symbols are added to a submodule but
        not to the persona.py re-export block.
        (PR #64 review F-64-DR-16: re-export maintenance burden.)
        """
        import agents.persona as persona
        from agents import dispatch, persona_behavior, persona_types, tick

        missing: list[str] = []
        for module in (persona_types, persona_behavior, dispatch, tick):
            for name in getattr(module, "__all__", []):
                if not hasattr(persona, name):
                    missing.append(f"{module.__name__}.{name}")

        assert not missing, (
            f"Symbols not re-exported from agents.persona: {missing}"
        )

    def test_persona_all_includes_submodule_symbols(self):
        """persona.py __all__ includes all submodule __all__ symbols.

        Verifies that persona.py's __all__ is a superset of all four
        extracted submodule __all__ lists.  Without this, ``from
        agents.persona import *`` would silently drop symbols that are
        available via explicit import.
        (F-64-DR5-06: persona.py had no __all__ — now verified.)
        """
        import agents.persona as persona
        from agents import dispatch, persona_behavior, persona_types, tick

        persona_all = set(getattr(persona, "__all__", []))
        missing: list[str] = []
        for module in (persona_types, persona_behavior, dispatch, tick):
            for name in getattr(module, "__all__", []):
                if name not in persona_all:
                    missing.append(f"{module.__name__}.{name}")

        assert not missing, (
            f"Symbols in submodule __all__ but not in persona.__all__: {missing}"
        )

    def test_circular_import_isolation(self):
        """Each extracted module imports without persona.py loaded first.

        Runs a subprocess that imports each new module in isolation,
        verifying no circular import errors.  In-process import tests
        (test_*_importable above) cannot detect this because persona.py
        is already loaded at the module level of this test file.
        (F-64-DR5-08: no explicit circular import test in isolation.)
        """
        import subprocess
        import sys

        script = (
            "import importlib; "
            "[importlib.import_module(m) for m in "
            "('agents.persona_types', 'agents.persona_behavior', "
            "'agents.dispatch', 'agents.tick', 'agents.persona_runtime', "
            "'agents.persona_runtime.action_loop', "
            "'agents.persona_runtime.memory_context', "
            "'agents.persona_runtime.state_persistence')]"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Circular import detected in isolated subprocess:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


# ─── Mood Enum Tests ────────────────────────────────────────


class TestMood:
    def test_all_six_values(self):
        assert len(Mood) == 6
        expected = {"neutral", "focused", "frustrated", "energized", "uncertain", "satisfied"}
        assert {m.value for m in Mood} == expected

    def test_serialize_deserialize(self):
        for mood in Mood:
            assert Mood(mood.value) is mood


# ─── PersonaState Tests ────────────────────────────────────


class TestPersonaState:
    def test_defaults(self):
        state = PersonaState()
        assert state.mood is Mood.NEUTRAL
        assert state.stress_level == 0.0
        assert state.energy == 1.0
        assert state.recent_context == []
        assert state.goal_progress == {}

    def test_to_prompt_section_default(self):
        state = PersonaState()
        section = state.to_prompt_section()
        assert "Current mood: neutral" in section
        # stress and energy not shown at default values
        assert "Stress" not in section
        assert "Energy" not in section

    def test_to_prompt_section_stress_above_threshold(self):
        state = PersonaState(stress_level=0.5)
        section = state.to_prompt_section()
        assert "Stress level: 0.5/1.0" in section

    def test_to_prompt_section_stress_below_threshold(self):
        state = PersonaState(stress_level=0.2)
        section = state.to_prompt_section()
        assert "Stress" not in section

    def test_to_prompt_section_low_energy(self):
        state = PersonaState(energy=0.3)
        section = state.to_prompt_section()
        assert "Energy level: 0.3/1.0" in section
        assert "conserve effort" in section

    def test_to_prompt_section_normal_energy(self):
        state = PersonaState(energy=0.7)
        section = state.to_prompt_section()
        assert "Energy" not in section

    def test_to_prompt_section_recent_context(self):
        state = PersonaState(recent_context=["discussed roadmap", "reviewed PR"])
        section = state.to_prompt_section()
        assert "Recent context:" in section
        assert "discussed roadmap" in section
        assert "reviewed PR" in section

    def test_to_prompt_section_recent_context_limited_to_5(self):
        state = PersonaState(recent_context=[f"item-{i}" for i in range(10)])
        section = state.to_prompt_section()
        # Should only show last 5
        assert "item-5" in section
        assert "item-9" in section
        assert "item-4" not in section

    def test_to_prompt_section_goal_progress(self):
        state = PersonaState(goal_progress={"Ship v2": 0.75})
        section = state.to_prompt_section()
        assert "Goal progress:" in section
        assert "Ship v2: 75%" in section

    def test_drain_energy(self):
        state = PersonaState(energy=1.0)
        state.drain_energy()
        assert state.energy == pytest.approx(0.95)

    def test_drain_energy_clamps_to_zero(self):
        state = PersonaState(energy=0.02)
        state.drain_energy()
        assert state.energy == 0.0

    def test_recover_energy(self):
        state = PersonaState(energy=0.5)
        state.recover_energy()
        assert state.energy == pytest.approx(0.6)

    def test_recover_energy_clamps_to_one(self):
        state = PersonaState(energy=0.95)
        state.recover_energy()
        assert state.energy == 1.0

    def test_to_dict(self):
        state = PersonaState(
            mood=Mood.FOCUSED,
            stress_level=0.4,
            energy=0.8,
            recent_context=["test"],  # NOT persisted
            goal_progress={"goal": 0.5},
        )
        d = state.to_dict()
        assert d == {
            "mood": "focused",
            "stress_level": 0.4,
            "energy": 0.8,
            "goal_progress": {"goal": 0.5},
        }
        assert "recent_context" not in d

    def test_from_dict(self):
        data = {
            "mood": "frustrated",
            "stress_level": 0.6,
            "energy": 0.3,
            "goal_progress": {"task": 0.9},
        }
        state = PersonaState.from_dict(data)
        assert state.mood is Mood.FRUSTRATED
        assert state.stress_level == 0.6
        assert state.energy == 0.3
        assert state.goal_progress == {"task": 0.9}
        assert state.recent_context == []  # always empty

    def test_from_dict_unknown_mood_defaults(self):
        state = PersonaState.from_dict({"mood": "angry"})
        assert state.mood is Mood.NEUTRAL

    def test_from_dict_empty(self):
        state = PersonaState.from_dict({})
        assert state.mood is Mood.NEUTRAL
        assert state.energy == 1.0

    def test_round_trip_persistence(self):
        original = PersonaState(
            mood=Mood.ENERGIZED,
            stress_level=0.7,
            energy=0.4,
            goal_progress={"ship": 0.6},
        )
        restored = PersonaState.from_dict(original.to_dict())
        assert restored.mood is Mood.ENERGIZED
        assert restored.stress_level == original.stress_level
        assert restored.energy == original.energy
        assert restored.goal_progress == original.goal_progress


# ─── Behavioral Dimension Tests ────────────────────────────


class TestRenderBehavior:
    def test_all_dimensions_present(self):
        behavior = {
            "directness": "direct",
            "detail_focus": "big-picture",
            "formality": "professional",
            "risk_tolerance": "moderate",
            "expressiveness": "reserved",
        }
        rendered = render_behavior(behavior)
        assert "Says exactly what they think" in rendered
        assert "Focuses on high-level patterns" in rendered
        assert "Clear and structured" in rendered
        assert "Balances speed with diligence" in rendered
        assert "Keeps emotions out of professional" in rendered

    def test_defaults_applied_for_omitted_dimensions(self):
        # Empty behavior → all defaults applied
        rendered = render_behavior({})
        # Should have default descriptions for all 5 dimensions
        assert "Balances directness with tact" in rendered  # directness: balanced
        assert "Addresses both high-level" in rendered  # detail_focus: balanced
        assert "Clear and structured" in rendered  # formality: professional
        assert "Balances speed with diligence" in rendered  # risk_tolerance: moderate
        assert "Acknowledges emotions when relevant" in rendered  # expressiveness: moderate

    def test_partial_override(self):
        behavior = {"directness": "indirect"}
        rendered = render_behavior(behavior)
        assert "Diplomatic and tactful" in rendered
        # Other dimensions should use defaults
        assert "Balances speed with diligence" in rendered

    def test_unknown_dimension_ignored(self, caplog):
        behavior = {"unknown_dim": "unknown_val"}
        with caplog.at_level("WARNING", logger="agents.persona_behavior"):
            rendered = render_behavior(behavior)
        # Should still have defaults for known dimensions
        assert "Balances directness with tact" in rendered
        # PR #54 review: unknown dimensions now emit a warning
        assert "Unknown behavior dimension" in caplog.text
        assert "unknown_dim" in caplog.text

    def test_unknown_value_no_line(self):
        behavior = {"directness": "super-direct"}
        rendered = render_behavior(behavior)
        # The unknown value for directness should not produce a line
        # but other defaults should still be present
        assert "super-direct" not in rendered
        assert "Balances speed" in rendered

    def test_unknown_value_logs_warning(self, caplog):
        """Unknown values of a known dimension log a warning with valid values.

        Previously, unknown dimension values silently produced no output line.
        Operators had no way to know a typo was causing a missing behavioral
        description.  Now a WARNING is logged listing the valid values.
        (PR #64 review F-64-DR-05: unknown dimension values silently
        produce no output — no warning logged.)
        """
        behavior = {"directness": "super-direct"}
        with caplog.at_level("WARNING", logger="agents.persona_behavior"):
            render_behavior(behavior)
        assert any(
            "Unknown value" in r.message
            and "super-direct" in r.message
            and "directness" in r.message
            for r in caplog.records
        )

    def test_all_invalid_values_still_produces_defaults(self, caplog):
        """All user-provided values invalid — defaults still produce full output.

        When every dimension has an unrecognised value, the merged dict
        contains only invalid values for the user-specified keys, but
        defaults should fill in for any unspecified dimensions.  If all
        five dimensions are overridden with invalid values, the rendered
        output should be empty (no valid descriptions) and five warnings
        should be logged.
        (PR #64 review F-64-DR-22: render_behavior all-invalid-values edge case.)
        """
        behavior = {
            "directness": "xyz",
            "detail_focus": "abc",
            "formality": "nope",
            "risk_tolerance": "invalid",
            "expressiveness": "wrong",
        }
        with caplog.at_level("WARNING", logger="agents.persona_behavior"):
            rendered = render_behavior(behavior)
        # All five dimensions have invalid values — no description lines produced
        assert rendered == ""
        # A warning should be logged for each invalid value
        warning_records = [
            r for r in caplog.records
            if "Unknown value" in r.message
        ]
        assert len(warning_records) == 5

    @pytest.mark.parametrize("dimension,values", [
        ("directness", ["indirect", "balanced", "direct"]),
        ("detail_focus", ["big-picture", "balanced", "detail-focused"]),
        ("formality", ["casual", "professional", "formal"]),
        ("risk_tolerance", ["cautious", "moderate", "bold"]),
        ("expressiveness", ["reserved", "moderate", "expressive"]),
    ])
    def test_all_values_produce_descriptions(self, dimension: str, values: list[str]):
        for value in values:
            desc = DIMENSION_DESCRIPTIONS[dimension][value]
            assert isinstance(desc, str)
            assert len(desc) > 10  # non-trivial description
