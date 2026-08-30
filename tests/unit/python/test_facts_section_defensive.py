"""Unit tests for the PR 5c (RFC 0026 review follow-ups, slice 3)
defensive fixes on :mod:`agents.persona_runtime.facts_section` and
adjacent surfaces.

Four contracts pinned here:

* **L-1 — header truncation must not strip the item separator.**
  :func:`render_facts_section` previously built the header as
  ``f"Known facts about {subject}:\\n"`` and passed it to
  :meth:`MemoryBudget.try_add` whole.  When the canonical subject is
  long enough that the full header exceeds ``remaining`` but the
  truncated form still satisfies ``MIN_TOKENS_FACTS=24``, the
  returned form is ``"Known facts about very_long_user…"`` — the
  trailing ``:\\n`` was the last thing in the source string and is
  lost to the ellipsis.  The subsequent
  ``admitted_header + "\\n".join(items)`` then glued the first item
  directly to the header with no separator.  The fix admits the
  subject-bearing prefix and the trailing newline as a two-part
  admission so the rendering invariant survives truncation.

* **L-3 — :func:`resolve_facts_config` defensive against null budget knobs.**
  Operators who write ``budget_tokens: null`` and bypass
  ``make validate`` used to crash agent construction with
  ``TypeError: int() argument must be a string, a bytes-like object or
  a real number, not 'NoneType'``.  Defence-in-depth: the resolver
  now collapses ``None`` to the default before the ``int()`` call.

* **N-2 — :func:`recall_facts_for_event` signature cleanup.**
  The pre-PR-5c signature carried an ``agent_id`` kwarg that the
  helper used only inside its WARNING log template;
  :meth:`FactStore.recall` already filters by ``self._agent_id``
  (the store is per-agent), so the kwarg falsely implied the helper
  accepted an agent filter.  PR 5c drops the kwarg and reads
  ``fact_store.agent_id`` off the store at the log site.

* **extraction_model — orphan knob dropped from the schema.**
  See :class:`TestExtractionModelFieldDropped` for the decision and
  rationale.

The L-2 (``FactStore.store`` canonicalisation) follow-up is pinned in
:mod:`tests.unit.python.test_fact_store_canonicalisation`.
"""

from __future__ import annotations

import pytest

# ─── L-1: header truncation must keep item separator (PR 5c) ───


