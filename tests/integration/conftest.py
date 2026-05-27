import pytest


@pytest.fixture(autouse=True)
def _resolvable_summarization_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the summarisation model resolvable for the close-path integration tests.

    v0.3.4 "no default provider" ([amendment 2026-05-27]): the shipped base
    ``config/optimization.yaml`` ships the ``summarizer`` alias UNCONFIGURED, so
    the default ``summarization_model()`` → ``"summarizer"`` →
    ``model_aliases.resolve`` now raises a loud ``SystemExit``.
    ``summarize_closed_interaction`` catches that and degrades to
    ``SUMMARY_UNAVAILABLE_TEXT`` *before* reaching the LLM-summary / envelope-parse
    / fact-extraction logic the close-path integration suites pin
    (``test_summarize_on_close`` / ``test_summarize_on_close_phases`` /
    ``test_facts_extractor_*`` / ``test_interaction_multi_turn*``) — so without
    this they collapse to the fallback and fail on the LLM-summary assertion.

    Mirrors the unit-suite fixture in ``tests/unit/python/conftest.py``: it
    patches only the name bound *inside* ``summarize_close`` (not the
    ``agents.optimization`` accessor), so its effect is confined to
    ``summarize_closed_interaction`` callers. Those tests mock the LLM, so the
    model only needs to *resolve*, never call out — a raw vendor id the prefix
    table recognises does that. Tests that deliberately want an unresolvable
    model re-monkeypatch this in the test body, which wins over this baseline.

    See ISSUE-0076: CI never ran the full ``tests/integration/`` suite, so the
    base-config ``summarizer: unconfigured`` change reached ``main`` with these
    close-path tests silently broken.
    """
    import agents.persona_runtime.summarize_close as sc

    monkeypatch.setattr(sc, "summarization_model", lambda: "claude-haiku-test")
