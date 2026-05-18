"""RFC 0029 Phase 1 PR 3 — downstream call-site refactor (TDD pins).

PR 3 routes the persona-runtime / sub-agent memory call sites — and the
``create_persona_agent`` factory — through the ``agents.memory``
``MemoryStore`` facade, off the legacy ``MemoryFacade`` shim:

- ``build_personal_tiers`` constructs the three per-agent personal tiers
  (``EpisodicMemory`` / ``RelationshipMemory`` / ``FactStore``) *inside*
  ``agents/memory/`` so the RFC 0029 PR 2 ``DeprecationWarning`` on direct
  external construction stays silent on the production path.
- ``create_persona_agent`` is the sole production site outside
  ``agents/memory/`` that constructed those tiers directly; after PR 3 it
  builds them through the facade, closing the deprecation window.
- No ``MemoryFacade`` reference survives in the migrated call sites
  (``persona_runtime/``, ``sub_agents/``, ``persona.py``) — the alias
  lives on only as the documented one-minor-version compat shim.
- ``tests/perf/personal_tier_latency.py`` ships and *runs* (the baseline
  capture + enforcing gate are RFC 0029 Phase 1 PR 5).
"""

from __future__ import annotations

import importlib.util
import warnings
from pathlib import Path


from agents.memory.episodic import EpisodicMemory
from agents.memory.facts import FactStore
from agents.memory.relationship import RelationshipMemory

_DIRECT_CONSTRUCTION_WARNING = "Direct construction of"


# ─── build_personal_tiers — the construction seam ──────────────


def test_build_personal_tiers_is_exported_from_memory_package() -> None:
    """PR 3 surfaces the construction seam on the ``agents.memory`` facade."""
    from agents.memory import PersonalTiers, build_personal_tiers
    from agents.memory.personal_tiers import (
        PersonalTiers as CanonicalTiers,
    )
    from agents.memory.personal_tiers import (
        build_personal_tiers as canonical_builder,
    )

    assert PersonalTiers is CanonicalTiers
    assert build_personal_tiers is canonical_builder


def test_build_personal_tiers_returns_the_three_personal_tiers() -> None:
    from agents.memory import build_personal_tiers

    tiers = build_personal_tiers("alice", db_path=":memory:")

    assert isinstance(tiers.episodic, EpisodicMemory)
    assert isinstance(tiers.relationship, RelationshipMemory)
    assert isinstance(tiers.facts, FactStore)
    # agent_id threads through to every tier.
    assert tiers.episodic.agent_id == "alice"
    assert tiers.relationship.agent_id == "alice"


def test_build_personal_tiers_emits_no_deprecation_warning() -> None:
    """Construction lands inside ``agents/memory/`` — the PR 2 boundary
    ``DeprecationWarning`` (direct external tier construction) stays silent.
    """
    from agents.memory import build_personal_tiers

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        build_personal_tiers("alice", db_path=":memory:")

    offenders = [
        str(w.message)
        for w in caught
        if issubclass(w.category, DeprecationWarning)
        and _DIRECT_CONSTRUCTION_WARNING in str(w.message)
    ]
    assert offenders == [], f"unexpected boundary warnings: {offenders}"


async def test_personal_tiers_initialize_and_close_cleanly() -> None:
    """The handles are live tiers — the persona runtime drives their
    own ``initialize()`` / ``close()`` lifecycle (behaviour unchanged)."""
    from agents.memory import build_personal_tiers

    tiers = build_personal_tiers("alice", db_path=":memory:")
    await tiers.episodic.initialize()
    await tiers.relationship.initialize()
    await tiers.facts.initialize()
    try:
        # A query against the freshly-opened tier proves the handle is a
        # live, initialised EpisodicMemory (empty corpus → zero episodes).
        assert await tiers.episodic.count_episodes() == 0
    finally:
        await tiers.facts.close()
        await tiers.episodic.close()
        await tiers.relationship.close()


# ─── create_persona_agent — deprecation window closed ─────────


def test_create_persona_agent_emits_no_deprecation_warning() -> None:
    """``create_persona_agent`` was the only production site outside
    ``agents/memory/`` that tripped the PR 2 boundary warning. After PR 3
    it builds the personal tiers through the facade — the warning is
    silent on the production path.
    """
    from agents.persona import create_persona_agent

    from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )

    offenders = [
        str(w.message)
        for w in caught
        if issubclass(w.category, DeprecationWarning)
        and _DIRECT_CONSTRUCTION_WARNING in str(w.message)
    ]
    assert offenders == [], f"unexpected boundary warnings: {offenders}"


# ─── MemoryFacade swept out of the migrated call sites ────────


def _agents_dir() -> Path:
    import agents

    return Path(agents.__file__).resolve().parent


def test_no_memoryfacade_reference_in_migrated_call_sites() -> None:
    """RFC 0029 Phase 1 PR 3: no ``MemoryFacade`` reference survives in
    ``persona_runtime/``, ``sub_agents/`` or ``persona.py`` — the alias
    lives on only as the documented compat shim in ``agents/memory/``.
    """
    agents_dir = _agents_dir()
    targets: list[Path] = [
        agents_dir / "persona.py",
        *(agents_dir / "persona_runtime").rglob("*.py"),
        *(agents_dir / "sub_agents").rglob("*.py"),
    ]
    offenders: list[str] = []
    for path in targets:
        if "__pycache__" in path.parts:
            continue
        if "MemoryFacade" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(agents_dir)))
    assert offenders == [], (
        f"MemoryFacade still referenced in migrated call sites: {offenders}"
    )


def test_summarize_close_routes_compress_through_memory_store() -> None:
    """The one persona-runtime ``MemoryFacade.compress`` call site is
    migrated to ``MemoryStore`` (same class object, behaviour identical)."""
    from agents.memory.store import MemoryStore
    from agents.persona_runtime import summarize_close

    assert summarize_close.MemoryStore is MemoryStore
    assert not hasattr(summarize_close, "MemoryFacade")


# ─── perf harness ships and runs (gate enforcement is PR 5) ───


def _load_perf_harness():
    repo_root = Path(__file__).resolve().parents[3]
    harness_path = repo_root / "tests" / "perf" / "personal_tier_latency.py"
    assert harness_path.is_file(), f"perf harness missing: {harness_path}"
    spec = importlib.util.spec_from_file_location(
        "personal_tier_latency", harness_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_personal_tier_latency_harness_runs() -> None:
    """RFC 0029 §Test Strategy perf harness: it *runs* and emits a p99
    number here (PR 3). PR 5 captures the baseline JSON and flips it into
    an enforcing CI gate — this PR only pins that the harness executes.
    """
    harness = _load_perf_harness()
    result = await harness.measure_recall_p99(corpus_size=24, iterations=24)
    assert isinstance(result, dict)
    p99 = result["recall_episodes_p99_ms"]
    assert isinstance(p99, float)
    assert p99 >= 0.0
    assert result["corpus_size"] == 24
    assert result["iterations"] == 24
