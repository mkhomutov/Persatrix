import logging
import os
import tempfile

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.tools.registry import clear_registry

# Re-export the catch-up loopback orchestrator as a session-scoped
# fixture name so test files in this directory can request
# ``orchestrator`` without importing it. Without this, importing the
# fixture by name into each test file triggers ruff F811 (fixture
# parameters look like redefinitions of the imported name to ruff's
# scope analysis) — twelve false-positive findings across the
# catch-up suite. ``noqa: F401`` is the documented escape hatch for
# the pytest fixture-discovery pattern. (PR-265 review follow-up:
# extracted fixture to keep test_channel_catchup.py under the
# 500-line review cap after adding the timestamp / explicit-limit
# tests.)
from ._catchup_test_helpers import orchestrator  # noqa: F401


@pytest.fixture(autouse=True)
def _no_leaked_log_handlers():
    """Strip any stdlib root handler a test installs via ``configure_logging``.

    ``agents.observability.logging.configure_logging`` installs a
    ``ProcessorFormatter`` handler on the **root** logger whose
    ``foreign_pre_chain`` runs ``_ship_to_orchestrator`` — i.e. it enqueues every
    propagated record onto the *active* log shipper. The observability/audit
    rendered-egress tests call ``configure_logging`` and never remove that
    handler, so it leaks onto the root logger.

    Left in place, a *later* real-``AgentServer`` test (e.g.
    ``test_server_catchup_wiring`` / ``test_registration``'s ``TestSessionLifecycle``)
    starts a real shipper against a dead orchestrator; the shipper's
    stream-error path re-logs through that leaked handler, which re-enqueues onto
    the shipper's own queue — a self-feeding loop that wedged CI into a
    multi-minute hang (ISSUE-0108; not reproducible on macOS, only under the CI
    runner's gRPC/event-loop behaviour). Snapshot the root handler set and remove
    anything a test added, so no test can leak the ship-enqueueing handler into a
    later one."""
    root = logging.getLogger()
    saved = root.handlers[:]
    yield
    leaked = [h for h in root.handlers if h not in saved]
    if leaked:
        for handler in leaked:
            root.removeHandler(handler)
        # Force the next configure_logging() to rebuild rather than early-return
        # on its idempotency guard (the handler it would reuse is now gone).
        import agents.observability.logging as _obs_logging

        _obs_logging._configured = False


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.fixture(autouse=True)
def _isolate_session_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure ``PERSATRIX_SESSION_ID`` does not leak across tests.

    RFC 0031 Phase 1 makes :class:`agents.memory.MemoryStore` read
    ``PERSATRIX_SESSION_ID`` at construction so the task-agent /
    sub-agent path inherits the operator-namespace tag without an
    explicit kwarg at every write site
    (see ``agents/memory/facade.py`` ``__init__`` for the rationale).

    The flip side: any test that constructs a ``MemoryStore`` and
    asserts ``_session_id == "legacy"`` will silently pick up a shell-
    inherited value when the developer (or a CI job) happens to have
    ``PERSATRIX_SESSION_ID`` exported.  This autouse fixture
    monkeypatches the var out before every test so the env baseline is
    deterministic; tests that *want* the env-read path call
    ``monkeypatch.setenv("PERSATRIX_SESSION_ID", ...)`` themselves and
    that overrides this delete for the test's scope.

    Symmetric with ``_clean_registry`` above (same autouse pattern, same
    "deterministic baseline before the test runs" intent).  PR 4 review
    follow-up F5.
    """
    monkeypatch.delenv("PERSATRIX_SESSION_ID", raising=False)


@pytest.fixture(autouse=True)
def _resolvable_summarization_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the summarisation model resolvable for the close-path unit tests.

    v0.3.4 "no default provider": the shipped base ``config/optimization.yaml``
    ships the ``summarizer`` alias UNCONFIGURED, so the default
    ``summarization_model()`` → ``"summarizer"`` → ``model_aliases.resolve``
    raises a loud ``SystemExit``. ``summarize_closed_interaction`` catches
    that and degrades to ``SUMMARY_UNAVAILABLE_TEXT`` *before* reaching the
    envelope-parse / fact-extraction logic the close-path tests
    (``test_summarize_close_helpers`` / ``test_envelope_parse_observability``)
    pin — so without this they'd all collapse to the fallback.

    The patch targets only the names bound *inside* ``summarize_close`` (not the
    ``agents.optimization`` accessor ``test_optimization_routing`` asserts
    against), so its effect is confined to ``summarize_closed_interaction``
    callers. RFC 0033 Phase 3 retired the raw-vendor-ID pass-through, so a
    raw id no longer resolves; the baseline instead stubs ``resolve_model``
    to a canned :class:`~agents.model_aliases.ResolvedModel` directly — the
    close-path tests mock the LLM, so the model only needs to *resolve* to a
    valid record, never call out. Tests that deliberately want an
    unresolvable model re-monkeypatch this in the test body (raising
    ``SystemExit`` from the stub), which wins over this baseline.
    """
    import agents.persona_runtime.summarize_close as sc
    from agents.model_aliases import ResolvedModel

    monkeypatch.setattr(sc, "summarization_model", lambda: "summarizer")
    monkeypatch.setattr(
        sc,
        "resolve_model",
        lambda _ref: ResolvedModel(
            alias="summarizer",
            provider="anthropic",
            model="claude-haiku-test",
            input_per_1m_tokens=0.80,
            output_per_1m_tokens=4.00,
        ),
    )


@pytest.fixture
async def memory():
    """Create an initialized EpisodicMemory instance with in-memory DB."""
    mem = EpisodicMemory(agent_id="test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


@pytest.fixture
async def memory_pair():
    """Create two EpisodicMemory instances sharing different agent IDs on the same DB.

    Uses a temp file so both connections share state.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        mem_a = EpisodicMemory(agent_id="agent-a", db_path=path)
        mem_b = EpisodicMemory(agent_id="agent-b", db_path=path)
        await mem_a.initialize()
        await mem_b.initialize()
        yield mem_a, mem_b
        await mem_a.close()
        await mem_b.close()
    finally:
        os.unlink(path)
