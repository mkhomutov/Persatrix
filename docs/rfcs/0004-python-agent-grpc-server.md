# RFC 0004 — Python Agent gRPC Server (AgentService Implementation)

**Type**: architecture
**Status**: 📋 Proposed
**Author**: Orchestr8 team
**Date**: 2026-04-09
**Target**: v0.1 (MVP)
**Depends on**: RFC 0001, RFC 0003
**Superseded by**: None

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Implement the Python agent-side gRPC server that receives `TaskRequest` messages from the Go orchestrator's `GRPCExecutor` (RFC 0003) and returns `TaskResponse` results. This RFC covers the `AgentService` servicer, agent loading from config, the three v0.1 task agent implementations (`PlannerAgent`, `CoderAgent`, `ReviewerAgent`), their LLM interaction loop, and the four built-in tools (`file_read`, `file_write`, `shell_exec`, `http_request`). Together, these close the end-to-end execution loop: CLI → REST API → Scheduler → Executor → **Agent** → LLM → tools → result.

## Motivation

RFCs 0001–0003 built the complete Go orchestrator: state management, REST API, workflow scheduling, and gRPC task dispatch. However, the Executor dispatches `TaskRequest` messages to agent addresses that currently host nothing — `agents/server.py` is a stub that cannot accept gRPC connections, and all three task agents raise `NotImplementedError`. The system is architecturally complete but functionally inert.

```text
CLI ──REST──► HTTP Server (RFC 0002)
                   │
                   └─ POST /api/v1/workflows/run  ──► Scheduler (RFC 0003)
                                                           │
                                                           ▼
                                                       Executor
                                                           │
                                                       gRPC dial
                                                           │
                                                           ▼
                                                    ┌─────────────┐
                                                    │ Agent Server │ ← THIS RFC
                                                    │  (Python)    │
                                                    │              │
                                                    │  AgentService│
                                                    │  servicer    │
                                                    │      │       │
                                                    │      ▼       │
                                                    │  BaseAgent   │
                                                    │  .handle()   │
                                                    │      │       │
                                                    │      ▼       │
                                                    │  LLM Client  │
                                                    │  + Tools     │
                                                    └─────────────┘
```

Without this RFC, submitting a workflow via the CLI produces a `Failed` run every time — the Executor cannot connect to any agent. Completing this RFC delivers the first fully functional end-to-end workflow execution: a user submits a feature request via the CLI, the orchestrator plans the DAG, dispatches tasks to Python agents, agents call an LLM with tool use, and results flow back through the pipeline.

If we do nothing, the project has a well-tested orchestrator that orchestrates nothing.

## Goals

1. Implement `AgentService` gRPC servicer in `agents/server.py` — handle `ExecuteTask` and `HealthCheck` RPCs.
2. Implement agent loading from `config/agents.yaml` — parse config, determine agent type, instantiate the correct `BaseAgent` subclass with model/tools/permissions.
3. Implement `CoderAgent.handle()`, `ReviewerAgent.handle()`, and `PlannerAgent.handle()` — system prompt construction, LLM call, tool-use loop, output formatting.
4. Implement the four built-in tools: `file_read`, `file_write`, `shell_exec`, `http_request` with permission enforcement and sandboxing.
5. Implement `PermissionGate` in `agents/tools/permissions.py` — deny-by-default permission checks against agent config.
6. Implement `PathValidator` in `agents/tools/sandbox.py` — workspace-scoped path restriction for filesystem tools.
7. Generate Python gRPC stubs from `proto/task.proto`.
8. Wire the server for single-agent-per-process operation: `python -m agents.server --agent <id> --port <port>`.
9. Achieve ≥ 80% test coverage for `agents/` packages.

## Non-Goals

- **Persona agents.** `PersonaAgent` with `on_event()` and `on_tick()` is v0.2+. Only task agents are implemented.
- **Streaming execution.** `ExecuteTaskStream` RPC returns `stream TaskProgress` — deferred to v0.2. The servicer method returns `UNIMPLEMENTED`.
- **MCP tool bridge.** `agents/tools/mcp_bridge.py` is v0.2+. The `mcp:github` tool references in `config/agents.yaml` are parsed but produce a warning at startup ("MCP tools not yet available").
- **Memory tiers.** `agents/memory/` (episodic, relationship, working) is v0.2+. Agents are stateless within a single task execution.
- **Sub-agent spawning.** `agents/sub_agents/` is v0.2+.
- **Multi-agent process.** v0.1 runs one agent per process. Multi-agent hosting (loading multiple agents into a single gRPC server) is deferred.
- **Real LLM calls in tests.** All tests use mock LLM responses. No API keys required for CI.
- **Connection keep-alive / multiplexing.** The gRPC server accepts connections as they arrive; no client-side pooling concern on the agent side.
- **Rate limiting.** Per-agent rate limiting on the tool wrapper is scaffolded (`# TODO: Rate limit check`) but not implemented.
- **Telemetry / OTEL spans.** Tool invocation spans are scaffolded but not wired.
- **`store_get` / `store_set` tools.** Task-scoped key-value store tools are deferred. The four core tools (`file_read`, `file_write`, `shell_exec`, `http_request`) are sufficient for v0.1 workflows.
- **Config validation.** `agents/validate.py` is a separate concern. This RFC does not implement JSON Schema validation of `agents.yaml`.
- **Per-task tool restriction.** `TaskConfig.allowed_tools` allows the orchestrator to restrict tools per-task. Enforcing this filter is deferred to v0.2; in v0.1 the agent uses its full configured tool set for every task. (Review-fix D2)
- **`ResourceLimiter` / `OutputSizeLimiter` stubs.** The existing `agents/tools/sandbox.py` has TODO stubs for `ResourceLimiter` and `OutputSizeLimiter` alongside `PathValidator`. This RFC only implements `PathValidator`; the other two stubs are preserved as-is for v0.2. (PR-review m8)

