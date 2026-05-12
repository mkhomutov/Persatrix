"""Guard that ``make reset``'s user-facing text names every wiped volume.

``make reset`` runs ``docker compose down -v``, which removes every named
volume declared at the top level of ``docker-compose.yaml`` — not just
the ones the docs happen to mention. PR #324 deep review (SF-1) caught
that the help comment and the post-run echo named only the channels DB
and persona memory, omitting the ``workspace`` volume that is shared by
the orchestrator and every agent container. An operator with scratch
files under ``/workspace`` would lose them silently.

This test parses ``docker-compose.yaml`` for the authoritative volume
list and the ``reset:`` target block in the ``Makefile`` for the lines
the operator actually sees, then asserts every declared volume name
appears in at least one of those operator-visible strings. If a future
contributor adds a fourth named volume to compose and forgets to update
the help/echo, this test fails before the docs drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _named_volumes_from_compose() -> list[str]:
    """Return the names of every top-level volume declared in compose."""
    compose_path = REPO_ROOT / "docker-compose.yaml"
    with compose_path.open(encoding="utf-8") as fh:
        compose = yaml.safe_load(fh)
    volumes = compose.get("volumes") or {}
    return sorted(volumes.keys())


def _reset_target_operator_text() -> str:
    """Return the operator-visible portion of the ``reset`` Makefile target.

    Concatenates the ``## ``-prefixed help comment on the target line with
    every ``@echo`` argument inside the recipe body. ``@#`` comment lines
    (which Make never prints) are excluded — they document maintainer
    rationale, not what the operator sees when running the target.
    """
    makefile_path = REPO_ROOT / "Makefile"
    text = makefile_path.read_text(encoding="utf-8")

    target_match = re.search(
        r"^reset:[^\n]*?##\s*(?P<help>[^\n]+)\n(?P<body>(?:\t[^\n]*\n)+)",
        text,
        flags=re.MULTILINE,
    )
    assert target_match is not None, "Could not locate ``reset:`` target in Makefile"

    help_text = target_match.group("help")
    body = target_match.group("body")

    echo_strings: list[str] = []
    for line in body.splitlines():
        echo_match = re.match(r'\t@echo\s+"(?P<msg>[^"]*)"\s*$', line)
        if echo_match:
            echo_strings.append(echo_match.group("msg"))

    return "\n".join([help_text, *echo_strings])


class TestMakeResetDocumentsAllVolumes:
    @pytest.mark.parametrize("volume", _named_volumes_from_compose())
    def test_volume_named_in_operator_text(self, volume: str) -> None:
        operator_text = _reset_target_operator_text()
        assert volume in operator_text, (
            f"docker-compose.yaml declares the named volume {volume!r}, but "
            f"the ``make reset`` help comment and @echo lines do not "
            f"mention it. ``docker compose down -v`` wipes every declared "
            f"volume, so the operator-visible text must enumerate them "
            f"all. Current operator-visible text:\n\n{operator_text}"
        )
