"""The persona prompt's sections on their own: ``render_persona_sections()``.

EXP-001's arm A shows the advisers' identities through this function, so it
reads the words the persona agents read. The sections are checked against
the section composer's golden prompt, in test_persona_section_composer.py.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from agents.persona_runtime.prompt_assembly import render_persona_sections
from agents.persona_types import PersonaState

from ._persona_test_helpers import _PERSONA_CONFIG
from .test_persona_section_composer import _GOLDEN_FULL_PERSONA_PROMPT


class TestRenderPersonaSections:
    """The sections alone: EXP-001's arm A shows the advisers' identities
    through this function, so it reads the words the persona agents read."""

    def _render(self, only: Iterable[str] | None = None) -> list[str]:
        cfg = _PERSONA_CONFIG
        return render_persona_sections(
            cfg["persona"], PersonaState(), cfg["name"], cfg["role"], only=only,
        )

    def test_they_open_the_system_prompt(self) -> None:
        opening = "\n\n".join(self._render()) + "\n\nCurrent time:"
        assert _GOLDEN_FULL_PERSONA_PROMPT.startswith(opening)

    def test_only_keeps_the_named_sections_in_prompt_order(self) -> None:
        assert self._render(only=("goals", "identity")) == [
            _GOLDEN_FULL_PERSONA_PROMPT.split("\n\n")[0],
            "Goals:\n- Primary: Ship v2.0 on time\n- Secondary: Reduce tech debt by 20%\n"
            "- Hidden motivation: Prove the team can self-organize",
        ]

    def test_a_name_that_is_no_section_is_refused(self) -> None:
        with pytest.raises(ValueError, match="'goal'"):
            self._render(only=("identity", "goal"))

    def test_only_is_read_once_so_a_generator_works(self) -> None:
        names = (name for name in ("goals", "identity"))
        assert self._render(only=names) == self._render(only=("goals", "identity"))

    def test_a_str_names_one_section_and_is_never_split(self) -> None:
        assert self._render(only="identity") == [_GOLDEN_FULL_PERSONA_PROMPT.split("\n\n")[0]]
        with pytest.raises(ValueError, match="named ''"):
            self._render(only="")
