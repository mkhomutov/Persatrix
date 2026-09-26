"""EXP-001 harness — a local deployment of the advisers (PR 5a).

Arms B to D-prime hold each meeting in a channel whose members are the four
advisers, run as persona agents, and the operator. The harness writes each
deployment's config; ``test_exp001_processes`` covers its processes.
Pre-registration §3 asks four things of it before any scored meeting.
Check 1: in B, C and D′ nothing
from an earlier meeting reaches any prompt, so each of their meetings starts
on a new deployment with empty stores. Check 6: no call is refused by the
shipped spending limits, which the deployment turns off. Checks 7 and 8, for
the deployed advisers: each one's clock shows the meeting's date, and its
prompt holds the identity arm A shows, word for word.
"""

from __future__ import annotations

import copy
import datetime as dt
import re
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from agents.clock import CLOCK_ANCHOR_ENV, CLOCK_START_ENV, DEFAULT_TIMEZONE, reset_agent_clock
from agents.persona import create_persona_agent
from agents.persona_runtime.channel_roster import RosterMember, render_roster_section
from agents.prompt_loader import load_persona_section
from agents.temporal.rendering import format_now_anchor
from evaluators.exp001.arm_a import identity_sections
from evaluators.exp001.costs import ARMS_MODEL, PRICES
from evaluators.exp001.deployment import (
    ARMS_ALIAS,
    SHIPPED_AUTONOMY,
    SHIPPED_NOTES,
    SHIPPED_PERSONA,
    Alias,
    DeploymentError,
    Layout,
    adviser_config,
    channel_config,
    channels_config,
    optimization_config,
    write_deployment,
)
from evaluators.exp001.materials import load_series
from evaluators.exp001.panel import adviser_agent_config, load_panel
from evaluators.exp001.runtime import meeting_clock_env, story_start

from ._persona_test_helpers import _make_client

_REPO = Path(__file__).resolve().parents[3]
_EXP = _REPO / "evaluators" / "experiments" / "EXP-001"
PANEL = load_panel(_EXP / "panel.yaml")
_SHIPPED = _REPO / "config"


