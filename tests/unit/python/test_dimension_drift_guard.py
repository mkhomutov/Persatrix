"""Import-time guards on ``agents.persona_behavior``.

The module re-imports cleanly when the behavior-dimensions YAML and the
in-code defaults table agree, and raises ``RuntimeError`` at import
time when they drift apart, so a malformed asset cannot silently
produce a degraded persona prompt.

These tests reload the module with a patched
``load_dimension_descriptions`` to simulate YAML edits without touching
the on-disk file.  Each test restores the production loader and
re-imports the real module before returning, so subsequent tests in
the same process see the unpatched state.
"""

import pytest


class TestDimensionDriftGuard:
    def _reload_with_loader(self, monkeypatch, fake):
        """Patch ``load_dimension_descriptions`` and reload the module.

        Returns the freshly imported module on success, or re-raises
        whatever the import-time check raised.  The original module
        is restored after the test by another reload.
        """
        import importlib

        import agents.persona_behavior as pb
        from agents import prompt_loader

        monkeypatch.setattr(prompt_loader, "load_dimension_descriptions", fake)
        try:
            return importlib.reload(pb)
        finally:
            # Reload again with the real loader so subsequent tests in
            # this process see the production state.
            monkeypatch.undo()
            importlib.reload(pb)

    def test_extra_dimension_in_yaml_raises(self, monkeypatch):
        # YAML adds a dimension that the defaults table doesn't know
        # about.  The keyset mismatch must surface at import time.
        def fake(repo_root=None):
            return {
                "directness": {"balanced": "x"},
                "detail_focus": {"balanced": "x"},
                "formality": {"professional": "x"},
                "risk_tolerance": {"moderate": "x"},
                "expressiveness": {"moderate": "x"},
                "novel_dimension": {"middle": "x"},
            }

        with pytest.raises(RuntimeError, match="do not match"):
            self._reload_with_loader(monkeypatch, fake)

    def test_missing_dimension_in_yaml_raises(self, monkeypatch):
        # YAML drops a dimension the defaults table still references.
        def fake(repo_root=None):
            return {
                "directness": {"balanced": "x"},
                "detail_focus": {"balanced": "x"},
                "formality": {"professional": "x"},
                # risk_tolerance missing
                "expressiveness": {"moderate": "x"},
            }

        with pytest.raises(RuntimeError, match="do not match"):
            self._reload_with_loader(monkeypatch, fake)

    def test_renamed_default_value_raises(self, monkeypatch):
        # YAML renames the middle value of a dimension (e.g. "balanced"
        # → "neutral") without the matching ``_DIMENSION_DEFAULTS``
        # update.  Without this guard ``render_behavior({})`` would
        # silently produce no line for the renamed dimension.
        def fake(repo_root=None):
            return {
                "directness": {"neutral": "x"},  # was "balanced"
                "detail_focus": {"balanced": "x"},
                "formality": {"professional": "x"},
                "risk_tolerance": {"moderate": "x"},
                "expressiveness": {"moderate": "x"},
            }

        with pytest.raises(RuntimeError, match="not a known value"):
            self._reload_with_loader(monkeypatch, fake)