@pytest.mark.asyncio
class TestHeaderTruncationPreservesItemSeparator:
    """Long-subject + tight-remaining → truncated header must not strip
    the trailing newline that frames the first item line.

    Bug shape (pre-PR-5c): the header
    ``f"Known facts about {subject}:\\n"`` was admitted as one unit; if
    truncation kicked in the ellipsis replaced the tail (including the
    ``\\n``) and the rendered block read like
    ``"Known facts about very_long_user…- bob prefers tea"`` —
    the item glued to the truncated header with no separator.

    Reachability bound is narrow (subject ≳ 60 chars **and** budget
    remainder lands between the truncation floor and the full-header
    cost at the point the header is admitted), so the production
    happy path under ``MEMORY_BUDGET_TOKENS=1500`` rarely triggers
    it.  Pin defensive — the dementia-test invariant should not
    depend on a particular budget remainder for the prompt to be
    well-formed.
    """

    async def test_truncated_header_does_not_glue_to_first_item(
        self,
    ) -> None:
        """Construct a budget tight enough to force header truncation
        and assert the rendered block has a newline separator between
        whatever header survived and the first item line.

        The exact header text is not pinned — the truncation strategy
        owns its content.  Only the invariant under test is pinned:
        the bug-signature glued form
        ``"<truncated header>…<item line>"`` (item glued directly to
        the trailing ellipsis with no newline) never appears.
        """
        from agents.memory.facts import Fact  # noqa: PLC0415
        from agents.persona_runtime.facts_section import (  # noqa: PLC0415
            render_facts_section,
        )
        from agents.persona_runtime.memory_budget import (  # noqa: PLC0415
            MemoryBudget,
        )

        # 200-char random-like subject tokenises to ~107 tokens under
        # cl100k_base (no repeated-token compression).  The full
        # header ``"Known facts about <subject>:\n"`` then costs ~111
        # tokens and the item line ~110 tokens.  Sized so the item
        # admits whole and the header truncates rather than drops.
        long_subject = (
            "dwtgmlqucavltalsfyxaaoyjfkaflmggflhavoqezwdissykvr"
            "xnmuycelpyjbvwiavixunhswthtpzevybxsndhwwrnssgrayzj"
            "qrhfucutuoejyzwjlkjxhnopowfvfvbifbynhalqaqgvbpjawd"
            "uybudukulalmqvuxsazyolfwfwhuyenztpsxfflnrcwphmuhxv"
        )

        facts = [
            Fact(
                fact_id="long-subject-fact",
                agent_id="dementia-agent",
                subject=long_subject,
                predicate="prefers",
                object="tea",
                certainty=1.0,
                source_interaction_id="i1",
                asserted_at=1000.0,
                last_recalled_at=None,
                superseded_by=None,
                session_id="legacy",
            ),
        ]

        # Budget sized so:
        #   * The fact line ``"- <long_subject> prefers tea"`` (~110
        #     tokens) admits whole.
        #   * Remaining after the item is ~90 tokens — exceeds the
        #     ``MIN_TOKENS_FACTS=24`` truncation floor but cannot fit
        #     the full ~111-token header.  The pre-PR-5c shape
        #     truncated the header and lost the trailing ``:\n``.
        budget = MemoryBudget(total_tokens=200)

        section = render_facts_section(
            facts, budget, facts_budget_tokens=200,
        )

        # Two acceptable outcomes:
        #   (a) Header truncated cleanly with separator preserved →
        #       section non-None, item line starts on its own newline.
        #   (b) Header dropped entirely → section is None.
        # The bug signature rejected is a non-None section whose
        # content lacks "\n- " — the item glued to the truncated
        # header with no separator.
        #
        # Note (PR #346 review L-2, re-noted for ISSUE-0116): under
        # the original sizing the exit was always branch (b) — the
        # full-subject header truncated to the exact remainder, so the
        # separator's ``try_add`` failed and the block dropped.  Since
        # the ISSUE-0116 bounded header template, the header for this
        # 200-char subject costs only the bounded prefix (~30 tokens),
        # admits whole against the ~90-token remainder, and the exit
        # is branch (a): the ``"\n- "`` assertion below executes and
        # passes.  The bug-shape rejection (non-None section without
        # ``"\n- "``) remains the guard for any sizing drift that
        # re-opens the truncation window.
        if section is not None:
            assert "\n- " in section.content, (
                "L-1 regression: item line is not framed by a newline "
                "after the header — the truncation path stripped the "
                "header's trailing separator.  Bug signature: "
                f"section.content={section.content!r}"
            )

    async def test_two_part_admission_emits_separator_when_budget_fits(
        self,
    ) -> None:
        """Complementary happy-path pin (PR #346 review L-2).

        :meth:`test_truncated_header_does_not_glue_to_first_item`
        above pins only the *bug-shape rejection* branch — under its
        sizing ``section`` is always ``None`` so its ``"\\n- "``
        assertion never runs.  This test pins the *success path*:
        under a comfortable budget the new ``prefix + "\\n" + items``
        admission produces a well-formed block with the separator
        preserved.  Without it the two-part admission idiom is
        exercised only on its drop-the-block branch — a refactor
        breaking the ``admitted_prefix + admitted_sep +
        "\\n".join(items)`` concatenation would ship green.

        No "truncated prefix + separator preserved" case is added:
        ``MemoryBudget`` truncates oversized text to exactly
        ``remaining`` tokens, decrementing ``remaining`` to ``0`` so
        the separator's ``try_add`` always fails.  That branch is
        present in the admission code but practically unreachable,
        and a test depending on the tokeniser leaving slack would be
        brittle.  The two reachable outcomes — drop and clean-emit —
        are both pinned.
        """
        from agents.memory.facts import Fact  # noqa: PLC0415
        from agents.persona_runtime.facts_section import (  # noqa: PLC0415
            render_facts_section,
        )
        from agents.persona_runtime.memory_budget import (  # noqa: PLC0415
            MemoryBudget,
        )

        facts = [
            Fact(
                fact_id="fact-1",
                agent_id="dementia-agent",
                subject="bob",
                predicate="prefers",
                object="tea",
                certainty=1.0,
                source_interaction_id="i1",
                asserted_at=1000.0,
                last_recalled_at=None,
                superseded_by=None,
                session_id="legacy",
            ),
            Fact(
                fact_id="fact-2",
                agent_id="dementia-agent",
                subject="bob",
                predicate="lives_in",
                object="Berlin",
                certainty=1.0,
                source_interaction_id="i2",
                asserted_at=1001.0,
                last_recalled_at=None,
                superseded_by=None,
                session_id="legacy",
            ),
        ]

        # Comfortable budget — header + items fit whole with
        # headroom; no truncation pressure on any admission.
        budget = MemoryBudget(total_tokens=500)
        section = render_facts_section(
            facts, budget, facts_budget_tokens=500,
        )

        assert section is not None, (
            "expected the section to emit under a comfortable budget"
        )
        # The new two-part admission idiom: prefix (header sans
        # newline) + "\n" separator + items joined by "\n".  The
        # rendered block must therefore start with the header text
        # and contain a "\n- " separator framing each item.
        assert section.content.startswith("Known facts about bob:"), (
            "rendered block must lead with the canonical header "
            f"prefix; got {section.content!r}"
        )
        assert "\n- bob prefers tea" in section.content, (
            "first item must be framed on its own line — the two-"
            "part admission must place a newline between the header "
            f"prefix and the first item.  Got {section.content!r}"
        )
        assert "\n- bob lives_in Berlin" in section.content, (
            "subsequent items keep the same \"\\n- \" framing; the "
            "items list is joined by \"\\n\" so the second item is "
            f"also separator-prefixed.  Got {section.content!r}"
        )