def _shipped(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((_SHIPPED / name).read_text())
    return loaded


class TestTheAdvisersConfig:
    def test_an_adviser_is_its_panel_config_with_the_deployment_settings(self) -> None:
        adviser = PANEL.advisers[1]
        entry = adviser_config(PANEL, adviser, memory_db=Path("/deploy/memory/velvet-pika.db"))
        assert entry == {
            **adviser_agent_config(adviser),
            "model": "quality",
            "temperature": 0.7,
            "tools": [],
            "max_retries": 2,
            "timeout_seconds": 300,
            "permissions": {"memory": {"read": True, "write": True}},
            "autonomy": {
                "max_actions_per_tick": 1, "idle_after_ticks": 5,
                "level": "reactive", "timers": [],
            },
            "memory": {
                "db_path": "/deploy/memory/velvet-pika.db",
                "notes": {
                    "enabled": True, "max_notes": 500, "auto_reflect_after": 5,
                    "inject_recent_notes": 3,
                },
            },
            "conversation_window": {"enabled": True, "max_turns": 200, "max_tokens": 32000},
            "relationships": [],
        }

    def test_nothing_is_added_under_persona_and_no_reply_limit_is_set(self) -> None:
        """PR 4 leaves this to PR 5: arm A renders exactly what sits under
        ``persona``, and a timezone or quirks would change the prompt."""
        for adviser in PANEL.advisers:
            entry = adviser_config(PANEL, adviser, memory_db=Path("/m.db"))
            assert entry["persona"] == adviser_agent_config(adviser)["persona"]
            assert "max_tokens" not in entry
            assert "timezone" not in entry["persona"]

    def test_what_the_panel_leaves_unset_is_what_the_shipped_personas_use(self) -> None:
        reactive = [
            agent for agent in _shipped("agents.yaml")["agents"]
            if agent["type"] == "persona" and agent["autonomy"]["level"] == "reactive"
        ]
        assert len(reactive) == 2
        for agent in reactive:
            assert {key: agent[key] for key in SHIPPED_PERSONA} == SHIPPED_PERSONA
            assert {key: agent["autonomy"][key] for key in SHIPPED_AUTONOMY} == SHIPPED_AUTONOMY
            assert agent["memory"]["notes"] == SHIPPED_NOTES


class TestOptimizationConfig:
    def test_every_alias_is_the_arms_model_at_the_pre_registered_price(self) -> None:
        config = optimization_config(_shipped("optimization.yaml"), memory_budget_tokens=0)
        entry = {
            "provider": "anthropic", "model": ARMS_MODEL,
            "input_per_1m_tokens": 3.00, "output_per_1m_tokens": 15.00,
        }
        aliases = ("quality", "fast", "summarizer")
        assert config["models"]["aliases"] == dict.fromkeys(aliases, entry)
        assert config["cost"]["pricing"]["models"] == {
            ARMS_MODEL: {"input_per_1m_tokens": 3.00, "output_per_1m_tokens": 15.00},
        }
        assert (ARMS_ALIAS.input_per_1m_tokens, ARMS_ALIAS.output_per_1m_tokens) == (
            PRICES[ARMS_MODEL].input, PRICES[ARMS_MODEL].output,
        )

    def test_a_trial_deployment_may_point_every_alias_elsewhere(self) -> None:
        """The offline mock provider serves the harness's own end-to-end test
        at no cost; the run itself always takes the default."""
        mock = Alias("mock", "offline", 0, 0)
        config = optimization_config(
            _shipped("optimization.yaml"), memory_budget_tokens=0, alias=mock,
        )
        assert {a["provider"] for a in config["models"]["aliases"].values()} == {"mock"}
        assert config["cost"]["pricing"]["models"] == {
            "offline": {"input_per_1m_tokens": 0, "output_per_1m_tokens": 0},
        }

    def test_the_spending_limits_are_off(self) -> None:
        """Check 6: each shipped limit is off at 0; how an exceeded one would
        act, and when it would warn, are left as shipped."""
        budgets = optimization_config(
            _shipped("optimization.yaml"), memory_budget_tokens=0,
        )["cost"]["budgets"]
        assert budgets == {
            "global": {
                "max_daily_usd": 0, "alert_at_percent": [50, 80, 95],
                "on_exceed": "pause_and_alert",
            },
            "per_workflow": {"default_max_usd": 0},
            "per_agent": {"default_max_usd": 0},
        }

    @pytest.mark.parametrize("tokens", [0, 1500])
    def test_the_arms_memory_budget_is_set(self, tokens: int) -> None:
        config = optimization_config(_shipped("optimization.yaml"), memory_budget_tokens=tokens)
        assert config["memory_budget"] == {"tokens": tokens}

    def test_everything_else_is_as_shipped(self) -> None:
        shipped = _shipped("optimization.yaml")
        config = optimization_config(copy.deepcopy(shipped), memory_budget_tokens=0)
        del config["memory_budget"]
        config["models"]["aliases"] = shipped["models"]["aliases"]
        config["cost"]["pricing"]["models"] = shipped["cost"]["pricing"]["models"]
        config["cost"]["budgets"] = shipped["cost"]["budgets"]
        assert config == shipped

    def test_the_shipped_config_is_left_alone(self) -> None:
        shipped = _shipped("optimization.yaml")
        before = copy.deepcopy(shipped)
        optimization_config(shipped, memory_budget_tokens=0)
        assert shipped == before


class TestChannelConfig:
    def test_a_governed_arm_meets_in_a_channel_shaped_like_the_shipped_roundtable(self) -> None:
        assert channel_config(PANEL, "C", name="advice-2", organisation="Linden Loaf") == {
            "name": "advice-2",
            "description": "Linden Loaf",
            "classification": "internal",
            "interaction_budget_tokens": 2_000_000,
            "escalation_chair_id": "lunar-stoat",
            "max_cascade_depth": 5,
            "end_vote_threshold": 4,
            "end_vote_window": 8,
            "reasoning": {"mode": "bid"},
            "autonomous": {
                "enabled": True,
                "topic": "Advice for Linden Loaf",
                "agenda": [],
                "convener": "crimson-crow",
                "goal": "Answer the operator's latest message.",
                "max_rounds": 8,
            },
            "members": [
                {"id": "lunar-stoat", "respond": "chair"},
                {"id": "velvet-pika", "respond": "participant"},
                {"id": "ripple-kite", "respond": "participant"},
                {"id": "crimson-crow", "respond": "participant"},
                {"id": "operator", "respond": "observer"},
            ],
        }

    def test_arm_b_meets_with_governance_off(self) -> None:
        entry = channel_config(PANEL, "B", name="advice-2", organisation="Linden Loaf")
        assert entry["members"] == [
            {"id": a.id, "respond": "always"} for a in PANEL.advisers
        ] + [{"id": "operator", "respond": "observer"}]
        assert (entry["reasoning"], entry["end_vote_threshold"], entry["end_vote_window"]) == (
            {"mode": "off"}, 9, 8,
        )

    def test_the_fleet_settings_stay_as_shipped_and_only_these_channels_are_declared(self) -> None:
        shipped = _shipped("channels.yaml")
        entry = channel_config(PANEL, "C", name="advice-1", organisation="Linden Loaf")
        config = channels_config(shipped, [entry])
        assert config["channels"] == [entry]
        assert {k: v for k, v in config.items() if k != "channels"} == {
            k: v for k, v in shipped.items() if k != "channels"
        }
        assert config["max_channels"] == 50


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return Layout(tmp_path / "deployment")


def _channel(arm: str = "C") -> dict[str, Any]:
    return channel_config(PANEL, arm, name="advice-1", organisation="Linden Loaf")


def _clock_line_at(epoch: float) -> str:
    """The now-anchor line an adviser on UTC shows at agent time *epoch*.

    The renderer truncates to the second, so any instant within a second
    reads as that second."""
    anchor = format_now_anchor(epoch, DEFAULT_TIMEZONE)
    return load_persona_section("now-anchor").format_map({"now_anchor": anchor})


class TestWriteDeployment:
    def test_it_writes_the_config_every_process_reads(self, layout: Layout) -> None:
        write_deployment(layout, PANEL, "B", channels=[_channel("B")])
        written = {p.name for p in layout.config.iterdir()}
        assert written == {
            "agents.yaml", "optimization.yaml", "channels.yaml", "security.yaml", "ui.yaml",
        }
        agents = yaml.safe_load((layout.config / "agents.yaml").read_text())
        assert agents == {
            "schema_version": _shipped("agents.yaml")["schema_version"],
            "agents": [
                adviser_config(PANEL, a, memory_db=layout.memory_db(a.id)) for a in PANEL.advisers
            ],
        }
        optimization = yaml.safe_load((layout.config / "optimization.yaml").read_text())
        assert optimization["memory_budget"] == {"tokens": 0}
        channels = yaml.safe_load((layout.config / "channels.yaml").read_text())
        assert channels["channels"] == [_channel("B")]
        for name in ("security.yaml", "ui.yaml"):
            assert (layout.config / name).read_text() == (_SHIPPED / name).read_text()

    def test_each_store_starts_empty(self, layout: Layout) -> None:
        """Check 1: a deployment is written into an empty directory only, and
        every store it names lies inside it."""
        write_deployment(layout, PANEL, "C", channels=[_channel()])
        for directory in (layout.data, layout.memory, layout.workflows, layout.workspace):
            assert list(directory.iterdir()) == []
        for adviser in PANEL.advisers:
            assert layout.memory_db(adviser.id).parent == layout.memory

    def test_a_directory_that_already_holds_anything_is_refused(self, layout: Layout) -> None:
        layout.memory.mkdir(parents=True)
        layout.memory_db("lunar-stoat").write_text("an earlier meeting")
        with pytest.raises(DeploymentError, match="is not empty"):
            write_deployment(layout, PANEL, "C", channels=[_channel()])

    def test_config_the_schemas_refuse_is_refused_before_anything_starts(
        self, layout: Layout,
    ) -> None:
        bad = {**_channel(), "end_vote_window": "eight"}
        with pytest.raises(DeploymentError, match="channels.yaml.*end_vote_window"):
            write_deployment(layout, PANEL, "C", channels=[bad])


class TestTheDeployedAdvisersReadAsArmADoes:
    """Checks 7 and 8 for the channel arms: each adviser, built as a persona
    agent from the entry the deployment wrote, on the meeting's clock."""

    async def test_every_identity_section_and_the_clock_line_arm_a_shows(
        self, layout: Layout, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_deployment(layout, PANEL, "C", channels=[_channel()])
        entries = yaml.safe_load((layout.config / "agents.yaml").read_text())["agents"]
        story_date = dt.date(2036, 10, 13)
        began = dt.datetime.now(dt.UTC)
        for key, value in meeting_clock_env(story_date, began).items():
            monkeypatch.setenv(key, value)
        reset_agent_clock()
        try:
            for adviser, entry in zip(PANEL.advisers, entries, strict=True):
                # Real time either side of everything that builds the agent
                # and its prompt, so the bracket holds wherever the agent
                # reads its clock along the way (see below).
                before = time.time()
                agent = create_persona_agent(
                    agent_id=entry["id"], config=entry, llm_client=_make_client(),
                )
                await agent.initialize_memory()
                try:
                    prompt = agent._build_system_prompt()
                    after = time.time()
                finally:
                    await agent.close_memory()
                sections = identity_sections(adviser)
                where = [prompt.index(section) for section in sections]
                assert where == sorted(where)
                # The deployment gives the adviser the meeting's clock
                # through the environment: it starts at 10:00 on the story
                # date and runs on with real time from ``began``, so the
                # instant its prompt shows is its real reading plus
                # ``offset``.  Both come from this test's own values — the
                # story start it asked for and real readings — so the window
                # still says the clock is the *meeting's*, in 2036, and not
                # whatever clock the agent happened to find.  The renderer
                # truncates to the second, so one whole second in the window
                # is the line the prompt shows.
                offset = story_start(story_date).timestamp() - began.timestamp()
                first, last = int(before + offset), int(after + offset)
                shown = [_clock_line_at(second) for second in range(first, last + 1)]
                assert any(line in prompt for line in shown), (
                    f"expected {entry['id']}'s prompt to show the meeting's "
                    f"clock somewhere between {_clock_line_at(first)!r} "
                    f"and {_clock_line_at(last)!r}"
                )
        finally:
            monkeypatch.delenv(CLOCK_START_ENV)
            monkeypatch.delenv(CLOCK_ANCHOR_ENV)
            reset_agent_clock()

    def test_every_adviser_reads_the_organisation_in_its_room(self, layout: Layout) -> None:
        """§2: every arm gets the organisation's one-line description, and arm
        A's prompt names it. An adviser's prompt shows its channel's name and
        description; the topic reaches only a convene, agenda or synthesis turn."""
        organisation = load_series(_EXP / "series-1.yaml").organisation
        entry = channel_config(PANEL, "C", name="advice-2", organisation=organisation)
        write_deployment(layout, PANEL, "C", channels=[entry])
        [written] = yaml.safe_load((layout.config / "channels.yaml").read_text())["channels"]
        # What GET /api/v1/channels/{id} serves from the declaration, as the roster reads it.
        meta = {"name": written["name"], "description": written["description"]}
        me = RosterMember(id="lunar-stoat", name="Lunar Stoat", role="Chair", is_self=True)
        section = render_roster_section(meta, [me])
        assert section is not None
        assert section.content.startswith(f"Channel #advice-2 — {organisation}\n")
        # The orchestrator carries the declared description through to that read.
        for go, carried in [
            ("internal/channels/config.go", r'Description\s+string\s+`yaml:"description"`'),
            ("internal/channels/router_reconcile.go", r"Description:\s+decl\.Description,"),
            ("internal/server/channel_response_builders.go", r"Description:\s+ch\.Description,"),
        ]:
            assert re.search(carried, (_REPO / go).read_text()), go