## Design / Implementation

### Proto Compilation (Python Stubs)

Generate Python gRPC stubs from `proto/task.proto`:

```text
proto/task.proto  ──grpc_tools.protoc──►  agents/generated/task_pb2.py
                                          agents/generated/task_pb2_grpc.py
                                          agents/generated/__init__.py
```

The `Makefile` `proto` target is extended to generate Python stubs alongside Go stubs:

```makefile
proto-python:
	python -m grpc_tools.protoc \
		-I proto/ \
		--python_out=agents/generated \
		--grpc_python_out=agents/generated \
		proto/task.proto
```

Dependencies added to `pyproject.toml`: `grpcio >= 1.68.0`, `grpcio-tools >= 1.68.0`, `protobuf >= 5.28.0` (matching existing version pins in `pyproject.toml`). Additional new dependencies across all phases: `aiohttp >= 3.9.0` (for `http_request` tool and self-registration) and `anthropic >= 0.40.0` (for LLM client). (PR-review N1: aligned version pins with pyproject.toml; PR-review M7: consolidated dependency list.)

### TaskInputConfig Dataclass

**File:** `agents/base.py`

The existing `TaskInput` dataclass is extended with a `config` field carrying per-task configuration from `TaskConfig` in the proto. A new `TaskInputConfig` dataclass is defined alongside `TaskInput`:

```python
@dataclass
class TaskInputConfig:
    """Per-task configuration overrides from TaskConfig proto message."""

    max_llm_calls: int = 0  # 0 means "use agent default"
    max_tokens: int = 0     # 0 means "use agent default"
    # PR-review B2: carry allowed_tools from proto even though enforcement
    # is deferred to v0.2, so the field is available to wire up later.
    allowed_tools: list[str] = field(default_factory=list)  # TODO(v0.2): enforce allowed_tools filter in agent handle()


@dataclass
class TaskInput:
    """Input to an agent for task execution."""

    task_id: str
    workflow_id: str
    payload: str
    context: dict[str, str] = field(default_factory=dict)
    config: TaskInputConfig = field(default_factory=TaskInputConfig)  # Review-fix D1
```

The `config` field has a default so that existing tests and callers that construct `TaskInput` without config continue to work.

### AgentService Servicer

**File:** `agents/server.py`

```python
class AgentServiceServicer(task_pb2_grpc.AgentServiceServicer):
    """gRPC servicer implementing AgentService from proto/task.proto."""

    def __init__(self, agents: dict[str, BaseAgent]):
        self._agents = agents

    async def ExecuteTask(
        self,
        request: task_pb2.TaskRequest,
        context: grpc.aio.ServicerContext,
    ) -> task_pb2.TaskResponse:
        agent = self._agents.get(request.agent_id)
        if agent is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"Agent not found: {request.agent_id}")
            return task_pb2.TaskResponse(
                task_id=request.task_id,
                status=task_pb2.FAILED,
                error_message=f"Agent not found: {request.agent_id}",
            )

        task_input = TaskInput(
            task_id=request.task_id,
            workflow_id=request.workflow_id,
            payload=request.payload,
            context=dict(request.context),
            # Review-fix D1: pass per-task config overrides through to agent
            config=TaskInputConfig(
                max_llm_calls=request.config.max_llm_calls or 0,
                max_tokens=request.config.max_tokens or 0,
                # PR-review B2: carry allowed_tools even though enforcement
                # is deferred to v0.2 — avoids silently discarding proto fields.
                allowed_tools=list(request.config.allowed_tools),
            ),
        )

        try:
            start = time.monotonic()
            output = await asyncio.wait_for(
                agent.handle(task_input),
                timeout=request.config.timeout_seconds or None,
            )
            duration_ms = int((time.monotonic() - start) * 1000)

            return task_pb2.TaskResponse(
                task_id=request.task_id,
                status=task_pb2.COMPLETED if output.status == TaskStatus.COMPLETED
                       else task_pb2.FAILED,
                result=output.result,
                metadata={
                    # Review-fix P2: use json.dumps for non-str values to
                    # produce parseable strings instead of repr()-like output
                    # (e.g. "None", "{'k': 'v'}") from plain str().
                    **{k: v if isinstance(v, str) else json.dumps(v)
                       for k, v in output.metadata.items()},
                    "duration_ms": str(duration_ms),
                },
                error_message="" if output.status == TaskStatus.COMPLETED
                              else output.result,
            )
        except asyncio.TimeoutError:
            context.set_code(grpc.StatusCode.DEADLINE_EXCEEDED)
            context.set_details("Task execution timed out")
            return task_pb2.TaskResponse(
                task_id=request.task_id,
                status=task_pb2.FAILED,
                error_message="Task execution timed out",
            )
        except Exception as exc:
            logger.exception("Task execution failed: %s", request.task_id)
            return task_pb2.TaskResponse(
                task_id=request.task_id,
                status=task_pb2.FAILED,
                error_message=str(exc),
            )

    async def HealthCheck(
        self,
        request: task_pb2.HealthCheckRequest,
        context: grpc.aio.ServicerContext,
    ) -> task_pb2.HealthCheckResponse:
        # PR-review B4: delegate to agent.health_check() so agents can
        # report unhealthy state (e.g. LLM API key missing, workspace
        # not mounted) instead of hardcoding SERVING.
        agent = self._agents.get(request.service) if request.service else None
        if agent is not None:
            healthy = await agent.health_check()
        else:
            # No specific agent requested — report healthy if any agent is loaded.
            healthy = len(self._agents) > 0
        return task_pb2.HealthCheckResponse(
            status=task_pb2.SERVING if healthy else task_pb2.NOT_SERVING,
        )

    async def ExecuteTaskStream(
        self,
        request: task_pb2.TaskRequest,
        context: grpc.aio.ServicerContext,
    ) -> None:
        # TODO(v0.2): implement streaming execution with TaskProgress messages
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Streaming execution not implemented in v0.1")
```

