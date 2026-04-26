"""Tests for ``agents.prompt_loader.load_dimension_descriptions``.

The loader parses, structurally validates, and caches the persona
behavior-dimensions YAML at
``prompts/runtime/persona/sections/behavior-dimensions.yaml``.  The
last test class is a bytes-identical regression guard pinning every
shipped description to what was previously inlined in
``agents/persona_behavior.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.prompt_loader import (
    PromptLoadError,
    _read_dimension_descriptions,
    load_dimension_descriptions,
)


@pytest.fixture()
def dimensions_repo_root(tmp_path: Path) -> Path:
    """Repo-root layout for ``load_dimension_descriptions`` tests.

    Each test gets its own ``tmp_path``, so the lru_cache on
    ``_read_dimension_descriptions`` (keyed by resolved path) cannot
    leak between tests.
    """
    (tmp_path / "prompts" / "runtime" / "persona" / "sections").mkdir(parents=True)
    return tmp_path


def _write_dimensions(repo_root: Path, body: str) -> None:
    (
        repo_root
        / "prompts"
        / "runtime"
        / "persona"
        / "sections"
        / "behavior-dimensions.yaml"
    ).write_text(body, encoding="utf-8")


class TestLoadDimensionDescriptionsSuccess:
    def test_returns_parsed_structure(self, dimensions_repo_root: Path) -> None:
        _write_dimensions(
            dimensions_repo_root,
            "directness:\n"
            "  indirect: \"soft\"\n"
            "  direct: \"hard\"\n",
        )
        result = load_dimension_descriptions(repo_root=dimensions_repo_root)
        assert result == {"directness": {"indirect": "soft", "direct": "hard"}}

    def test_caches_repeated_reads(self, dimensions_repo_root: Path) -> None:
        # Pin the cache contract: a second read after deleting the file
        # still succeeds because the value is cached.  This matches the
        # semantics of ``load_snippet`` and lets direct callers re-invoke
        # cheaply without re-parsing the YAML.
        _write_dimensions(
            dimensions_repo_root,
            "directness:\n  indirect: \"soft\"\n",
        )
        first = load_dimension_descriptions(repo_root=dimensions_repo_root)
        (
            dimensions_repo_root
            / "prompts"
            / "runtime"
            / "persona"
            / "sections"
            / "behavior-dimensions.yaml"
        ).unlink()
        second = load_dimension_descriptions(repo_root=dimensions_repo_root)
        assert first == second == {"directness": {"indirect": "soft"}}


class TestLoadDimensionDescriptionsErrors:
    def test_missing_file_raises_with_clear_error(
        self, dimensions_repo_root: Path
    ) -> None:
        with pytest.raises(PromptLoadError, match="not found"):
            load_dimension_descriptions(repo_root=dimensions_repo_root)

    def test_invalid_yaml_raises(self, dimensions_repo_root: Path) -> None:
        # Unbalanced quote — yaml.safe_load raises YAMLError.
        _write_dimensions(dimensions_repo_root, "directness: \"unterminated\n")
        with pytest.raises(PromptLoadError, match="not valid YAML"):
            load_dimension_descriptions(repo_root=dimensions_repo_root)

    def test_top_level_list_rejected(self, dimensions_repo_root: Path) -> None:
        _write_dimensions(dimensions_repo_root, "- directness\n- detail_focus\n")
        with pytest.raises(PromptLoadError, match="must be a mapping"):
            load_dimension_descriptions(repo_root=dimensions_repo_root)

    def test_inner_value_not_dict_rejected(
        self, dimensions_repo_root: Path
    ) -> None:
        _write_dimensions(dimensions_repo_root, "directness: not a dict\n")
        with pytest.raises(PromptLoadError, match="must map to a"):
            load_dimension_descriptions(repo_root=dimensions_repo_root)

    def test_inner_leaf_not_string_rejected(
        self, dimensions_repo_root: Path
    ) -> None:
        _write_dimensions(
            dimensions_repo_root,
            "directness:\n  indirect: 42\n",
        )
        with pytest.raises(PromptLoadError, match="must be a non-empty string"):
            load_dimension_descriptions(repo_root=dimensions_repo_root)

    def test_inner_leaf_empty_string_rejected(
        self, dimensions_repo_root: Path
    ) -> None:
        # An empty (or whitespace-only) description would render as a bare
        # "- " bullet in render_behavior; reject it at load time so a
        # truncated YAML edit cannot silently produce a degraded prompt.
        _write_dimensions(
            dimensions_repo_root,
            'directness:\n  indirect: ""\n',
        )
        with pytest.raises(PromptLoadError, match="must be a non-empty string"):
            load_dimension_descriptions(repo_root=dimensions_repo_root)

    def test_inner_leaf_whitespace_only_rejected(
        self, dimensions_repo_root: Path
    ) -> None:
        _write_dimensions(
            dimensions_repo_root,
            'directness:\n  indirect: "   "\n',
        )
        with pytest.raises(PromptLoadError, match="must be a non-empty string"):
            load_dimension_descriptions(repo_root=dimensions_repo_root)


class TestShippedDimensionDescriptionsByteIdentity:
    """Regression guard: shipped behavior-dimension YAML must match what
    was previously inlined as ``DIMENSION_DESCRIPTIONS``.

    PR B of the prompt-externalization plan moved the dimension
    descriptions out of ``agents/persona_behavior.py`` and into
    ``prompts/runtime/persona/sections/behavior-dimensions.yaml``. These
    assertions pin every dimension/value/description triple to the bytes
    the runtime saw before the move so an accidental edit to the YAML
    is caught by CI rather than by an LLM behavior shift.
    """

    PROD_REPO_ROOT = Path(__file__).resolve().parents[3]

    EXPECTED: dict[str, dict[str, str]] = {
        "directness": {
            "indirect": (
                "Diplomatic and tactful. Softens criticism, asks questions"
                " instead of stating objections directly."
            ),
            "balanced": (
                "Balances directness with tact. States positions clearly"
                " but frames feedback constructively."
            ),
            "direct": (
                "Says exactly what they think."
                " Doesn't sugarcoat feedback or hedge opinions."
            ),
        },
        "detail_focus": {
            "big-picture": (
                "Focuses on high-level patterns and architecture."
                " Skips minutiae to keep discussions strategic."
            ),
            "balanced": (
                "Addresses both high-level concerns and"
                " specific details as needed."
            ),
            "detail-focused": (
                "Thorough and meticulous. Flags edge cases,"
                " checks specifics, prefers exhaustive analysis."
            ),
        },
        "formality": {
            "casual": (
                "Informal and approachable. Uses humor,"
                " contractions, and conversational language."
            ),
            "professional": (
                "Clear and structured. Uses professional"
                " language without being stiff."
            ),
            "formal": (
                "Precise and formal. Uses structured reports,"
                " proper titles, and measured language."
            ),
        },
        "risk_tolerance": {
            "cautious": (
                "Wants thorough analysis before decisions."
                " Asks for more data. Flags risks others might overlook."
            ),
            "moderate": (
                "Balances speed with diligence."
                " Comfortable with reasonable assumptions."
            ),
            "bold": (
                "Willing to make calls with incomplete information"
                " and course-correct. Bias toward action."
            ),
        },
        "expressiveness": {
            "reserved": (
                "Keeps emotions out of professional communication."
                " Focuses on facts and logic."
            ),
            "moderate": (
                "Acknowledges emotions when relevant"
                " but keeps focus on substance."
            ),
            "expressive": (
                "Openly shares reactions and feelings. Communication"
                " is warm, enthusiastic, or frustrated as the"
                " situation warrants."
            ),
        },
    }

    def test_full_round_trip(self) -> None:
        # Pin every dimension/value/description triple in one shot so a
        # diff against the YAML is the diff against this expectation.
        loaded = load_dimension_descriptions(repo_root=self.PROD_REPO_ROOT)
        assert loaded == self.EXPECTED

    def test_default_repo_root_resolves_production_yaml(self) -> None:
        # Independent of any explicit ``repo_root`` argument, the default
        # anchor (``Path(__file__).parent.parent`` from prompt_loader)
        # must locate the shipped YAML.  Catches a regression that
        # would silently fall back to a different anchor (e.g. cwd).
        # Clear the cache first so we exercise the file read, not a
        # cached result keyed by an explicit root.
        _read_dimension_descriptions.cache_clear()
        assert load_dimension_descriptions() == self.EXPECTED

    def test_persona_behavior_module_attr_matches_yaml(self) -> None:
        # ``agents.persona_behavior.DIMENSION_DESCRIPTIONS`` is the public
        # surface other code imports.  Pin it to the YAML contents so a
        # future regression (e.g. someone re-introducing an inline
        # constant) is caught here.
        from agents.persona_behavior import DIMENSION_DESCRIPTIONS

        assert DIMENSION_DESCRIPTIONS == self.EXPECTED
