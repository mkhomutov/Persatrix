---
applyTo: "agents/**/*.py,tests/**/*.py,evaluators/**/*.py"
description: "Python agent conventions: async-first, 3.11+ type hints, ruff linting, tool decorator pattern, BaseAgent ABC"
---

# Python Agents

- **Type hints required.** Use `X | None` (not `Optional[X]`), `dict[str, Any]` (not `Dict`). mypy enforced.
- **Async-first:** all agent methods are `async def`. Entry points use `asyncio.run()`.
- **Linting:** ruff configured in `pyproject.toml`. Run `make lint` or `cd agents && ruff check .`.
- **Testing:** pytest with `asyncio_mode = "auto"`. Use `@pytest.fixture(autouse=True)` with `clear_registry()` for tool tests.
- **Error handling:** Set `error_type` field to distinguish transient vs permanent failures. Raise `NotImplementedError` for stubs.
- **Tool pattern:** `@tool(name=...)` auto-generates parameter schemas from type hints. The `permissions=[...]` it also takes are checked before the tool runs: `admit_call` (`agents/tools/tool_list.py`) answers a model's call with `Permission denied: <name>` when the agent's config does not grant every name. Only seven names can be declared, the ones an agent's config can grant: `filesystem:read`, `filesystem:write`, `network:http`, `shell:exec`, `memory:read`, `memory:write`, `channels:recall`. Any other raises `ValueError` when the tool is defined, so a tool cannot have a permission of its own yet. For anything finer than the name (which path, which command, which domain), ask in the tool's own body, as the file, shell and HTTP tools do in `agents/tools/builtin.py`. A tool that declares none is held only by the agent's `tools` list, and a tool function called directly from Python passes no check but its own.
- **gRPC servicer methods use PascalCase** to match the proto contract (e.g. `ExecuteTask`, `HealthCheck`). `N802` is suppressed for these — see `[tool.ruff.lint.per-file-ignores]` in `agents/pyproject.toml`, the file-level `# ruff: noqa: N802` in `agents/server_servicers.py`, and inline `# noqa: N802` on test servicers. Do not rename them to snake_case or remove the suppressions.
- **BaseAgent** is an ABC—subclass it and implement `handle(task: TaskInput) -> TaskOutput`.
- **PersonaAgent** extends BaseAgent with `on_event()` and `on_tick()` for event-driven autonomy (v0.2+).
- **Sub-agents** are not in use. `agents/sub_agents/spawner.py` runs a sub-agent inside the agent's own process, and tests run it; nothing else calls it. `PersonaAgent.spawn_sub_agent()` has no client behind it, and the `spawn_sub_agent` action answers `not_implemented`. No permission is inherited or narrowed: the rule that a sub-agent gets no more than its parent is not built ([ISSUE-0285](../../docs/issues/ISSUE-0285-sub-agent-permission-rule-and-delegation-timeout-are-not-built.md)), so do not rely on it. A task's `allowed_tools` list is enforced when it names a tool: a tool it does not name is not offered and does not run (`task_allows` in `agents/tools/tool_list.py`); an empty list restricts nothing.
- **Platform awareness:** Guard `loop.add_signal_handler()` with `sys.platform != "win32"`.
- **Dataclasses:** Use `field(default_factory=...)` for mutable defaults.
- **Comments in plain English.** Write comments a non-programmer could follow — say what the code does and why it matters, briefly. Full rules: [Documentation Guide § Writing Style](../../docs/documentation-guide.md#writing-style).

## TDD

- **Red-Green-Refactor:** Add a failing test in `tests/unit/python/` before writing implementation code. Run `pytest tests/unit/python/test_<module>.py -v` to confirm the red state.
- **Test file naming:** `tests/unit/python/test_<module>.py` mirrors `agents/<module>.py`. Component tests that need agent fixtures go in `agents/tests/`.
- **One assert per logical case:** Prefer separate test methods over multi-assert blocks; failures pinpoint the broken behaviour immediately.
- **Mocking LLM calls:** Always mock `LLMClient` at the boundary (`unittest.mock.AsyncMock`). Never let a unit test make real LLM or network calls.
- **Async tests:** Mark with `@pytest.mark.asyncio` (or rely on `asyncio_mode = "auto"`). Do not use `asyncio.run()` inside tests.
- **Integration tests** (`tests/integration/`) are exempt from strict TDD — write them after the unit layer validates the pieces.