The servicer catches all exceptions from `agent.handle()` and converts them to `FAILED` `TaskResponse` — the gRPC call itself never fails with an application error, which prevents the Go Executor from retrying a task that legitimately failed (as opposed to a transient gRPC transport error). Agents only return terminal statuses (`COMPLETED` or `FAILED`) in `TaskResponse.status`. The `RUNNING`, `PENDING`, `RETRYING`, and `CANCELLED` statuses are managed by the orchestrator's state store (RFC 0001) and are never set by agents. (PR-review m1)

### Agent Loading

**File:** `agents/server.py` — `load_agent()` function

```python
def load_agent(agent_id: str, config_path: str) -> BaseAgent:
    """Load an agent by ID from YAML config."""
    # PR-review m5: surface clear errors at startup for operator experience
    # rather than raw tracebacks from missing files or malformed YAML.
    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        raise SystemExit(f"Agent config not found: {config_path}")
    except yaml.YAMLError as exc:
        raise SystemExit(f"Invalid YAML in {config_path}: {exc}")

    agent_configs = {a["id"]: a for a in config.get("agents", [])}
    if agent_id not in agent_configs:
        raise ValueError(f"Agent {agent_id!r} not found in {config_path}")

    agent_config = agent_configs[agent_id]
    agent_type = _resolve_agent_type(agent_config)

    return agent_type(agent_id=agent_id, config=agent_config)
```

Agent type resolution uses capabilities and config to map to the correct class:

| Agent ID | Class | Resolution rule |
|----------|-------|-----------------|
| `planner` | `PlannerAgent` | `"planning" in capabilities` |
| `code-writer` | `CoderAgent` | `"code_generation" in capabilities` |
| `code-reviewer` | `ReviewerAgent` | `"code_review" in capabilities and "code_generation" not in capabilities` |

A `type` field in `agents.yaml` would be cleaner; the capability-based heuristic is a v0.1 pragmatic choice since adding a `type` field requires a schema change and config migration. A `# TODO(v0.2): add explicit 'type' field to agent config` comment marks this. If no rule matches, the function raises `ValueError(f"Cannot determine agent type for {agent_id} from capabilities: {caps}")` so that misconfigured agents fail loudly at startup rather than silently falling through. (Review-fix D3)

### Task Agent Implementation

All three task agents follow the same pattern:

1. Build a system prompt from `self.config["role"]` and agent-specific instructions.
2. Initialize an LLM client using `self.config["model"]` and `self.config["temperature"]`.
3. Send the task payload + context to the LLM.
4. Handle tool-use responses: if the LLM returns tool calls, dispatch them through the tool registry, collect results, and send them back to the LLM.
5. Repeat the tool-use loop until the LLM returns a final text response or the max iteration limit is reached.
6. Return a `TaskOutput` with the final result and metadata (tokens used, tool calls count).

When the LLM returns multiple tool calls in a single response, tools are executed **sequentially** in the order they appear in `response.content`. This avoids file-write conflicts and makes tool side-effects deterministic. A `# TODO(v0.2): parallel tool execution with conflict detection` comment marks the optimization opportunity. (Review-fix D6)

#### `_execute_tools` Method

The `_execute_tools` helper parses Anthropic `ToolUseBlock` objects from the response content, dispatches each to the tool registry, enforces permissions, and formats results back into the Anthropic tool-result message format. (Review-fix M3)

```python
async def _execute_tools(self, content: list) -> list[dict]:
    """Execute tool calls from LLM response content sequentially.

    Parses ToolUseBlock objects, dispatches through the tool registry
    with permission checks, and returns Anthropic-format tool results.
    """
    results = []
    for block in content:
        if block.type != "tool_use":
            continue
        tool_fn = self._tool_registry.get(block.name)
        if tool_fn is None:
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": f"Unknown tool: {block.name}",
                "is_error": True,
            })
            continue
        try:
            result = await tool_fn(**block.input)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result.data if result.success else result.error,
                "is_error": not result.success,
            })
        except PermissionError as exc:
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(exc),
                "is_error": True,
            })
        # PR-review M2: catch all exceptions, not just PermissionError.
        # Other tool errors (FileNotFoundError, aiohttp.ClientError, OSError)
        # should be returned as structured tool-error results so the LLM
        # can decide how to proceed, rather than bubbling up as an opaque
        # task failure via the generic except in ExecuteTask.
        except Exception as exc:
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": f"Tool error ({type(exc).__name__}): {exc}",
                "is_error": True,
            })
    return results
```

