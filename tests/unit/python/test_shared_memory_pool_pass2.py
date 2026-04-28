"""PR #223 pass-2 deep-review regression tests for shared memory pools.

Split out of ``test_shared_memory_pool.py`` to keep that file under the
project's 500-line per-file size limit (pre-commit ``file size`` check).
The cases here pin two pass-2 review findings:

* ``S3-tag`` — ``read_via_facade`` AND-tag filter must over-fetch so
  ``limit`` is honoured after the post-filter trim.
* ``N-persona`` — persona runtime ``initialize_memory`` must keep the
  ``shared_pools`` kwarg shape (keyword-only, default ``None``) so an
  accidental wiring change cannot land silently.
"""

from __future__ import annotations

from typing import Any

from agents.memory.facade import MemoryFacade
from agents.memory.shared_pool import (
    SharedMemoryPool,
    SharedPoolConfig,
    SharedPoolRegistry,
)


async def test_tag_filter_overfetches_under_limit(tmp_path: Any) -> None:
    """PR #223 pass-2 review S3-tag: AND-tag filter must honour ``limit``.

    ``read_via_facade`` applies the AND-tag filter *after* ``pool.read``
    has trimmed to ``limit``.  Without an over-fetch a caller asking
    for ``limit=2`` with a tag set could receive 0 or 1 entries even
    when many tagged matches exist deeper in the FTS5 ranking — the
    same trim-after-limit class as PR-220 review M3 (tags) and PR-223
    pass-1 S3 (min_confidence).  After the fix the facade over-fetches
    by ``_TAG_FILTER_OVERFETCH_FACTOR`` (= ``_MIN_CONFIDENCE_OVERFETCH_FACTOR``)
    and trims back to ``limit`` after the AND-filter.
    """
    db = str(tmp_path / "tag_overfetch.db")
    cfg = SharedPoolConfig(
        name="overfetch",
        readers=frozenset({"alice"}),
        writers=frozenset({"alice"}),
    )
    pool_inst = SharedMemoryPool(cfg, db_path=db)
    await pool_inst.initialize()
    registry = SharedPoolRegistry({"overfetch": pool_inst})
    facade = MemoryFacade(
        agent_id="alice", db_path=db, shared_pools=registry,
    )
    await facade.initialize()
    try:
        # 5 untagged entries (would be returned first by FTS5) + 2
        # tagged entries that match the requested tag.  With limit=2
        # the un-overfetched code path returned the 2 untagged hits,
        # then the AND-tag filter dropped them to 0.
        for i in range(5):
            await facade.publish_to_pool(
                "overfetch", f"signal plain {i}", confidence=0.5,
            )
        await facade.publish_to_pool(
            "overfetch", "signal tagged A", confidence=0.5,
            tags=("wanted",),
        )
        await facade.publish_to_pool(
            "overfetch", "signal tagged B", confidence=0.5,
            tags=("wanted",),
        )
        out = await facade.read_from_pool(
            "overfetch", "signal", limit=2, tags=("wanted",),
        )
        assert len(out) == 2
        assert all("wanted" in e.tags for e in out)
    finally:
        await facade.close()
        await pool_inst.close()


def test_persona_runtime_initialize_memory_accepts_shared_pools_kwarg() -> None:
    """PR #223 pass-2 review N-persona: persona runtime must continue to
    accept the ``shared_pools`` kwarg as a no-op until wiring lands.

    Contract-style assertion (signature shape) so an accidental wiring
    cannot land silently.  ``BaseAgent.initialize_memory`` accepts the
    kwarg in PR 4; the persona runtime accepts but discards it (see
    ``agents/persona_runtime/state_persistence.py::initialize_memory``
    docstring).  When persona-side wiring lands in a follow-on PR this
    test should be replaced with a behavioural assertion that
    ``shared_pools`` is actually threaded into the persona memory tier.
    """
    import inspect

    from agents.persona_runtime.state_persistence import (
        _StatePersistenceMixin,
    )

    sig = inspect.signature(_StatePersistenceMixin.initialize_memory)
    assert "shared_pools" in sig.parameters, (
        "persona runtime initialize_memory must accept the shared_pools "
        "kwarg for signature parity with BaseAgent.initialize_memory"
    )
    # Keyword-only — matches BaseAgent.initialize_memory.
    assert (
        sig.parameters["shared_pools"].kind
        is inspect.Parameter.KEYWORD_ONLY
    )
    # Default ``None`` so callers can pass it unconditionally.
    assert sig.parameters["shared_pools"].default is None
