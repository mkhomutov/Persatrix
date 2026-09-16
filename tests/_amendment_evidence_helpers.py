"""Shared scaffold for the External evidence check tests.

``test_amendment_evidence.py`` reads one page's amendments and their
evidence; ``test_amendment_evidence_scan.py`` runs the whole check over a
``docs/`` directory. Both lay amendments out the same way, so that layout
lives here once.

Importable as ``_amendment_evidence_helpers`` because ``tests/conftest.py``
puts ``tests/`` on ``sys.path``."""

from __future__ import annotations

FILLED = {
    "Installs by anyone other than the author": "none known",
    "Issues or pull requests from anyone else": "0",
    "Demos shown, to whom, when": "none recorded",
    "User conversations written up": "none",
    "Experiment results published": "none",
}


def _table(rows: dict[str, str]) -> str:
    body = "".join(f"| {label} | {value} |\n" for label, value in rows.items())
    return f"| Field | Since 2026-08-19 |\n|-------|------------------|\n{body}"


def _amendment(section: str, date: str = "2026-10-01") -> str:
    """One amendment laid out as docs/v0.3.x-sequencing.md lays them out."""
    return (
        f"## Amendment {date} — open the next thing\n"
        "\n"
        "Intro paragraph.\n"
        "\n"
        f"{section}"
        "\n"
        "### Why this changes\n"
        "\n"
        "1. Because.\n"
    )


def _evidence(rows: dict[str, str]) -> str:
    return f"### External evidence since the last amendment\n\nMeasured today.\n\n{_table(rows)}"