```python
async def handle(self, task: TaskInput) -> TaskOutput:
    messages = self._build_messages(task)
    tools = self._get_tool_definitions()
    total_tokens = 0
    tool_calls_count = 0

    # Review-fix D1: per-task overrides from TaskConfig take precedence
    # over agent YAML config.  Zero means "not set" (proto int32 default).
    max_llm_calls = task.config.max_llm_calls or self.config.get("max_llm_calls", 10)
    max_tokens = task.config.max_tokens or self.config.get("max_tokens", 4096)

    for _ in range(max_llm_calls):
        response = await self._llm_client.create_message(
            model=self.config["model"],
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=self.config.get("temperature", 0.3),
        )
        total_tokens += response.usage.input_tokens + response.usage.output_tokens

        if response.stop_reason == "end_turn":
            return TaskOutput(
                status=TaskStatus.COMPLETED,
                result=self._extract_text(response),
                metadata={"tokens_used": str(total_tokens), "tool_calls": str(tool_calls_count)},
            )

        # Review-fix B2: explicitly handle max_tokens truncation instead of
        # letting the loop continue and eventually producing a misleading
        # "Max LLM call iterations exceeded" failure.
        if response.stop_reason == "max_tokens":
            return TaskOutput(
                status=TaskStatus.FAILED,
                result="LLM response truncated: max_tokens limit reached",
                metadata={"tokens_used": str(total_tokens), "tool_calls": str(tool_calls_count)},
            )

        if response.stop_reason == "tool_use":
            tool_results = await self._execute_tools(response.content)
            tool_calls_count += len(tool_results)
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            continue

    return TaskOutput(
        status=TaskStatus.FAILED,
        result="Max LLM call iterations exceeded",
        metadata={"tokens_used": str(total_tokens), "tool_calls": str(tool_calls_count)},
    )
```

#### LLM Client Abstraction

A thin `LLMClient` wrapper around the Anthropic Python SDK isolates the LLM interaction for testability:

```python
class LLMClient:
    """Wrapper around the Anthropic SDK for LLM calls."""

    def __init__(self, api_key: str | None = None):
        import anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def create_message(self, **kwargs) -> Any:
        return await self._client.messages.create(**kwargs)
```

The LLM client is injected into agents via `BaseAgent.__init__`, enabling tests to substitute a mock client without patching. The API key is read from the `ANTHROPIC_API_KEY` environment variable (Anthropic SDK default behavior). No API key is stored in config files.

#### Agent-Specific System Prompts

Each agent builds its system prompt from the configured `role` plus agent-specific instructions:

| Agent | System Prompt Focus |
|-------|-------------------|
| `PlannerAgent` | Task decomposition, step ordering, dependency identification. Output valid YAML/JSON plan. |
| `CoderAgent` | Code generation per spec, use `file_write` and `shell_exec` tools, follow language conventions, include tests. |
| `ReviewerAgent` | Code correctness, style, security. Return structured review with `approved` boolean and list of issues. |

### Built-in Tool Implementation

**File:** `agents/tools/builtin.py`

All tools enforce permissions before execution via `PermissionGate.check()`. All filesystem tools enforce path restrictions via `PathValidator`.

#### `file_read`

```python
@tool(name="file_read", description="Read the contents of a file", permissions=["filesystem:read"])
async def file_read(path: str) -> ToolResult:
    validated_path = path_validator.validate(path, mode="read")
    content = validated_path.read_text(encoding="utf-8")
    return ToolResult(success=True, data=content)
```

#### `file_write`

```python
@tool(name="file_write", description="Write content to a file", permissions=["filesystem:write"])
async def file_write(path: str, content: str) -> ToolResult:
    validated_path = path_validator.validate(path, mode="write")
    validated_path.parent.mkdir(parents=True, exist_ok=True)
    validated_path.write_text(content, encoding="utf-8")
    return ToolResult(success=True, data=f"Wrote {len(content)} bytes to {path}")
```

#### `shell_exec`

```python
@tool(name="shell_exec", description="Execute a shell command", permissions=["shell:exec"])
async def shell_exec(command: str, timeout: int = 30) -> ToolResult:
    # Split command, validate against allowlist (no shell=True)
    args = shlex.split(command)
    if not permission_gate.is_command_allowed(args):
        return ToolResult(success=False, error=f"Command not allowed: {args[0]}", error_type="permanent")

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace_root,  # Review-fix D5: explicit CWD prevents non-deterministic
                              # behavior depending on server startup directory.
                              # workspace_root is set from --workspace CLI flag.
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        # PR-review M6: send SIGTERM first for graceful cleanup, then
        # SIGKILL after a short grace period if the process doesn't exit.
        # Always await proc.wait() to reap the child and avoid zombies.
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        return ToolResult(success=False, error=f"Command timed out after {timeout}s", error_type="transient")

    return ToolResult(
        success=proc.returncode == 0,
        data=stdout.decode(errors="replace"),
        error=stderr.decode(errors="replace") if proc.returncode != 0 else None,
    )
```

**Security:** Commands are executed via `create_subprocess_exec` (not `shell=True`), are split with `shlex.split`, and must match the `allowed_commands` list from the agent's `permissions.shell` config. The allowlist supports multi-word entries (e.g., `"git diff"`) by comparing progressively longer prefixes of the parsed command against the list — so `"git diff"` matches `["git diff"]` and also `["git"]`, but `"git push"` would **not** match `["git diff"]`. Arguments beyond the matched prefix are unrestricted. A `# TODO(v0.2): argument pattern matching for shell commands` marks this gap (see also [Security Considerations: Shell Command Argument Injection](#shell-command-argument-injection)).

#### `http_request`

