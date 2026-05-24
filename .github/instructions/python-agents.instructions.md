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
- **Tool pattern:** `@tool(name=..., permissions=[...])` auto-generates parameter schemas from type hints.
- **gRPC servicer methods use PascalCase** to match the proto contract (e.g. `ExecuteTask`, `HealthCheck`). `N802` is suppressed for these — see `[tool.ruff.lint.per-file-ignores]` in `agents/pyproject.toml`, the file-level `# ruff: noqa: N802` in `agents/server_servicers.py`, and inline `# noqa: N802` on test servicers. Do not rename them to snake_case or remove the suppressions.
- **BaseAgent** is an ABC—subclass it and implement `handle(task: TaskInput) -> TaskOutput`.
- **PersonaAgent** extends BaseAgent with `on_event()` and `on_tick()` for event-driven autonomy (v0.2+).
- **Sub-agents** are ephemeral: spawned by parent agents via `orchestrator_client.spawn_sub_agent()` with inherited permissions.
- **Platform awareness:** Guard `loop.add_signal_handler()` with `sys.platform != "win32"`.
- **Dataclasses:** Use `field(default_factory=...)` for mutable defaults.
- **Comments in plain English.** Write comments a non-programmer could follow — say what the code does and why it matters, briefly. Full rules: [Documentation Guide § Writing Style](../../docs/documentation-guide.md#writing-style).

## TDD (from v0.3.0 onward)

- **Red-Green-Refactor:** Add a failing test in `tests/unit/python/` before writing implementation code. Run `pytest tests/unit/python/test_<module>.py -v` to confirm the red state.
- **Test file naming:** `tests/unit/python/test_<module>.py` mirrors `agents/<module>.py`. Component tests that need agent fixtures go in `agents/tests/`.
- **One assert per logical case:** Prefer separate test methods over multi-assert blocks; failures pinpoint the broken behaviour immediately.
- **Mocking LLM calls:** Always mock `LLMClient` at the boundary (`unittest.mock.AsyncMock`). Never let a unit test make real LLM or network calls.
- **Async tests:** Mark with `@pytest.mark.asyncio` (or rely on `asyncio_mode = "auto"`). Do not use `asyncio.run()` inside tests.
- **Integration tests** (`tests/integration/`) are exempt from strict TDD — write them after the unit layer validates the pieces.
