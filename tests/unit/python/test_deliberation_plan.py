"""RFC 0051 Phase 2 (v0.3.10) — the ``CompositionPlan`` parser + renderer.

PR 3 of the RFC 0051 PR plan (``docs/rfcs/0051-pr-plan.md``). PR 1–2 made a
persona privately decide *whether* to post (the structured silence verdict);
Phase 2 adds *what* the post should accomplish — a private ``CompositionPlan``
threaded into the Tier-C compose. This file pins the pure value type, its
regex-tolerant parser, and the renderer in isolation
([RFC 0051 §C](../../docs/rfcs/0051-reasoning-before-posting.md) / §Phase 2):
no ``action_loop`` / agent coupling, so it is the unit the no-leak integration
test (``tests/integration/test_deliberation_no_leak.py``) points straight at.

Load-bearing contracts:

* **Fail-closed to "no plan", not to silence.** An unparseable plan returns
  ``None`` and the persona composes *unplanned* — the opposite bias to the
  gate's bias-to-*silence*, because by Phase 2 the gate has already decided the
  persona *should* post. A missing plan must never block the post.
* **``intent`` is the anchor.** No parseable ``intent`` → no plan. The other
  fields are best-effort around it (``addressed_to`` defaults to ``channel``).
* **``key_points`` is capped at 3** — the substance to land, not an essay.
"""

from __future__ import annotations

from agents.persona_runtime.deliberation_plan import (
    CompositionPlan,
    parse_plan,
    render_plan_section,
)

_WELL_FORMED = (
    "should_post: yes\n"
    "reason_code: adds_substance\n"
    "intent: surface the write-throughput risk Redis carries for this workload\n"
    "key_points: Redis is single-threaded on writes; "
    "our p99 is write-heavy; Postgres LISTEN/NOTIFY is an alternative\n"
    "addressed_to: iron-fox\n"
    "avoid_restating: that Redis is fast for reads; that it is the obvious cache\n"
    "reason_note: iron-fox only weighed read latency\n"
)


class TestParsePlanWellFormed:
    def test_full_plan_parses_every_field(self):
        plan = parse_plan(_WELL_FORMED)
        assert plan is not None
        assert plan.intent == (
            "surface the write-throughput risk Redis carries for this workload"
        )
        assert plan.key_points == (
            "Redis is single-threaded on writes",
            "our p99 is write-heavy",
            "Postgres LISTEN/NOTIFY is an alternative",
        )
        assert plan.addressed_to == "iron-fox"
        assert plan.avoid_restating == (
            "that Redis is fast for reads",
            "that it is the obvious cache",
        )

    def test_plan_is_an_immutable_value_type(self):
        """Frozen value type — the plan is transported, never mutated en route to
        compose (RFC 0051 §C "separate value types")."""
        plan = parse_plan(_WELL_FORMED)
        assert isinstance(plan, CompositionPlan)
        assert isinstance(plan.key_points, tuple)
        assert isinstance(plan.avoid_restating, tuple)

    def test_equals_separator_is_tolerated(self):
        """The plan grammar mirrors the verdict grammar's ``[:=]`` tolerance so a
        model that answers ``intent = ...`` still parses."""
        plan = parse_plan("intent = ship the cache decision\nkey_points = a; b")
        assert plan is not None
        assert plan.intent == "ship the cache decision"
        assert plan.key_points == ("a", "b")

    def test_key_points_capped_at_three(self):
        """≤3 (RFC 0051 §C) — a model that lists five points is trimmed to the
        first three, never an essay smuggled into the compose prompt."""
        plan = parse_plan(
            "intent: decide the datastore\n"
            "key_points: one; two; three; four; five\n",
        )
        assert plan is not None
        assert plan.key_points == ("one", "two", "three")

    def test_blank_list_entries_are_dropped(self):
        plan = parse_plan("intent: x\nkey_points: a;; ; b\navoid_restating: ;;\n")
        assert plan is not None
        assert plan.key_points == ("a", "b")
        assert plan.avoid_restating == ()


class TestParsePlanDefaults:
    def test_intent_only_yields_a_plan_addressed_to_channel(self):
        """``intent`` alone is enough — the others are best-effort; with no
        ``addressed_to`` the post addresses the whole ``channel`` (the open-floor
        default)."""
        plan = parse_plan("intent: name the risk no one has raised")
        assert plan is not None
        assert plan.intent == "name the risk no one has raised"
        assert plan.key_points == ()
        assert plan.addressed_to == "channel"
        assert plan.avoid_restating == ()

    def test_intent_tolerates_surrounding_verdict_lines(self):
        """The plan rides in the *same* response as the should_post/reason_code
        verdict, so the parser must ignore those lines and find the plan fields."""
        plan = parse_plan(
            "should_post: yes\nreason_code: adds_substance\n"
            "intent: add the missing write-path caveat\n",
        )
        assert plan is not None
        assert plan.intent == "add the missing write-path caveat"


