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
