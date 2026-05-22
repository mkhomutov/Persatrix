"""Unit tests for ``summarize_autonomy_cadence`` — RFC 0024 PR 2 / PR 5.1.

The cadence summary feeds the COST WARNING and "Started" INFO logs that
are the loudest operator-facing signal about a persona's autonomous
spend (see ``agents.server_persona.initialize_persona_agents``).  These
tests pin the rendering rule for the ``autonomy.timers`` path; the
heavyweight end-to-end log-capture tests live in
``test_server_persona_wiring_timers.py``.
"""

from __future__ import annotations

from agents.server_persona_timers import summarize_autonomy_cadence


class TestCadenceSummaryRendering:
    def test_legacy_tick_interval_path(self):
        """``timers=None`` (legacy ``tick_interval_seconds``) renders the
        scalar interval verbatim."""
        assert summarize_autonomy_cadence(None, 60) == "tick_interval=60s"

    def test_integer_interval_renders_without_decimal(self):
        assert (
            summarize_autonomy_cadence(
                [{"id": "consolidate", "interval_seconds": 60}], 0,
            )
            == "timers=[consolidate@60s]"
        )

    def test_integer_valued_float_normalises_to_integer(self):
        """PR 2 review (6): an ``interval_seconds: 60.0`` config is legal
        (the schema declares ``type: number``) but used to render as
        ``consolidate@60.0s`` while ``60`` rendered as ``consolidate@60s`` —
        a latent inconsistency in the COST log. ``{:g}`` normalises the
        integer-valued float so both spellings read identically."""
        assert (
            summarize_autonomy_cadence(
                [{"id": "consolidate", "interval_seconds": 60.0}], 0,
            )
            == "timers=[consolidate@60s]"
        )

    def test_fractional_interval_preserves_precision(self):
        """A genuinely fractional interval keeps its fractional part —
        ``{:g}`` normalises ``60.0 → 60`` without flattening ``60.5``."""
        assert (
            summarize_autonomy_cadence(
                [{"id": "consolidate", "interval_seconds": 60.5}], 0,
            )
            == "timers=[consolidate@60.5s]"
        )

    def test_multiple_timers_joined(self):
        assert (
            summarize_autonomy_cadence(
                [
                    {"id": "a", "interval_seconds": 30.0},
                    {"id": "b", "interval_seconds": 1.5},
                ],
                0,
            )
            == "timers=[a@30s, b@1.5s]"
        )