class TestParsePlanDropsEchoedPlaceholders:
    """A small ``fast`` model with nothing to fill a field may echo the user
    snippet's literal ``<…>`` placeholder verbatim — the same failure the verdict
    parser already guards for ``reason_note``
    (``test_salience_bid_reasoning.py::…placeholder_echo_is_dropped``). The plan
    parser must not let that template noise become a "plan": an echoed ``intent``
    is no anchor, and an echoed list/addressed field is no value. This matters
    twice over — the echo is garbage *and*, because the plan is rendered into the
    Tier-C compose as a trusted system-prompt section, an un-stripped echo would
    inject template instructions into that prompt (RFC 0051 §E inbound-direction
    note)."""

    def test_echoed_intent_placeholder_is_no_plan(self):
        assert parse_plan(
            "should_post: yes\nreason_code: adds_substance\n"
            "intent: <one clause — what your post should accomplish; only if posting>\n",
        ) is None

    def test_echoed_list_placeholders_are_dropped(self):
        plan = parse_plan(
            "intent: name the unraised write-path risk\n"
            "key_points: <up to 3 points to land, separated by ';'; only if posting>\n"
            "avoid_restating: <what's already been said that you won't repeat, "
            "';'-separated — optional>\n",
        )
        assert plan is not None
        assert plan.key_points == ()
        assert plan.avoid_restating == ()

    def test_echoed_addressed_to_placeholder_falls_back_to_channel(self):
        plan = parse_plan(
            "intent: name the unraised write-path risk\n"
            "addressed_to: <a participant's name, or 'channel'; only if posting>\n",
        )
        assert plan is not None
        assert plan.addressed_to == "channel"


class TestParsePlanBoundsFieldLength:
    """Each field is length-bounded (mirrors the verdict's ``reason_note`` 240-char
    cap) so a runaway clause cannot bloat the trusted compose prompt — the
    primary bound on how much transcript-derived text the plan can carry into it
    (RFC 0051 §E inbound-direction note)."""

    def test_long_intent_is_truncated(self):
        plan = parse_plan("intent: " + "x" * 500)
        assert plan is not None
        assert len(plan.intent) == 240

    def test_long_list_payload_is_bounded(self):
        plan = parse_plan("intent: x\nkey_points: " + "y" * 500)
        assert plan is not None
        assert plan.key_points  # still parses a (bounded) point
        assert all(len(point) <= 240 for point in plan.key_points)
        assert sum(len(point) for point in plan.key_points) <= 240


class TestParsePlanCapsAvoidRestating:
    def test_avoid_restating_capped_like_key_points(self):
        """``avoid_restating`` carries the same anti-essay bound as ``key_points``
        — both are rendered verbatim into the compose prompt, so neither may
        smuggle an unbounded list past the gate (RFC 0051 §C)."""
        plan = parse_plan(
            "intent: x\navoid_restating: a; b; c; d; e; f\n",
        )
        assert plan is not None
        assert plan.avoid_restating == ("a", "b", "c")


class TestParsePlanFailClosedToNoPlan:
    def test_missing_intent_is_no_plan(self):
        """No anchor → ``None`` → compose *unplanned* (not blocked). The bias is
        opposite the gate's bias-to-silence (RFC 0051 §Phase 2)."""
        assert parse_plan("key_points: a; b\naddressed_to: channel") is None

    def test_empty_text_is_no_plan(self):
        assert parse_plan("") is None
        assert parse_plan(None) is None

    def test_blank_intent_value_is_no_plan(self):
        """A present-but-empty ``intent:`` line is not a usable anchor."""
        assert parse_plan("intent:   \nkey_points: a") is None


class TestRenderPlanSection:
    def test_renders_a_stable_private_section_with_every_field(self):
        plan = parse_plan(_WELL_FORMED)
        assert plan is not None
        section = render_plan_section(plan)
        assert plan.intent in section
        for point in plan.key_points:
            assert point in section
        assert plan.addressed_to in section
        for skip in plan.avoid_restating:
            assert skip in section

    def test_section_marks_itself_private(self):
        """The rendered section must instruct the persona that the plan is
        PRIVATE and must never be revealed — the in-prompt half of the §E wall
        (the structural half is that it is never an AgentAction / never
        persisted, pinned by the no-leak test)."""
        plan = parse_plan(_WELL_FORMED)
        assert plan is not None
        section = render_plan_section(plan).lower()
        assert "private" in section
        assert "never" in section or "do not" in section

    def test_empty_optional_lists_render_cleanly(self):
        """An intent-only plan still renders — no dangling 'Key points:' with an
        empty value that would read as an instruction to say nothing."""
        plan = parse_plan("intent: name the unraised risk")
        assert plan is not None
        section = render_plan_section(plan)
        assert "name the unraised risk" in section
        assert section.strip() != ""
