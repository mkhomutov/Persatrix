"""EXP-001 harness — reading the panel (PR 4).

``panel.yaml`` fixes the four advisers every arm uses and the wording of every
instruction the harness adds. Arm A renders the advisers from the same agent
configs the channel arms will deploy, so both read one source, and a panel the
run could not use is refused before any meeting starts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from evaluators.exp001.materials import MeetingKind
from evaluators.exp001.panel import (
    PanelError,
    adviser_agent_config,
    fill_placeholders,
    load_panel,
)

_PANEL = (
    Path(__file__).resolve().parents[3] / "evaluators" / "experiments" / "EXP-001" / "panel.yaml"
)


class TestTheShippedPanel:
    def test_four_advisers_in_file_order_with_one_chair(self) -> None:
        panel = load_panel(_PANEL)
        assert [a.id for a in panel.advisers] == [
            "lunar-stoat", "velvet-pika", "ripple-kite", "crimson-crow",
        ]
        assert panel.chair.id == "lunar-stoat"

    def test_the_advisers_answer_at_the_panel_temperature(self) -> None:
        assert load_panel(_PANEL).temperature == 0.7

    def test_a_loaded_panel_cannot_be_changed_and_hashes(self) -> None:
        panel = load_panel(_PANEL)
        adviser = panel.advisers[0]
        with pytest.raises(TypeError):
            adviser.goals["primary"] = "changed"  # type: ignore[index]
        with pytest.raises(TypeError):
            panel.arm_a_by_meeting["plan"] = "changed"  # type: ignore[index]
        assert {adviser: adviser.id}[adviser] == "lunar-stoat"

    def test_each_meeting_kind_gets_its_arm_a_instruction(self) -> None:
        panel = load_panel(_PANEL)
        assert panel.memo_format.startswith("Write a decision memo for the person who asked.")
        assert panel.memo_format.endswith("describe how the advice\nwas reached.")
        assert panel.recall_format.endswith("say so rather than guess.")
        assert panel.arm_a_instruction(MeetingKind.BRIEFING) == "Reply briefly."
        assert panel.arm_a_instruction(MeetingKind.PLAN) == panel.memo_format
        assert panel.arm_a_instruction(MeetingKind.CONTROL) == panel.memo_format
        assert panel.arm_a_instruction(MeetingKind.RECALL) == panel.recall_format

    def test_the_arm_a_template_is_kept_word_for_word(self) -> None:
        template = load_panel(_PANEL).arm_a_template
        assert template.startswith("{current_time_line}\nYou are an advisory panel of four")
        assert template.endswith("then reply\nonce, as the panel.")


class TestTheChannelArms:
    """Arms B to D-prime run the advisers as persona agents in a channel;
    panel.yaml fixes that channel, their settings and the memo turn after it."""

    def test_governance_is_off_in_b_and_as_shipped_in_the_others(self) -> None:
        panel = load_panel(_PANEL)
        b = panel.channels["B"]
        assert dict(b.members) == dict.fromkeys((a.id for a in panel.advisers), "always")
        assert (b.reasoning_mode, b.end_vote_threshold, b.end_vote_window) == ("off", 9, 8)
        for arm in ("C", "D", "D-prime"):
            governed = panel.channels[arm]
            assert dict(governed.members) == {
                "lunar-stoat": "chair", "velvet-pika": "participant",
                "ripple-kite": "participant", "crimson-crow": "participant",
            }
            assert (
                governed.reasoning_mode, governed.end_vote_threshold, governed.end_vote_window,
            ) == ("bid", 4, 8)

    def test_every_channel_arm_shares_the_rest(self) -> None:
        panel = load_panel(_PANEL)
        assert sorted(panel.channels) == ["B", "C", "D", "D-prime"]
        for channel in panel.channels.values():
            assert (
                channel.topic, channel.goal, channel.agenda, channel.max_rounds,
                channel.interaction_budget_tokens, channel.escalation_chair_id,
                channel.convener, channel.classification, channel.cascade_depth_cap,
            ) == (
                "Advice for {organisation}", "Answer the operator's latest message.", (), 8,
                2_000_000, "lunar-stoat", "crimson-crow", "internal", 5,
            )

    def test_only_d_recalls_memory_into_its_prompts(self) -> None:
        assert dict(load_panel(_PANEL).memory_budget_tokens) == {
            "B": 0, "C": 0, "D": 1500, "D-prime": 0,
        }

    def test_the_settings_the_advisers_are_deployed_with(self) -> None:
        assert load_panel(_PANEL).persona_settings == {
            "permissions": {"memory": {"read": True, "write": True}},
            "relationships": [],
            "autonomy": {"level": "reactive", "timers": []},
            "conversation_window": {"enabled": True, "max_turns": 200, "max_tokens": 32000},
        }

    def test_the_operator_is_a_member_who_never_answers(self) -> None:
        assert load_panel(_PANEL).operator == "operator"

    def test_the_memo_turn_asks_the_chair_for_the_memo_or_the_answers(self) -> None:
        panel = load_panel(_PANEL)
        plan = panel.memo_turn_instruction(MeetingKind.PLAN)
        assert plan == (
            "The discussion has ended. As chair, write the panel's decision memo from\n"
            f"what was said.\n{panel.memo_format}"
        )
        assert panel.memo_turn_instruction(MeetingKind.CONTROL) == plan
        assert panel.memo_turn_instruction(MeetingKind.RECALL) == (
            f"The discussion has ended. As chair, give the panel's answers.\n{panel.recall_format}"
        )
        with pytest.raises(ValueError, match="a briefing has no memo turn"):
            panel.memo_turn_instruction(MeetingKind.BRIEFING)

    def test_the_channel_settings_cannot_be_changed(self) -> None:
        panel = load_panel(_PANEL)
        with pytest.raises(TypeError):
            panel.channels["B"].members["lunar-stoat"] = "observer"  # type: ignore[index]
        with pytest.raises(TypeError):
            panel.persona_settings["relationships"] = ["x"]  # type: ignore[index]


class TestPlaceholders:
    """rubric.yaml's templating note: a placeholder is filled by replacing its
    exact string, and every other brace in the materials is literal text."""

    def test_every_other_brace_is_literal_text(self, tmp_path: Path, doc: dict[str, Any]) -> None:
        text = 'Reply {{briefly}}, as {"R1": "right"}, not {memo} or {organisation!r}.'
        doc["instructions"]["arm_a_by_meeting"]["briefing"] = text
        assert load_panel(_write(tmp_path, doc)).arm_a_instruction(MeetingKind.BRIEFING) == text

    def test_a_filled_value_is_never_filled_again(
        self, tmp_path: Path, doc: dict[str, Any],
    ) -> None:
        doc["instructions"]["memo_format"] = "Quote {recall_format} as written."
        panel = load_panel(_write(tmp_path, doc))
        assert panel.arm_a_instruction(MeetingKind.PLAN) == "Quote {recall_format} as written."

    def test_one_pass_fills_each_named_placeholder(self) -> None:
        assert fill_placeholders(
            "{organisation}: {current_time_line} {x}", {
                "organisation": "Linden Loaf {current_time_line}", "current_time_line": "now",
            },
        ) == "Linden Loaf {current_time_line}: now {x}"


class TestAgentConfig:
    def test_it_holds_what_the_persona_runtime_reads(self) -> None:
        adviser = load_panel(_PANEL).advisers[1]
        assert adviser_agent_config(adviser) == {
            "id": "velvet-pika",
            "type": "persona",
            "name": "Velvet Pika",
            "role": "Checks the money side of every plan",
            "persona": {
                "title": "Finance adviser",
                "background": (
                    "Checks whether the numbers add up: costs, cash, borrowing, fees, "
                    "payback and anything the budget leaves out."
                ),
                "behavior": {
                    "directness": "direct",
                    "detail_focus": "detail-focused",
                    "formality": "professional",
                    "risk_tolerance": "cautious",
                    "expressiveness": "reserved",
                },
                "goals": {
                    "primary": (
                        "Make sure the organisation can afford the decision "
                        "and the figures are right"
                    ),
                },
                "knowledge": {
                    "domains": ["budgets", "cash flow", "contracts and fees", "payback"],
                },
            },
        }

    def test_each_call_returns_a_copy_the_caller_may_change(self) -> None:
        adviser = load_panel(_PANEL).advisers[0]
        adviser_agent_config(adviser)["persona"]["goals"]["primary"] = "changed"
        assert adviser_agent_config(adviser)["persona"]["goals"]["primary"] != "changed"


def _write(tmp_path: Path, doc: dict[str, Any]) -> Path:
    path = tmp_path / "panel.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


@pytest.fixture
def doc() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(_PANEL.read_text())
    return loaded


class TestRefusals:
    def test_a_panel_without_four_advisers(self, tmp_path: Path, doc: dict[str, Any]) -> None:
        del doc["advisers"][3]
        with pytest.raises(PanelError, match="3 advisers, expected 4"):
            load_panel(_write(tmp_path, doc))

    def test_a_panel_without_exactly_one_chair(self, tmp_path: Path, doc: dict[str, Any]) -> None:
        doc["advisers"][1]["duty"] = "chair"
        with pytest.raises(PanelError, match="2 chairs"):
            load_panel(_write(tmp_path, doc))

    def test_two_advisers_with_one_id(self, tmp_path: Path, doc: dict[str, Any]) -> None:
        doc["advisers"][2]["id"] = "velvet-pika"
        with pytest.raises(PanelError, match="velvet-pika.*twice"):
            load_panel(_write(tmp_path, doc))

    @pytest.mark.parametrize("aid", ["Lunar Stoat", "lunar-stoat\n", "pool-stoat", 7])
    def test_an_id_no_agent_can_have(
        self, tmp_path: Path, doc: dict[str, Any], aid: object,
    ) -> None:
        doc["advisers"][0]["id"] = aid
        with pytest.raises(PanelError, match="is not an agent ID"):
            load_panel(_write(tmp_path, doc))

    def test_advisers_that_are_not_a_list(self, tmp_path: Path, doc: dict[str, Any]) -> None:
        doc["advisers"] = None
        with pytest.raises(PanelError, match="advisers is None, not a list"):
            load_panel(_write(tmp_path, doc))

    def test_an_adviser_that_is_not_a_mapping(self, tmp_path: Path, doc: dict[str, Any]) -> None:
        doc["advisers"][0] = "lunar-stoat"
        with pytest.raises(PanelError, match="an adviser is 'lunar-stoat', not a mapping"):
            load_panel(_write(tmp_path, doc))

    def test_an_adviser_missing_a_field(self, tmp_path: Path, doc: dict[str, Any]) -> None:
        del doc["advisers"][2]["background"]
        with pytest.raises(PanelError, match="ripple-kite: no background"):
            load_panel(_write(tmp_path, doc))

    def test_a_field_the_pre_registration_does_not_know(
        self, tmp_path: Path, doc: dict[str, Any],
    ) -> None:
        doc["advisers"][0]["quirks"] = ["Hums while reading"]
        with pytest.raises(PanelError, match="lunar-stoat: unknown field quirks"):
            load_panel(_write(tmp_path, doc))

    @pytest.mark.parametrize(("field", "value", "error"), [
        ("goals", {"primary": "x", "secondary": "Keep costs down"}, "goals.secondary"),
        ("goals", {"primary": ""}, "goals name no goal"),
        ("goals", {"primery": "x"}, "goals: .*'primery' was unexpected"),
        ("goals", ["ab", "cd"], "goals: .* is not of type 'object'"),
        ("behavior", {"directness": "blunt"}, "behavior.directness: 'blunt' is not one of"),
        ("behavior", ["direct"], "behavior: .* is not of type 'object'"),
        ("title", True, "title: True is not of type 'string'"),
        ("background", ["one", "two"], "background: .* is not of type 'string'"),
        ("name", 7, "name is 7, not text"),
    ])
    def test_an_adviser_value_the_persona_agents_could_not_render(
        self, tmp_path: Path, doc: dict[str, Any], field: str, value: object, error: str,
    ) -> None:
        doc["advisers"][2][field] = value
        with pytest.raises(PanelError, match=f"ripple-kite: {error}"):
            load_panel(_write(tmp_path, doc))

    def test_a_key_given_twice(self, tmp_path: Path) -> None:
        path = tmp_path / "panel.yaml"
        path.write_text(_PANEL.read_text().replace(
            "  temperature: 0.7\n", "  temperature: 0.7\n  temperature: 0.2\n",
        ))
        with pytest.raises(PanelError, match="temperature is given twice"):
            load_panel(path)

    @pytest.mark.parametrize("path", [
        ("instructions", "recall_format"),
        ("instructions", "arm_a_by_meeting", "recall"),
        ("persona_settings",),
        ("persona_settings", "conversation_window"),
        ("instructions", "memo_turn", "recall"),
        ("channel", "every_channel_arm", "goal"),
        ("memory", "D-prime", "memory_budget_tokens"),
        ("operator", "respond"),
    ])
    def test_a_missing_part(
        self, tmp_path: Path, doc: dict[str, Any], path: tuple[str, ...],
    ) -> None:
        parent = doc
        for key in path[:-1]:
            parent = parent[key]
        del parent[path[-1]]
        with pytest.raises(PanelError, match=f"panel.yaml has no {'.'.join(path)}$"):
            load_panel(_write(tmp_path, doc))

    def test_an_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "panel.yaml"
        path.write_text("")
        with pytest.raises(PanelError, match="panel.yaml has no advisers"):
            load_panel(path)

    @pytest.mark.parametrize("temperature", [True, "0.7", 7, -0.1, float("nan")])
    def test_a_temperature_the_provider_would_not_take(
        self, tmp_path: Path, doc: dict[str, Any], temperature: object,
    ) -> None:
        doc["persona_settings"]["temperature"] = temperature
        with pytest.raises(PanelError, match="persona_settings.temperature is .*, not a number"):
            load_panel(_write(tmp_path, doc))

    @pytest.mark.parametrize("text", ["   ", ["Write a memo."]])
    def test_an_instruction_that_is_not_text(
        self, tmp_path: Path, doc: dict[str, Any], text: object,
    ) -> None:
        doc["instructions"]["memo_format"] = text
        with pytest.raises(PanelError, match="instructions.memo_format is .*, not text"):
            load_panel(_write(tmp_path, doc))

    def test_an_arm_a_instruction_naming_a_placeholder_it_cannot_fill(
        self, tmp_path: Path, doc: dict[str, Any],
    ) -> None:
        doc["instructions"]["arm_a_by_meeting"]["plan"] = "For {organisation}: {memo_format}"
        with pytest.raises(PanelError, match="arm_a_by_meeting.plan names {organisation}"):
            load_panel(_write(tmp_path, doc))

    def test_an_arm_a_instruction_no_meeting_reads(
        self, tmp_path: Path, doc: dict[str, Any],
    ) -> None:
        doc["instructions"]["arm_a_by_meeting"]["control"] = "Write a control memo."
        with pytest.raises(PanelError, match="arm_a_by_meeting has control, which no meeting"):
            load_panel(_write(tmp_path, doc))

    def test_an_arm_a_template_missing_a_placeholder(
        self, tmp_path: Path, doc: dict[str, Any],
    ) -> None:
        template = doc["instructions"]["arm_a_system"]["template"]
        doc["instructions"]["arm_a_system"]["template"] = template.replace(
            "{organisation}", "the organisation",
        )
        with pytest.raises(PanelError, match="template lacks {organisation}"):
            load_panel(_write(tmp_path, doc))

    def test_an_arm_a_template_naming_a_placeholder_it_never_fills(
        self, tmp_path: Path, doc: dict[str, Any],
    ) -> None:
        doc["instructions"]["arm_a_system"]["template"] += "\n{memo_format}"
        with pytest.raises(PanelError, match="template names {memo_format}, which the harness"):
            load_panel(_write(tmp_path, doc))


class TestChannelRefusals:
    """A channel arm the panel leaves unset, or sets so its meetings could not
    run as the pre-registration describes, is refused before any meeting."""

    def test_a_channel_arm_no_block_holds(self, tmp_path: Path, doc: dict[str, Any]) -> None:
        doc["channel"]["governance_on"]["arms"] = ["C", "D"]
        with pytest.raises(PanelError, match="no channel block holds arm D-prime"):
            load_panel(_write(tmp_path, doc))

    def test_an_arm_two_blocks_hold(self, tmp_path: Path, doc: dict[str, Any]) -> None:
        doc["channel"]["governance_off"]["arms"] = ["B", "C"]
        with pytest.raises(PanelError, match="arm C is in two channel blocks"):
            load_panel(_write(tmp_path, doc))

    def test_arm_a_in_a_channel(self, tmp_path: Path, doc: dict[str, Any]) -> None:
        doc["channel"]["governance_off"]["arms"] = ["A", "B"]
        with pytest.raises(PanelError, match="governance_off names arm 'A', which meets in no"):
            load_panel(_write(tmp_path, doc))

    def test_members_other_than_the_advisers(self, tmp_path: Path, doc: dict[str, Any]) -> None:
        del doc["channel"]["governance_on"]["members"]["crimson-crow"]
        with pytest.raises(PanelError, match="governance_on members are not the four advisers"):
            load_panel(_write(tmp_path, doc))

    def test_a_disposition_no_channel_knows(self, tmp_path: Path, doc: dict[str, Any]) -> None:
        doc["channel"]["governance_on"]["members"]["velvet-pika"] = "sometimes"
        with pytest.raises(PanelError, match="velvet-pika is 'sometimes', not a disposition"):
            load_panel(_write(tmp_path, doc))

    def test_a_closing_chair_other_than_the_panels(
        self, tmp_path: Path, doc: dict[str, Any],
    ) -> None:
        doc["channel"]["every_channel_arm"]["escalation_chair_id"] = "velvet-pika"
        with pytest.raises(PanelError, match="escalation_chair_id is 'velvet-pika', not lunar-"):
            load_panel(_write(tmp_path, doc))

    @pytest.mark.parametrize("convener", ["lunar-stoat", "operator"])
    def test_a_convener_that_is_the_chair_or_no_adviser(
        self, tmp_path: Path, doc: dict[str, Any], convener: str,
    ) -> None:
        doc["channel"]["every_channel_arm"]["convener"] = convener
        with pytest.raises(PanelError, match="convener .* an adviser other than the chair"):
            load_panel(_write(tmp_path, doc))

    @pytest.mark.parametrize("autonomous", [False, "true", None])
    def test_a_channel_that_is_not_armed(
        self, tmp_path: Path, doc: dict[str, Any], autonomous: object,
    ) -> None:
        doc["channel"]["every_channel_arm"]["autonomous"] = autonomous
        with pytest.raises(PanelError, match="every_channel_arm.autonomous is .*, not true"):
            load_panel(_write(tmp_path, doc))

    @pytest.mark.parametrize(("key", "value"), [
        ("max_rounds", 0), ("interaction_budget_tokens", True), ("cascade_depth_cap", "5"),
    ])
    def test_a_bound_that_is_not_a_positive_count(
        self, tmp_path: Path, doc: dict[str, Any], key: str, value: object,
    ) -> None:
        doc["channel"]["every_channel_arm"][key] = value
        with pytest.raises(PanelError, match=f"every_channel_arm.{key} is .*, not a positive"):
            load_panel(_write(tmp_path, doc))

    @pytest.mark.parametrize("tokens", [True, "1500", -1, 1.5])
    def test_a_memory_budget_that_is_not_a_count(
        self, tmp_path: Path, doc: dict[str, Any], tokens: object,
    ) -> None:
        doc["memory"]["D"]["memory_budget_tokens"] = tokens
        with pytest.raises(PanelError, match="memory.D.memory_budget_tokens is .*, not a count"):
            load_panel(_write(tmp_path, doc))

    @pytest.mark.parametrize(("respond", "operator", "error"), [
        ("observer", "velvet-pika", "operator velvet-pika is an adviser"),
        ("observer", "The Operator", "'The Operator' is not an agent ID"),
        ("always", "operator", "the operator responds 'always', not as an observer"),
    ])
    def test_an_operator_who_could_take_part(
        self, tmp_path: Path, doc: dict[str, Any], respond: str, operator: str, error: str,
    ) -> None:
        doc["operator"].update(id=operator, respond=respond)
        with pytest.raises(PanelError, match=error):
            load_panel(_write(tmp_path, doc))

    def test_a_memo_turn_naming_a_placeholder_it_cannot_fill(
        self, tmp_path: Path, doc: dict[str, Any],
    ) -> None:
        doc["instructions"]["memo_turn"]["plan"] += "For {organisation}."
        with pytest.raises(PanelError, match="memo_turn.plan names {organisation}"):
            load_panel(_write(tmp_path, doc))
