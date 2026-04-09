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

Dependencies added to `pyproject.toml`: `grpcio >= 1.62`, `grpcio-tools >= 1.62`, `protobuf >= 5.26`.

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
                    **{k: str(v) for k, v in output.metadata.items()},
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
        return task_pb2.HealthCheckResponse(
            status=task_pb2.SERVING,
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

The servicer catches all exceptions from `agent.handle()` and converts them to `FAILED` `TaskResponse` — the gRPC call itself never fails with an application error, which prevents the Go Executor from retrying a task that legitimately failed (as opposed to a transient gRPC transport error).

### Agent Loading

**File:** `agents/server.py` — `load_agent()` function

```python
def load_agent(agent_id: str, config_path: str) -> BaseAgent:
    """Load an agent by ID from YAML config."""
    with open(config_path) as f:
        config = yaml.safe_load(f)

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

A `type` field in `agents.yaml` would be cleaner; the capability-based heuristic is a v0.1 pragmatic choice since adding a `type` field requires a schema change and config migration. A `# TODO(v0.2): add explicit 'type' field to agent config` comment marks this.

### Task Agent Implementation

All three task agents follow the same pattern:

1. Build a system prompt from `self.config["role"]` and agent-specific instructions.
2. Initialize an LLM client using `self.config["model"]` and `self.config["temperature"]`.
3. Send the task payload + context to the LLM.
4. Handle tool-use responses: if the LLM returns tool calls, dispatch them through the tool registry, collect results, and send them back to the LLM.
5. Repeat the tool-use loop until the LLM returns a final text response or the max iteration limit is reached.
6. Return a `TaskOutput` with the final result and metadata (tokens used, tool calls count).

```python
async def handle(self, task: TaskInput) -> TaskOutput:
    messages = self._build_messages(task)
    tools = self._get_tool_definitions()
    total_tokens = 0
    tool_calls_count = 0

    for _ in range(self.config.get("max_llm_calls", 10)):
        response = await self._llm_client.create_message(
            model=self.config["model"],
            messages=messages,
            tools=tools,
            max_tokens=self.config.get("max_tokens", 4096),
            temperature=self.config.get("temperature", 0.3),
        )
        total_tokens += response.usage.input_tokens + response.usage.output_tokens

        if response.stop_reason == "end_turn":
            return TaskOutput(
                status=TaskStatus.COMPLETED,
                result=self._extract_text(response),
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
    if not permission_gate.is_command_allowed(args[0]):
        return ToolResult(success=False, error=f"Command not allowed: {args[0]}", error_type="permanent")

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return ToolResult(success=False, error=f"Command timed out after {timeout}s", error_type="transient")

    return ToolResult(
        success=proc.returncode == 0,
        data=stdout.decode(errors="replace"),
        error=stderr.decode(errors="replace") if proc.returncode != 0 else None,
    )
```

**Security:** Commands are executed via `create_subprocess_exec` (not `shell=True`), are split with `shlex.split`, and must match the `allowed_commands` list from the agent's `permissions.shell` config. Only the command base name (first arg) is matched against the allowlist — arguments are unrestricted. A `# TODO(v0.2): argument pattern matching for shell commands` marks this gap.

#### `http_request`

```python
@tool(name="http_request", description="Make an HTTP request", permissions=["network:http"])
async def http_request(url: str, method: str = "GET", body: str = "") -> ToolResult:
    parsed = urllib.parse.urlparse(url)
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

    def is_command_allowed(self, command: str) -> bool:
        """Check shell command against allowed_commands list."""
        allowed = self._permissions.get("shell", {}).get("allowed_commands", [])
        return command in allowed

    def is_domain_allowed(self, domain: str) -> bool:
        """Check domain against network allow/deny lists."""
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

1. Parse CLI args (`--agent`, `--port`, `--host`, `--config`).
2. Load agent config from YAML.
3. Instantiate the agent with `load_agent(agent_id, config_path)`.
4. Initialize the gRPC async server (`grpc.aio.server()`).
5. Register `AgentServiceServicer` with the loaded agent.
6. Bind to `host:port` with insecure port (no TLS in v0.1; `# TODO(security): enable TLS`).
7. Start serving with graceful shutdown on `SIGTERM` / `SIGINT`.

Single-agent-per-process model: Each agent process registers with the orchestrator via `POST /api/v1/agents/register` at startup (or is pre-registered in `docker-compose.yaml` env). The registration includes the agent's `host:port` address for the Executor to dial.

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
                "address": f"{host}:{port}",
                "capabilities": self.agents[agent_id].capabilities,
                "status": "healthy",
            },
        )
