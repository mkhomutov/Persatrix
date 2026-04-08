"""
Tests for tool registry and decorators.

PR review: proves the @tool decorator registers tools correctly, parameter
schema auto-generation works, the wrapper handles success/failure, and
clear_registry() provides test isolation.
"""

import pytest

from agents.tools.registry import (
    ToolResult,
    clear_registry,
    get_tool,
    list_tools,
    tool,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure each test starts with an empty registry."""
    clear_registry()
    yield
    clear_registry()


class TestToolDecorator:
    def test_registration(self):
        @tool(name="greet", description="Say hello")
        async def greet(name: str) -> ToolResult:
            return ToolResult(success=True, data=f"hello {name}")

        defn = get_tool("greet")
        assert defn is not None
        assert defn.name == "greet"
        assert defn.description == "Say hello"
        assert defn.tier == "custom"

    def test_default_name_from_function(self):
        @tool(description="test")
        async def my_func() -> ToolResult:
            return ToolResult(success=True)

        assert get_tool("my_func") is not None

    def test_parameter_schema_generation(self):
        @tool(name="calc")
        async def calc(x: int, y: float, flag: bool, label: str) -> ToolResult:
            return ToolResult(success=True)

        defn = get_tool("calc")
        assert defn is not None
        assert defn.parameters["x"]["type"] == "integer"
        assert defn.parameters["y"]["type"] == "number"
        assert defn.parameters["flag"]["type"] == "boolean"
        assert defn.parameters["label"]["type"] == "string"

    def test_required_vs_optional_params(self):
        @tool(name="opt")
        async def opt(required_arg: str, optional_arg: str = "default") -> ToolResult:
            return ToolResult(success=True)

        defn = get_tool("opt")
        assert defn is not None
        assert defn.parameters["required_arg"]["required"] is True
        assert defn.parameters["optional_arg"]["required"] is False

    def test_permissions_stored(self):
        @tool(name="risky", permissions=["shell:exec", "fs:write"])
        async def risky() -> ToolResult:
            return ToolResult(success=True)

        defn = get_tool("risky")
        assert defn is not None
        assert defn.permissions == ["shell:exec", "fs:write"]


class TestToolInvocation:
    async def test_successful_invocation(self):
        @tool(name="add")
        async def add(a: int, b: int) -> ToolResult:
            return ToolResult(success=True, data=a + b)

        result = await add(2, 3)
        assert result.success is True
        assert result.data == 5

    async def test_raw_return_wrapped_in_tool_result(self):
        @tool(name="raw")
        async def raw() -> str:
            return "plain value"

        result = await raw()
        assert isinstance(result, ToolResult)
        assert result.success is True
        assert result.data == "plain value"

    async def test_sync_function_support(self):
        @tool(name="sync_tool")
        def sync_tool() -> ToolResult:
            return ToolResult(success=True, data="sync")

        result = await sync_tool()
        assert result.success is True
        assert result.data == "sync"

    async def test_exception_captured(self):
        @tool(name="failing")
        async def failing() -> ToolResult:
            raise ValueError("bad input")

        result = await failing()
        assert result.success is False
        assert "bad input" in result.error
        assert result.error_type == "ValueError"


class TestRegistry:
    def test_list_tools(self):
        @tool(name="a")
        async def a():
            pass

        @tool(name="b")
        async def b():
            pass

        tools = list_tools()
        names = {t.name for t in tools}
        assert names == {"a", "b"}

    def test_clear_registry(self):
        @tool(name="temp")
        async def temp():
            pass

        assert get_tool("temp") is not None
        clear_registry()
        assert get_tool("temp") is None

    def test_get_nonexistent_tool(self):
        assert get_tool("does_not_exist") is None
