"""Which permission names a tool may declare, and what the gate answers (ISSUE-0214).

An agent's config can grant seven permissions: the five categories of the
agent schema's ``permissions`` block, each with the keys that turn a tool
on. ``@tool`` refuses to register a tool that declares any other name,
because no config could grant it and the tool would never run. The gate
answers "denied" for any other name too, whatever key of that name the
config holds: before, ``network:deny`` read as granted wherever a deny list
was set.

The last class holds the seven names to ``schemas/agent.schema.json``, so a
key added to the schema has to be classified here before it can pass.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import agents.tools.builtin as builtin
from agents.memory.episodic import EpisodicMemory
from agents.tools.memory_tools import create_memory_tools
from agents.tools.permissions import (
    GRANTABLE_PERMISSIONS,
    PERMISSION_CATEGORIES,
    PermissionGate,
)
from agents.tools.recall import create_recall_tool
from agents.tools.registry import ToolResult, clear_registry, get_tool, list_tools, tool

_SCHEMA = Path(__file__).resolve().parents[3] / "schemas" / "agent.schema.json"

_SEVEN = [
    "channels:recall", "filesystem:read", "filesystem:write", "memory:read",
    "memory:write", "network:http", "shell:exec",
]

# Every key of every category is set, so a gate that looked a name's key up
# in the config would grant each of the names below.
_EVERY_KEY: dict[str, Any] = {
    "filesystem": {"read": ["/w/**"], "write": ["/w/**"], "deny": ["**/.env*"]},
    "network": {"allow": ["example.com"], "deny": ["*"]},
    "shell": {"allowed_commands": ["ls"], "max_execution_seconds": 30},
    "memory": {"read": True, "write": True},
    "channels": {"recall": True},
    "db": {"read": True},
}

# Outside the five categories: a permission of the tool's own.
_OF_ITS_OWN = [
    "db:read", "fs:write", "github:pull_requests", "Filesystem:read", ":read", "read", "",
]
# Inside one of the five, where no key of the agent schema grants the action.
_NO_SUCH_ACTION = [
    "filesystem:deny", "filesystem:delete", "network:allow", "network:deny",
    "shell:allowed_commands", "shell:max_execution_seconds", "memory:purge",
    "channels:post", "filesystem:", "filesystem:read ", "filesystem:read:all", "network",
]


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


def _declare(*permissions: Any, name: str = "probe") -> None:
    @tool(name=name, description="probe", permissions=list(permissions))
    async def _probe() -> ToolResult:
        return ToolResult(success=True, data="ran")


class TestWhatAToolMayDeclare:
    @pytest.mark.parametrize("permission", _SEVEN)
    def test_each_of_the_seven_registers(self, permission: str) -> None:
        _declare(permission)

        registered = get_tool("probe")
        assert registered is not None and registered.permissions == [permission]

    def test_the_seven_are_all_there_is(self) -> None:
        assert sorted(GRANTABLE_PERMISSIONS) == _SEVEN
        assert sorted(PERMISSION_CATEGORIES) == [
            "channels", "filesystem", "memory", "network", "shell",
        ]

    def test_a_tool_that_declares_nothing_registers(self) -> None:
        _declare()

        registered = get_tool("probe")
        assert registered is not None and registered.permissions == []

    @pytest.mark.parametrize("permission", _OF_ITS_OWN)
    def test_a_permission_of_the_tools_own_is_an_error(self, permission: str) -> None:
        with pytest.raises(ValueError) as refused:
            _declare(permission)

        message = str(refused.value)
        assert "'probe'" in message and repr(permission) in message
        assert "a tool cannot have a permission of its own yet" in message
        # The author is told what can be declared instead.
        for name in _SEVEN:
            assert name in message
        assert get_tool("probe") is None

    @pytest.mark.parametrize("permission", _NO_SUCH_ACTION)
    def test_an_action_no_config_key_grants_is_an_error(self, permission: str) -> None:
        with pytest.raises(ValueError) as refused:
            _declare(permission)

        message = str(refused.value)
        assert "'probe'" in message and repr(permission) in message
        assert "no agent's config can grant it" in message
        assert "of its own" not in message
        for name in _SEVEN:
            assert name in message
        assert get_tool("probe") is None

    def test_one_bad_name_among_good_ones_refuses_the_tool(self) -> None:
        with pytest.raises(ValueError, match="'db:read'"):
            _declare("filesystem:read", "db:read", "memory:read")

        assert get_tool("probe") is None

    @pytest.mark.parametrize(
        "permissions", ["filesystem:read", [7], [None], [["filesystem:read"]]],
        ids=["bare-text", "number", "none", "nested-list"],
    )
    def test_a_list_that_is_not_a_list_of_names_is_an_error(self, permissions: Any) -> None:
        with pytest.raises(ValueError, match="must be a list of permission names"):
            @tool(name="probe", permissions=permissions)
            async def _probe() -> ToolResult:
                return ToolResult(success=True)

        assert get_tool("probe") is None

    @pytest.mark.parametrize(
        "shape",
        [
            lambda names: (name for name in names), iter, lambda names: map(str, names),
            tuple, dict.fromkeys, lambda names: dict.fromkeys(names).keys(),
        ],
        ids=["generator", "iter", "map", "tuple", "dict", "dict-keys"],
    )
    def test_names_given_in_any_iterable_are_stored_as_a_list(self, shape: Any) -> None:
        # One copy is taken, checked and stored: an iterable that can be read
        # only once must not be stored as nothing after the check has read it.
        @tool(name="probe", permissions=shape(["filesystem:read", "memory:write"]))
        async def _probe() -> ToolResult:
            return ToolResult(success=True)

        registered = get_tool("probe")
        assert registered is not None
        assert registered.permissions == ["filesystem:read", "memory:write"]
        assert type(registered.permissions) is list

    @pytest.mark.parametrize(
        "shape", [lambda names: (name for name in names), iter], ids=["generator", "iter"],
    )
    def test_a_bad_name_in_a_one_shot_iterable_is_still_an_error(self, shape: Any) -> None:
        with pytest.raises(ValueError, match="'db:read'"):
            @tool(name="probe", permissions=shape(["filesystem:read", "db:read"]))
            async def _probe() -> ToolResult:
                return ToolResult(success=True)

        assert get_tool("probe") is None

    def test_a_list_changed_after_the_tool_is_defined_changes_nothing(self) -> None:
        # The definition holds its own copy, not the author's list.
        mine = ["filesystem:read"]

        @tool(name="probe", permissions=mine)
        async def _probe() -> ToolResult:
            return ToolResult(success=True)

        mine.clear()
        mine.append("memory:write")

        registered = get_tool("probe")
        assert registered is not None and registered.permissions == ["filesystem:read"]
        assert registered.permissions is not mine

    def test_a_refused_tool_does_not_replace_the_one_registered_before(self) -> None:
        _declare("memory:read")
        before = get_tool("probe")

        with pytest.raises(ValueError):
            _declare("db:read")

        assert get_tool("probe") is before


class TestWhatTheGateAnswers:
    @pytest.mark.parametrize("permission", _SEVEN)
    def test_each_of_the_seven_is_granted_by_its_key(self, permission: str) -> None:
        assert PermissionGate(_EVERY_KEY).check(permission) is True
        assert PermissionGate({}).check(permission) is False

    @pytest.mark.parametrize("permission", _OF_ITS_OWN + _NO_SUCH_ACTION)
    def test_any_other_name_is_denied_whatever_the_config_holds(
        self, permission: str,
    ) -> None:
        assert PermissionGate(_EVERY_KEY).check(permission) is False

    @pytest.mark.parametrize(
        "permissions",
        ["all", ["filesystem:read"], 7, {"filesystem": True}, {"filesystem": ["read"]},
         {"filesystem": "read"}],
        ids=["text", "list", "number", "category-is-true", "category-is-a-list",
             "category-is-text"],
    )
    def test_a_block_of_the_wrong_shape_grants_nothing(self, permissions: Any) -> None:
        gate = PermissionGate(permissions)

        assert gate.check("filesystem:read") is False
        assert gate.missing(["filesystem:read"]) == "filesystem:read"

    def test_missing_names_the_first_permission_not_granted(self) -> None:
        gate = PermissionGate({"filesystem": {"read": ["/w/**"]}, "memory": {"read": True}})

        assert gate.missing([]) is None
        assert gate.missing(["filesystem:read", "memory:read"]) is None
        assert gate.missing(["filesystem:read", "memory:write", "shell:exec"]) == "memory:write"
        assert gate.missing(["db:read"]) == "db:read"
        # An entry that is not text is named, not raised on.
        assert gate.missing(["filesystem:read", 7]) == "7"  # type: ignore[list-item]


class TestTheBuiltInToolsDeclareWhatTheyAsk:
    async def test_all_nine_declare_one_grantable_permission(
        self, memory: EpisodicMemory,
    ) -> None:
        importlib.reload(builtin)
        gate = PermissionGate({})
        create_memory_tools(memory, gate)
        create_recall_tool(object(), gate, agent_id="ember-owl")  # type: ignore[arg-type]

        declared = {td.name: td.permissions for td in list_tools()}

        assert declared == {
            "file_read": ["filesystem:read"],
            "file_write": ["filesystem:write"],
            "shell_exec": ["shell:exec"],
            "http_request": ["network:http"],
            "store_note": ["memory:write"],
            "recall_notes": ["memory:read"],
            "update_note": ["memory:write"],
            "delete_note": ["memory:write"],
            "recall_channel_messages": ["channels:recall"],
        }


class TestTheNamesFollowTheAgentSchema:
    # Keys of the schema's ``permissions`` block that scope a tool and turn
    # nothing on by themselves.
    _SCOPING_ONLY = {
        ("filesystem", "deny"), ("network", "deny"), ("shell", "max_execution_seconds"),
    }
    # The key of its category that grants each permission.
    _GRANTED_BY = {
        "filesystem:read": "read", "filesystem:write": "write", "network:http": "allow",
        "shell:exec": "allowed_commands", "memory:read": "read", "memory:write": "write",
        "channels:recall": "recall",
    }

    @staticmethod
    def _schema_keys() -> set[tuple[str, str]]:
        block = json.loads(_SCHEMA.read_text())["definitions"]["permissions"]
        assert block["additionalProperties"] is False
        keys: set[tuple[str, str]] = set()
        for category, body in block["properties"].items():
            assert body["additionalProperties"] is False, category
            keys.update((category, key) for key in body["properties"])
        return keys

    def test_the_categories_are_the_schemas(self) -> None:
        assert {category for category, _ in self._schema_keys()} == PERMISSION_CATEGORIES

    def test_every_schema_key_grants_a_permission_or_is_named_as_scoping_only(self) -> None:
        granting = {
            (permission.split(":")[0], key) for permission, key in self._GRANTED_BY.items()
        }

        assert set(self._GRANTED_BY) == GRANTABLE_PERMISSIONS
        assert granting | self._SCOPING_ONLY == self._schema_keys()
        assert granting & self._SCOPING_ONLY == set()

    @pytest.mark.parametrize(("permission", "key"), sorted(_GRANTED_BY.items()))
    def test_each_permission_is_granted_by_its_schema_key_alone(
        self, permission: str, key: str,
    ) -> None:
        category = permission.split(":")[0]
        value: Any = True if category in ("memory", "channels") else ["x"]
        others = {
            other: body for other, body in _EVERY_KEY.items() if other != category
        }

        assert PermissionGate({category: {key: value}}).check(permission) is True
        # Every other key set, this one left out: still denied.
        rest = {k: v for k, v in _EVERY_KEY[category].items() if k != key}
        assert PermissionGate({**others, category: rest}).check(permission) is False