# ─── L-3: resolve_facts_config null-budget guard (PR 5c) ─────


class TestResolveFactsConfigNullBudgetGuard:
    """``budget_tokens: None`` in a programmatic config must collapse to
    the default rather than crashing :func:`resolve_facts_config` with
    ``TypeError: int(None)``.

    Production configs are gated by ``make validate`` against
    :file:`schemas/agent.schema.json` (``type: integer``, ``minimum:
    0``), which rejects ``null``.  But test fixtures, programmatic
    dict-built configs, and any path that bypasses ``make validate``
    can drop a ``None`` through — and ``int(None)`` raises.  Defence-
    in-depth: the resolver collapses ``None`` to the default before
    the ``int()`` call.
    """

    def test_none_budget_tokens_collapses_to_default(self) -> None:
        from agents.persona_runtime.facts_section import (  # noqa: PLC0415
            DEFAULT_FACTS_BUDGET_TOKENS,
            resolve_facts_config,
        )

        config = {
            "memory": {
                "facts": {
                    "enabled": True,
                    "budget_tokens": None,
                },
            },
        }
        enabled, budget = resolve_facts_config(config)
        assert enabled is True
        assert budget == DEFAULT_FACTS_BUDGET_TOKENS

    def test_missing_budget_tokens_still_uses_default(self) -> None:
        """Pre-existing behaviour — the ``raw is None`` branch must
        not regress the missing-key path."""
        from agents.persona_runtime.facts_section import (  # noqa: PLC0415
            DEFAULT_FACTS_BUDGET_TOKENS,
            resolve_facts_config,
        )

        config = {"memory": {"facts": {"enabled": True}}}
        enabled, budget = resolve_facts_config(config)
        assert enabled is True
        assert budget == DEFAULT_FACTS_BUDGET_TOKENS

    def test_explicit_int_budget_tokens_honoured(self) -> None:
        """The fast path that operators actually hit must keep
        working — guarding against ``None`` should not change the
        admit-an-integer path."""
        from agents.persona_runtime.facts_section import (  # noqa: PLC0415
            resolve_facts_config,
        )

        config = {
            "memory": {
                "facts": {"enabled": True, "budget_tokens": 350},
            },
        }
        enabled, budget = resolve_facts_config(config)
        assert enabled is True
        assert budget == 350


# ─── N-2: recall_facts_for_event signature cleanup (PR 5c) ─


