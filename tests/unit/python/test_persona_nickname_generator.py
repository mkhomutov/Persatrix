"""Tests for scripts.persona_nickname_generator."""

from __future__ import annotations

import re

import pytest

from scripts.persona_nickname_generator import (
    ADJECTIVES,
    NOUNS,
    PersonaNickname,
    _format_ids,
    _format_yaml,
    _to_display_name,
    generate_nicknames,
)


_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")


class TestToDisplayName:
    def test_converts_hyphenated_id_to_title_words(self) -> None:
        assert _to_display_name("ember-owl") == "Ember Owl"


class TestGenerateNicknames:
    def test_seed_produces_deterministic_output(self) -> None:
        first = generate_nicknames(count=5, seed=42)
        second = generate_nicknames(count=5, seed=42)

        assert [x.persona_id for x in first] == [x.persona_id for x in second]
        assert [x.display_name for x in first] == [x.display_name for x in second]

    def test_returns_unique_ids(self) -> None:
        entries = generate_nicknames(count=50, seed=7)
        ids = [x.persona_id for x in entries]

        assert len(ids) == len(set(ids))

    def test_ids_match_expected_pattern(self) -> None:
        entries = generate_nicknames(count=25, seed=99)

        assert all(_ID_PATTERN.match(item.persona_id) for item in entries)

    def test_display_name_is_title_case_id_projection(self) -> None:
        entries = generate_nicknames(count=10, seed=21)

        assert all(item.display_name == _to_display_name(item.persona_id) for item in entries)

    def test_rejects_non_positive_count(self) -> None:
        with pytest.raises(ValueError, match=r"count must be >= 1"):
            generate_nicknames(count=0)

    def test_rejects_count_over_available_combinations(self) -> None:
        max_count = len(ADJECTIVES) * len(NOUNS)

        with pytest.raises(ValueError, match=r"count exceeds available unique nicknames"):
            generate_nicknames(count=max_count + 1)


class TestFormatting:
    def test_format_ids_emits_one_id_per_line(self) -> None:
        entries = [
            PersonaNickname(persona_id="ember-owl", display_name="Ember Owl"),
            PersonaNickname(persona_id="orbit-kite", display_name="Orbit Kite"),
        ]

        assert _format_ids(entries) == "ember-owl\norbit-kite"

    def test_format_yaml_matches_expected_shape(self) -> None:
        entries = [PersonaNickname(persona_id="ember-owl", display_name="Ember Owl")]
        expected = '\n'.join([
            '- id: "ember-owl"',
            '  type: "persona"',
            '  name: "Ember Owl"',
        ])

        assert _format_yaml(entries) == expected