```python
@tool(name="http_request", description="Make an HTTP request", permissions=["network:http"])
async def http_request(url: str, method: str = "GET", body: str = "") -> ToolResult:
    parsed = urllib.parse.urlparse(url)

    # Review-fix S1: reject malformed URLs and non-HTTP schemes before
    # the domain check.  Without this, urlparse("not-a-url").hostname
    # returns None causing TypeError in fnmatch, and non-HTTP schemes
    # (file://, ftp://) could bypass the domain allowlist.
    if parsed.scheme not in ("http", "https"):
        return ToolResult(success=False, error=f"Unsupported URL scheme: {parsed.scheme!r}", error_type="permanent")
    if not parsed.hostname:
        return ToolResult(success=False, error=f"Invalid URL (no hostname): {url}", error_type="permanent")

    if not permission_gate.is_domain_allowed(parsed.hostname):
        return ToolResult(success=False, error=f"Domain not allowed: {parsed.hostname}", error_type="permanent")

    # Use aiohttp for async HTTP
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, data=body if body else None, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            text = await resp.text()
            return ToolResult(success=resp.status < 400, data=text, error=text if resp.status >= 400 else None)
```

### Permission Gate

**File:** `agents/tools/permissions.py`

Deny-by-default permission enforcement. The gate is initialized with the agent's `permissions` config block.

```python
class PermissionGate:
    """Enforces deny-by-default permissions for tool invocations."""

    def __init__(self, permissions: dict[str, Any]):
        self._permissions = permissions

    def check(self, permission: str) -> bool:
        """Check if a permission is granted. Returns False if not explicitly allowed."""
        category, action = permission.split(":", 1)
        category_perms = self._permissions.get(category)
        if category_perms is None:
            return False  # deny by default
        # ... check action against category config

    def is_command_allowed(self, args: list[str]) -> bool:
        """Check shell command against allowed_commands list.

        Supports multi-word entries (e.g. "git diff"): compares
        progressively longer prefixes of `args` against the allowlist
        so that both "git" (all git subcommands) and "git diff"
        (only git-diff) can be expressed.  Longest match wins.

        Review-fix B1: previously compared only args[0], which could
        never match a multi-word entry like "git diff".
        """
        allowed = self._permissions.get("shell", {}).get("allowed_commands", [])
        # Check progressively longer command prefixes (longest first)
        for n in range(len(args), 0, -1):
            candidate = " ".join(args[:n])
            if candidate in allowed:
                return True
        return False

    def is_domain_allowed(self, domain: str) -> bool:
        """Check domain against network allow/deny lists.

        Semantics: allow overrides deny ("default-deny allowlist" pattern).
        This intentionally differs from PathValidator's "deny always wins"
        semantics because the standard network config uses deny: ["*"] as
        a blanket default with specific allow entries (e.g.
        allow: ["api.anthropic.com"]).  If deny took unconditional
        precedence, deny: ["*"] would block everything including the
        LLM API, making the allow list useless.

        PathValidator does not use wildcard denies — its deny list contains
        specific sensitive paths (.env, .git/**) that must never be accessed
        regardless of allow patterns.  The two models serve different
        use cases and the semantic difference is intentional.
        (PR-review B3: documented rather than changing the logic, because
        the reviewer's proposed fix would break deny:["*"]+allow:[specific]
        configs used by all agents in agents.yaml.)
        """
        net = self._permissions.get("network", {})
        deny = net.get("deny", [])
        allow = net.get("allow", [])
        if any(fnmatch.fnmatch(domain, p) for p in deny):
            if not any(fnmatch.fnmatch(domain, p) for p in allow):
                return False
        return any(fnmatch.fnmatch(domain, p) for p in allow)
```

### Path Validator (Sandbox)

**File:** `agents/tools/sandbox.py`

```python
class PathValidator:
    """Validates filesystem paths against an agent's permission config."""

    def __init__(self, read_paths: list[str], write_paths: list[str], deny_paths: list[str]):
        self._read_paths = read_paths
        self._write_paths = write_paths
        self._deny_paths = deny_paths

    def validate(self, path: str, mode: str) -> Path:
        """Validate and resolve a path. Raises PermissionError if denied."""
        resolved = Path(path).resolve()

        # Deny list takes precedence
        for pattern in self._deny_paths:
            if fnmatch.fnmatch(str(resolved), pattern):
                raise PermissionError(f"Path denied by policy: {path}")

        # Check allowlist
        allowed = self._read_paths if mode == "read" else self._write_paths
        if not any(fnmatch.fnmatch(str(resolved), p) for p in allowed):
            raise PermissionError(f"Path not in {mode} allowlist: {path}")

        return resolved
```

Path traversal protection: `Path.resolve()` canonicalizes the path (resolves `..`, symlinks), then the resolved absolute path is matched against allow/deny glob patterns. This prevents `../../etc/passwd` attacks because the resolved path won't match workspace glob patterns like `/workspace/src/**`.

### Server Wiring

The `main()` function in `agents/server.py`:

1. Parse CLI args (`--agent`, `--port`, `--host`, `--config`, `--orchestrator-url`, `--workspace`).
2. Load agent config from YAML.
3. Instantiate the agent with `load_agent(agent_id, config_path)`.
4. Initialize the gRPC async server (`grpc.aio.server()`).
5. Register `AgentServiceServicer` with the loaded agent.
6. Bind to `host:port` with insecure port (no TLS in v0.1; `# TODO(security): enable TLS`).
7. Start serving with graceful shutdown on `SIGTERM` / `SIGINT`.

Single-agent-per-process model: Each agent process registers with the orchestrator via `POST /api/v1/agents/register` at startup (or is pre-registered in `docker-compose.yaml` env). The registration includes the agent's `host:port` address for the Executor to dial.

#### Graceful Shutdown Sequence

On `SIGTERM` or `SIGINT` the server follows this sequence (PR-review B5):

