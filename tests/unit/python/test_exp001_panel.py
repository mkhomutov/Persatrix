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
from evaluators.exp001.panel import PanelError, adviser_agent_config, load_panel

_PANEL = (
    Path(__file__).resolve().parents[3] / "evaluators" / "experiments" / "EXP-001" / "panel.yaml"
)


class TestTheShippedPanel:
    def test_four_advisers_in_file_order_with_one_chair(self):
        panel = load_panel(_PANEL)
        assert [a.id for a in panel.advisers] == [
            "lunar-stoat", "velvet-pika", "ripple-kite", "crimson-crow",
        ]
        assert panel.chair.id == "lunar-stoat"

    def test_the_advisers_answer_at_the_panel_temperature(self):
        assert load_panel(_PANEL).temperature == 0.7

    def test_each_meeting_kind_gets_its_arm_a_instruction(self):
        panel = load_panel(_PANEL)
        assert panel.memo_format.startswith("Write a decision memo for the person who asked.")
        assert panel.memo_format.endswith("describe how the advice\nwas reached.")
        assert panel.recall_format.endswith("say so rather than guess.")
        assert panel.arm_a_instruction(MeetingKind.BRIEFING) == "Reply briefly."
        assert panel.arm_a_instruction(MeetingKind.PLAN) == panel.memo_format
        assert panel.arm_a_instruction(MeetingKind.CONTROL) == panel.memo_format
        assert panel.arm_a_instruction(MeetingKind.RECALL) == panel.recall_format

    def test_the_arm_a_template_is_kept_word_for_word(self):
        template = load_panel(_PANEL).arm_a_template
        assert template.startswith("{current_time_line}\nYou are an advisory panel of four")
        assert template.endswith("then reply\nonce, as the panel.")


class TestAgentConfig:
    def test_it_holds_what_the_persona_runtime_reads(self):
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

    def test_each_call_returns_a_copy_the_caller_may_change(self):
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
    def test_a_panel_without_four_advisers(self, tmp_path, doc):
        del doc["advisers"][3]
        with pytest.raises(PanelError, match="3 advisers, expected 4"):
            load_panel(_write(tmp_path, doc))

    def test_a_panel_without_exactly_one_chair(self, tmp_path, doc):
        doc["advisers"][1]["duty"] = "chair"
        with pytest.raises(PanelError, match="2 chairs"):
            load_panel(_write(tmp_path, doc))

    def test_two_advisers_with_one_id(self, tmp_path, doc):
        doc["advisers"][2]["id"] = "velvet-pika"
        with pytest.raises(PanelError, match="velvet-pika.*twice"):
            load_panel(_write(tmp_path, doc))

    def test_an_id_no_agent_can_have(self, tmp_path, doc):
        doc["advisers"][0]["id"] = "Lunar Stoat"
        with pytest.raises(PanelError, match="'Lunar Stoat' is not an agent ID"):
            load_panel(_write(tmp_path, doc))

    def test_an_adviser_missing_a_field(self, tmp_path, doc):
        del doc["advisers"][2]["background"]
        with pytest.raises(PanelError, match="ripple-kite: no background"):
            load_panel(_write(tmp_path, doc))

    def test_a_field_the_pre_registration_does_not_know(self, tmp_path, doc):
        doc["advisers"][0]["quirks"] = ["Hums while reading"]
        with pytest.raises(PanelError, match="lunar-stoat: unknown field quirks"):
            load_panel(_write(tmp_path, doc))

    def test_an_arm_a_instruction_naming_an_unknown_text(self, tmp_path, doc):
        doc["instructions"]["arm_a_by_meeting"]["plan"] = "{memo}"
        with pytest.raises(PanelError, match="arm_a_by_meeting.plan.*'memo'"):
            load_panel(_write(tmp_path, doc))

    def test_an_arm_a_template_missing_a_placeholder(self, tmp_path, doc):
        template = doc["instructions"]["arm_a_system"]["template"]
        doc["instructions"]["arm_a_system"]["template"] = template.replace(
            "{organisation}", "the organisation",
        )
        with pytest.raises(PanelError, match="template lacks {organisation}"):
            load_panel(_write(tmp_path, doc))