```

The `--orchestrator-url` CLI flag (default: `http://127.0.0.1:8080`) configures the target. Self-registration is best-effort — if the orchestrator is unreachable, the agent logs a warning and continues serving. The orchestrator admin can manually register agents via the REST API.

## Security Considerations

### gRPC Without TLS (v0.1)

Consistent with RFC 0003's Go Executor: `grpc.aio.server()` with insecure port for v0.1/localhost/docker-compose use. A `# TODO(security): enable TLS for production gRPC` comment is placed at the server creation call. Production deployments MUST enable TLS or mTLS.

### LLM API Key Handling

The Anthropic API key is read from the `ANTHROPIC_API_KEY` environment variable. It is never logged, stored in config files, or included in `TaskResponse.metadata`. The agent process inherits the env var from `docker-compose.yaml` or the operator's shell.

### Tool Sandboxing

- **Filesystem:** All paths resolved to absolute form via `Path.resolve()`, then checked against allow/deny globs. Deny list takes precedence. `.env` and `.git/` are denied by default in agent config.
- **Shell:** Commands split via `shlex.split()` (no shell injection via `shell=True`). Only allowlisted command base names are permitted. Execution timeout enforced via `asyncio.wait_for`.
- **Network:** HTTP requests check domain against allow/deny lists before connecting. `deny: ["*"]` + `allow: ["api.anthropic.com"]` is the v0.1 default — agents can only reach the LLM API.

### Input Validation

`TaskRequest.payload` and `TaskRequest.context` are treated as opaque strings passed to the LLM. The agent does not eval, exec, or interpret these values beyond including them in the LLM prompt. Tool outputs are also passed as strings back to the LLM. This prevents prompt injection from executing arbitrary code on the agent host — code execution only happens through the sandboxed `shell_exec` tool with its allowlist.

### No Authentication (v0.1)

The gRPC server accepts connections from any client on its bound address. In v0.1 the agent binds to `127.0.0.1` (loopback) by default. The `--host 0.0.0.0` flag is used in Docker containers where the agent needs to be reachable from the orchestrator container. Authentication is deferred to the security RFC.

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
- **Deny overrides allow**: path matches both deny and allow → denied.
- **Command allowlist**: allowed command → `True`; unlisted command → `False`.
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

### Task Agent Tests

- **Successful handle**: mock LLM returns text response → `TaskOutput(status=COMPLETED)`.
- **Tool-use loop**: mock LLM returns tool call → tool dispatched → result sent back → final response.
- **Max iterations**: mock LLM always returns tool calls → `TaskOutput(status=FAILED, result="Max LLM call iterations exceeded")`.
- **LLM error**: mock LLM raises exception → `TaskOutput(status=FAILED)`.
- **Token counting**: verify `metadata["tokens_used"]` from mock responses.

### Server Tests

- **`ExecuteTask` success**: in-process gRPC client → `TaskResponse` with `COMPLETED`.
- **`ExecuteTask` agent not found**: unknown agent ID → `NOT_FOUND` gRPC status.
- **`ExecuteTask` timeout**: blocking agent → `DEADLINE_EXCEEDED`.
- **`ExecuteTask` agent failure**: agent raises exception → `FAILED` response (not gRPC error).
- **`HealthCheck`**: → `SERVING`.
- **`ExecuteTaskStream`**: → `UNIMPLEMENTED`.
- **Graceful shutdown**: signal → server stops cleanly.
- **Agent loading**: valid YAML → correct agent type instantiated; missing agent ID → `ValueError`.

### Integration Tests

- **End-to-end task execution**: start agent server in-process, send `TaskRequest` via gRPC client, verify `TaskResponse` with expected output from mock LLM.
- **Build smoke test**: `pip install -e ".[dev]"` succeeds, `make proto-python` succeeds.

## Open Questions

1. **LLM SDK choice**: The Anthropic Python SDK is the default based on `config/agents.yaml` model references (`claude-sonnet-4-20250514`). Should the `LLMClient` abstraction support multiple providers (e.g., OpenAI, Ollama) in v0.1 or is Anthropic-only acceptable?

2. **Agent config `type` field**: Current resolution uses capability-based heuristics. Should `config/agents.yaml` add an explicit `type: task | persona` field? Requires schema change.

3. **Workspace root**: Built-in tools need a workspace root path for sandbox validation. Should this be a CLI flag (`--workspace /path`), derived from the orchestrator's `--workflows-dir`, or hardcoded to `/workspace` for Docker?

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