1. **Stop accepting new RPCs.** Call `server.stop(grace=30)` — gRPC stops accepting new connections but lets in-flight RPCs finish.
2. **Wait for in-flight tasks.** In-flight `ExecuteTask` RPCs have up to 30 seconds to complete. If they finish within the window, their responses are sent normally.
3. **Cancel remaining RPCs.** After the 30-second grace period, any still-running RPCs are cancelled by gRPC. The Go Executor will see a transport error and may retry (subject to RFC 0003's retry policy).
4. **Call agent shutdown hooks.** Invoke `await agent.shutdown()` for the loaded agent, allowing cleanup of resources (e.g., open aiohttp sessions).
5. **De-register from orchestrator.** Send `DELETE /api/v1/agents/{id}` to the orchestrator URL (best-effort; failure is logged at WARNING). This prevents the Executor from dispatching new tasks to a stopped agent.
6. **Exit.**

The 30-second grace period is chosen to match the default `shell_exec` timeout, giving the most common tool operation time to finish. It is configurable via `--shutdown-grace` CLI flag.

### Agent Self-Registration

After the gRPC server starts, the agent process registers itself with the orchestrator's REST API:

```python
async def _self_register(self, orchestrator_url: str, agent_id: str, host: str, port: int) -> None:
    """Register this agent with the orchestrator."""
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"{orchestrator_url}/api/v1/agents/register",
            json={
                "id": agent_id,
                # PR-review m7: include name and role to satisfy RFC 0002's
                # AgentInfo contract (RFC 0001).  Without these the registry
                # stores an agent with empty Name/Role fields.
                "name": self.agents[agent_id].name,
                "role": self.agents[agent_id].role,
                "address": f"{host}:{port}",
                "capabilities": self.agents[agent_id].capabilities,
                # Review-fix P1: "status" removed — RFC 0002 sets
                # status=healthy server-side on registration; sending
                # it here violates the API contract.
            },
        )
```

The `--orchestrator-url` CLI flag (default: `http://127.0.0.1:8080`) configures the target. Self-registration is best-effort — if the orchestrator is unreachable, the agent logs a warning and continues serving. The orchestrator admin can manually register agents via the REST API.

### Logging

All modules use Python's stdlib `logging` module with `logging.getLogger(__name__)` per file, consistent with existing agent stubs. Log level is configurable via the `--log-level` CLI flag (default: `INFO`). Structured fields (task_id, agent_id, tool_name) are included in log messages via `logger.info("...", extra={...})` for grep-friendly output. API keys and tool output content are never logged. (Review-fix M1)

## Security Considerations

### gRPC Without TLS (v0.1)

Consistent with RFC 0003's Go Executor: `grpc.aio.server()` with insecure port for v0.1/localhost/docker-compose use. A `# TODO(security): enable TLS for production gRPC` comment is placed at the server creation call. Production deployments MUST enable TLS or mTLS.

### LLM API Key Handling

The Anthropic API key is read from the `ANTHROPIC_API_KEY` environment variable. It is never logged, stored in config files, or included in `TaskResponse.metadata`. The agent process inherits the env var from `docker-compose.yaml` or the operator's shell.

### Tool Sandboxing

- **Filesystem:** All paths resolved to absolute form via `Path.resolve()`, then checked against allow/deny globs. Deny list takes precedence. `.env` and `.git/` are denied by default in agent config.
- **Shell:** Commands split via `shlex.split()` (no shell injection via `shell=True`). Only allowlisted command names or multi-word prefixes are permitted (see [Shell Command Argument Injection](#shell-command-argument-injection-known-risk)). Execution timeout enforced via `asyncio.wait_for`. Subprocess CWD is pinned to the workspace root.
- **Network:** HTTP requests check domain against allow/deny lists before connecting. `deny: ["*"]` + `allow: ["api.anthropic.com"]` is the v0.1 default — agents can only reach the LLM API.

### Input Validation

`TaskRequest.payload` and `TaskRequest.context` are treated as opaque strings passed to the LLM. The agent does not eval, exec, or interpret these values beyond including them in the LLM prompt. Tool outputs are also passed as strings back to the LLM. This prevents prompt injection from executing arbitrary code on the agent host — code execution only happens through the sandboxed `shell_exec` tool with its allowlist.

### No Authentication (v0.1)

The gRPC server accepts connections from any client on its bound address. In v0.1 the agent binds to `127.0.0.1` (loopback) by default. The `--host 0.0.0.0` flag is used in Docker containers where the agent needs to be reachable from the orchestrator container. Authentication is deferred to the security RFC.

### Shell Command Argument Injection (Known Risk)

The `shell_exec` tool validates the command base name (or multi-word prefix) against an allowlist but does **not** restrict arguments. An LLM could invoke an allowed command with dangerous arguments — e.g., `python -c "import os; os.system('rm -rf /')"` if `python` is in the allowlist. This is a **known v0.1 limitation**. Mitigation: agents run in Docker containers with limited filesystem mounts and the `PathValidator` deny list blocks sensitive paths. Full argument pattern matching (e.g., per-command regex on allowed argument patterns) is deferred to v0.2. (Review-fix S3)

### DNS Rebinding (Known Limitation)

The `http_request` tool checks the domain name against the allowlist before connecting, but a DNS record could resolve to a private IP after the check passes. This is a standard SSRF-via-DNS-rebinding vector. For v0.1, the impact is limited because the default network policy is `deny: ["*"]` with only `api.anthropic.com` allowed. DNS rebinding hardening (resolve DNS before connect, reject private IPs) is deferred to v0.2.

## Phased Implementation Plan

### Phase 1: Proto Generation (Python Stubs) (~30 LOC config, generated output)

Summary: Generate Python gRPC stubs from `proto/task.proto`.

**Deliverables:**
1. `agents/generated/__init__.py` — package marker.
2. `agents/generated/task_pb2.py` — generated protobuf message types.
3. `agents/generated/task_pb2_grpc.py` — generated gRPC service stubs.
4. `Makefile` — add `proto-python` target.
5. `pyproject.toml` — add `grpcio`, `grpcio-tools`, `protobuf` dependencies.

**Dependencies:** None (proto files exist).

**Git policy:** Generated Python stubs (`agents/generated/*.py`) are committed to the repository, consistent with Go generated stubs in `internal/generated/`. This avoids requiring `grpcio-tools` at deployment time and keeps CI reproducible. (Review-fix M4)

### Phase 2: Permission Gate + Path Validator (~200 LOC)

Summary: Implement deny-by-default permission checks and filesystem sandboxing.

**Deliverables:**
1. `agents/tools/permissions.py` — `PermissionGate` class.
2. `agents/tools/sandbox.py` — `PathValidator` class.
3. `agents/tools/tests/test_permissions.py` — permission tests.
4. `agents/tools/tests/test_sandbox.py` — path validation tests.

**Dependencies:** None. Independent of proto stubs.

### Phase 3: Built-in Tools (~250 LOC)

Summary: Implement `file_read`, `file_write`, `shell_exec`, `http_request` with permission and sandbox integration.

**Deliverables:**
1. `agents/tools/builtin.py` — replace stubs with working implementations.
2. `tests/unit/python/test_tools.py` — tool unit tests.

**Dependencies:** Phase 2 (permission gate, path validator).

### Phase 4: Task Agents + LLM Client (~350 LOC)

Summary: Implement the three task agents with LLM interaction loop and tool dispatch.

**Deliverables:**
1. `agents/llm_client.py` — new `LLMClient` wrapper.
2. `agents/coder.py` — implement `handle()`.
3. `agents/reviewer.py` — implement `handle()`.
4. `agents/planner_agent.py` — implement `handle()`.
5. `tests/unit/python/test_agents.py` — agent tests with mock LLM.

**Dependencies:** Phase 3 (built-in tools for tool dispatch). `anthropic` SDK added to `pyproject.toml`.

### Phase 5: gRPC Server + Agent Loading + Self-Registration (~300 LOC)

Summary: Implement `AgentServiceServicer`, agent loading from config, server startup/shutdown, and self-registration.

**Deliverables:**
1. `agents/server.py` — replace stubs: `AgentServiceServicer`, `load_agent`, `AgentServer.start/stop`, self-registration.
2. `tests/unit/python/test_server.py` — server tests with in-process gRPC client.
3. `tests/integration/test_agent_server.py` — integration test: submit task via gRPC, verify response.

**Dependencies:** Phase 1 (Python gRPC stubs), Phase 4 (task agents).

**Total estimated scope:** ~1,130 LOC implementation + tests (generated proto output not counted).

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | `agents/base.py` | Add `TaskInputConfig` dataclass and `config` field to `TaskInput` (Review-fix D1) |
| Python agents | `agents/server.py` | Replace stubs — `AgentServiceServicer`, `load_agent`, `AgentServer.start/stop`, self-registration |
| Python agents | `agents/coder.py` | Implement `handle()` — system prompt, LLM call, tool loop |
| Python agents | `agents/reviewer.py` | Implement `handle()` — review prompt, LLM call, structured output |
| Python agents | `agents/planner_agent.py` | Implement `handle()` — planning prompt, LLM call, plan output |
| Python agents | `agents/llm_client.py` | New — `LLMClient` wrapper around Anthropic SDK |
| Python agents | `agents/tools/builtin.py` | Replace stubs — `file_read`, `file_write`, `shell_exec`, `http_request` |
| Python agents | `agents/tools/permissions.py` | New — `PermissionGate` class |
| Python agents | `agents/tools/sandbox.py` | New — `PathValidator` class |
| Python generated | `agents/generated/task_pb2.py` | Generated — protobuf message types |
| Python generated | `agents/generated/task_pb2_grpc.py` | Generated — gRPC service stubs |
| Python generated | `agents/generated/__init__.py` | New — package marker |
| Tests | `tests/unit/python/test_agents.py` | New — task agent tests with mock LLM |
| Tests | `tests/unit/python/test_server.py` | New — gRPC server unit tests |
| Tests | `tests/unit/python/test_tools.py` | Extended — built-in tool tests |
| Tests | `tests/integration/test_agent_server.py` | New — end-to-end gRPC integration test |
| Build | `Makefile` | Add `proto-python` target |
| Python config | `agents/pyproject.toml` | Add `grpcio`, `grpcio-tools`, `protobuf`, `anthropic`, `aiohttp`, `pyyaml` |
| Docker | `Dockerfile.agent` | Update to install proto deps and run `make proto-python` |

## Test Strategy

- **Unit tests** per module using `pytest` with `asyncio_mode = "auto"`.

### Permission Gate Tests

- **Granted permission**: filesystem:read with matching glob → `True`.
- **Denied permission (default)**: no permission config → `False`.
- **Deny overrides allow (paths)**: path matches both deny and allow → denied.
- **Allow overrides deny (network)**: domain matches both deny and allow → allowed (see `is_domain_allowed` docstring for rationale). (PR-review B3)
- **Command allowlist**: allowed command → `True`; unlisted command → `False`.
- **Multi-word command allowlist**: `"git diff"` in allowlist, `args=["git", "diff", "f.py"]` → `True`; `args=["git", "push"]` → `False`. (Review-fix B1)
- **Domain allowlist**: allowed domain → `True`; wildcard deny with specific allow → `True`; deny all → `False`.

### Path Validator Tests

- **Valid read path**: path within read glob → resolved `Path` returned.
- **Path traversal blocked**: `../../etc/passwd` → `PermissionError`.
- **Symlink resolution**: symlink pointing outside workspace → `PermissionError`.
- **Deny list precedence**: path in both allow and deny → `PermissionError`.
- **Write to read-only path**: write mode on read-only glob → `PermissionError`.

### Built-in Tool Tests

- **`file_read`**: read existing file → content returned; read denied path → `ToolResult(success=False)`.
- **`file_write`**: write file → content on disk; write denied path → `ToolResult(success=False)`.
- **`shell_exec`**: run allowed command (e.g., `echo test`) → stdout returned; denied command → blocked; timeout → killed.
- **`http_request`**: mock HTTP server → response returned; denied domain → blocked.
- **`http_request` URL validation**: malformed URL (no hostname) → `ToolResult(success=False)`; `file://` scheme → `ToolResult(success=False)`. (Review-fix S1)

### Task Agent Tests

- **Successful handle**: mock LLM returns text response → `TaskOutput(status=COMPLETED)`.
- **Tool-use loop**: mock LLM returns tool call → tool dispatched → result sent back → final response.
- **Max iterations**: mock LLM always returns tool calls → `TaskOutput(status=FAILED, result="Max LLM call iterations exceeded")`.
- **Max tokens truncation**: mock LLM returns `stop_reason="max_tokens"` → `TaskOutput(status=FAILED, result="LLM response truncated: max_tokens limit reached")`. (Review-fix B2)
- **Per-task config override**: mock LLM with `TaskInputConfig(max_llm_calls=2)` → agent uses 2 iterations, not YAML default. (Review-fix D1)
- **LLM error**: mock LLM raises exception → `TaskOutput(status=FAILED)`.
- **Token counting**: verify `metadata["tokens_used"]` from mock responses.

### Server Tests

- **`ExecuteTask` success**: in-process gRPC client → `TaskResponse` with `COMPLETED`.
- **`ExecuteTask` agent not found**: unknown agent ID → `NOT_FOUND` gRPC status.
- **`ExecuteTask` timeout**: blocking agent → `DEADLINE_EXCEEDED`.
- **`ExecuteTask` agent failure**: agent raises exception → `FAILED` response (not gRPC error).
- **`HealthCheck`**: loaded agent healthy → `SERVING`; agent `health_check()` returns `False` → `NOT_SERVING`.
- **`ExecuteTaskStream`**: → `UNIMPLEMENTED`.
- **Graceful shutdown**: signal → server stops cleanly.
- **Agent loading**: valid YAML → correct agent type instantiated; missing agent ID → `ValueError`.

### Integration Tests

- **End-to-end task execution**: start agent server in-process, send `TaskRequest` via gRPC client, verify `TaskResponse` with expected output from mock LLM.
- **Build smoke test**: `pip install -e ".[dev]"` succeeds, `make proto-python` succeeds.

## Open Questions

1. **LLM SDK choice**: The Anthropic Python SDK is the default based on `config/agents.yaml` model references (`claude-sonnet-4-20250514`). Should the `LLMClient` abstraction support multiple providers (e.g., OpenAI, Ollama) in v0.1 or is Anthropic-only acceptable?

2. **Agent config `type` field**: Current resolution uses capability-based heuristics. Should `config/agents.yaml` add an explicit `type: task | persona` field? Requires schema change.

3. **Workspace root**: Built-in tools need a workspace root path for sandbox validation. Should this be a CLI flag (`--workspace /path`), derived from the orchestrator's `--workflows-dir`, or hardcoded to `/workspace` for Docker? *Resolved: `--workspace` CLI flag (default: `/workspace` for Docker, CWD for local dev). Used by both `PathValidator` and `shell_exec` CWD. (Review-fix D5)*

4. **`aiohttp` dependency**: `http_request` tool and self-registration use `aiohttp`. Is this acceptable or should we use the stdlib `urllib` (sync, wrapped in `asyncio.to_thread`)? `aiohttp` is a more natural fit for the async-first architecture.

5. **Self-registration timing**: The agent registers itself with the orchestrator after the gRPC server starts listening. If the orchestrator is not yet running, registration fails silently. Should the agent retry registration with backoff?

## Decision / Next Steps

Once this RFC is accepted:

1. Create feature branches per a PR plan (`0004-pr-plan.md`).
2. Implement in phase order (Proto Gen → Permissions/Sandbox → Tools → Agents → Server).
3. PR < 500 lines per phase; squash merge to `main`.
4. **Next RFC**: v0.1 integration testing and end-to-end smoke test workflow, or Rust CLI implementation to complete the v0.1 MVP path.

## Related Documentation

- [ai-agents-orchestration-spec.md](../ai-agents-orchestration-spec.md) — §2.1–2.3 Agent Architecture, §3.1–3.2 gRPC Communication, §4.1 Task Agents, §5.1 Tool System
- [orchestr8-extension-spec.md](../orchestr8-extension-spec.md) — v0.2 persona agents, memory, sub-agents
- [0001-core-orchestration-pipeline.md](0001-core-orchestration-pipeline.md) — State, Registry, Planner
- [0002-rest-api-server.md](0002-rest-api-server.md) — REST API, agent registration
- [0003-scheduler-executor.md](0003-scheduler-executor.md) — Scheduler, GRPCExecutor (Go client side)
- [BRANCHING.md](../BRANCHING.md) — Branch naming and PR size guidelines
- Existing stubs: `agents/server.py`, `agents/base.py`, `agents/coder.py`, `agents/reviewer.py`, `agents/planner_agent.py`, `agents/tools/builtin.py`
- Proto definitions: `proto/task.proto`
- Agent config: `config/agents.yaml`