@pytest.mark.asyncio
class TestRecallFactsForEventSignature:
    """The pre-PR-5c signature was::

        async def recall_facts_for_event(
            fact_store, event, *, agent_id, limit=FACTS_RECALL_LIMIT,
        ) -> list[Fact]: ...

    PR 5c drops the ``agent_id`` kwarg.  Rationale:

    * :meth:`FactStore.recall` already filters by ``self._agent_id``
      (the store is per-agent ACL — RFC 0008 §H), so the kwarg never
      participated in the SQL filter.
    * The kwarg's only consumer was the log template at the recall-
      failure branch (``"Agent %s: facts recall …"``).  After the
      cleanup the log reads ``fact_store.agent_id`` off the store
      itself, which is the authoritative source.
    * Keeping the kwarg in the signature is misleading documentation
      — a future reader would assume the helper accepts an agent
      filter and write a call site that disagrees with the store's
      internal scope.
    """

    async def test_helper_does_not_require_agent_id_kwarg(self) -> None:
        """After PR 5c, callers should be able to invoke the helper
        without an ``agent_id`` kwarg.  Pin via direct call against a
        stub store — the helper reads ``fact_store.agent_id`` off
        the store for log purposes only.
        """
        from unittest.mock import AsyncMock, MagicMock  # noqa: PLC0415

        from agents.persona_runtime.facts_section import (  # noqa: PLC0415
            recall_facts_for_event,
        )

        fact_store = MagicMock()
        fact_store.agent_id = "dementia-agent"
        fact_store.recall = AsyncMock(return_value=[])

        event = MagicMock()
        event.sender_id = "bob"

        # No ``agent_id`` kwarg — calling the post-PR-5c shape.
        result = await recall_facts_for_event(fact_store, event)
        assert result == []
        # Recall was invoked once per seed (self + sender = 2).
        assert fact_store.recall.await_count == 2

    async def test_helper_logs_agent_id_from_store_on_failure(
        self,
    ) -> None:
        """When ``FactStore.recall`` raises, the WARNING log line
        derives the ``agent_id`` from the store, not from a passed-in
        kwarg.  Pin so a future refactor that loses the store-side
        attribution surfaces immediately."""
        import logging  # noqa: PLC0415
        from unittest.mock import AsyncMock, MagicMock  # noqa: PLC0415

        from agents.persona_runtime.facts_section import (  # noqa: PLC0415
            recall_facts_for_event,
        )

        fact_store = MagicMock()
        fact_store.agent_id = "dementia-agent"
        fact_store.recall = AsyncMock(
            side_effect=RuntimeError("backend down"),
        )

        event = MagicMock()
        event.sender_id = "bob"

        # Capture WARNING log records to assert agent_id sourcing.
        records: list[logging.LogRecord] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Handler(level=logging.WARNING)
        recall_logger = logging.getLogger(
            "agents.persona_runtime.facts_section",
        )
        recall_logger.addHandler(handler)
        try:
            result = await recall_facts_for_event(fact_store, event)
        finally:
            recall_logger.removeHandler(handler)

        # Recall is logged-and-continued so the result is empty.
        assert result == []
        # The WARNING line names the store's agent_id.
        warnings = [r for r in records if r.levelno >= logging.WARNING]
        assert warnings, "expected a WARNING log on recall failure"
        rendered = warnings[0].getMessage()
        assert "dementia-agent" in rendered


# ─── extraction_model decision: drop the field (PR 5c) ────


class TestExtractionModelFieldDropped:
    """PR 5c resolves the ``memory.facts.extraction_model`` orphan-knob
    follow-up by dropping the field from the schema and the example
    config.

    Decision (option 1 of the three the plan offered):

    * Option 1 — **drop the field** (chosen).  The combined
      summarize + extract call is a single LLM call; an
      ``extraction_model`` knob distinct from the summariser model
      is a category error.  No Python consumer exists today, so
      dropping is additive-compatible: configs that do not set the
      key remain valid.  Configs that *do* set it will fail
      ``additionalProperties: false`` validation — which is the
      correct failure mode, because the operator was relying on a
      no-op knob.
    * Option 2 — plumb through ``summarize_close`` — rejected.  The
      combined call uses one model; "extraction_model" would
      silently change the summariser model, surprising operators.
    * Option 3 — warn at config load — rejected.  Defensible as a
      hold-over but not a steady state; option 1 is cleaner.
    """

    def test_schema_has_no_extraction_model_field(self) -> None:
        """The ``memory.facts`` block in ``schemas/agent.schema.json``
        no longer declares ``extraction_model``."""
        import json  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        schema = json.loads(
            Path("schemas/agent.schema.json").read_text(encoding="utf-8"),
        )
        # Both ``agent`` and ``memory`` live under ``definitions``
        # — the agent block $refs into ``definitions.memory``, which
        # in turn owns the ``facts`` subschema.  Walk the definitions
        # directly to avoid materialising the $ref chain.
        memory_def = schema["definitions"]["memory"]
        facts_props = memory_def["properties"]["facts"]["properties"]
        assert "extraction_model" not in facts_props, (
            "PR 5c dropped memory.facts.extraction_model; "
            f"got {sorted(facts_props.keys())}"
        )

    def test_example_config_has_no_extraction_model_field(self) -> None:
        """The bundled :file:`config/agents.yaml` example no longer
        names ``extraction_model`` — operators reading the example
        for the canonical config shape do not see the orphan knob."""
        from pathlib import Path  # noqa: PLC0415

        config_text = Path("config/agents.yaml").read_text(
            encoding="utf-8",
        )
        assert "extraction_model" not in config_text, (
            "PR 5c dropped memory.facts.extraction_model from "
            "config/agents.yaml; the example still references it"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
