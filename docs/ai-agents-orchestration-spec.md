# Persatrix — AI Agents Orchestration Framework — MVP Specification

> **Read this first: an April 2026 design, not a description of what is built.**
> This document was written before most of the code existed and has not been
> kept in step with it. Much of what it says in the present tense was never
> built, was built differently, or arrived later under another name. That
> includes security controls, so do not use it to judge what protects a
> deployment.
>
> For what Persatrix does today, read the guides
> ([channels](guides/channels.md), [persona agents](guides/persona-agents.md),
> [authentication](guides/auth.md), [model providers](guides/model-providers.md)),
> [Observability](observability.md), and [ROADMAP.md](../ROADMAP.md), which
> says of each component whether it is built. [FILEMAP.md](../FILEMAP.md)
> lists the files that exist.
>
> Notes inside the sections below, marked "Planned" or dated, correct the
> claims that matter most, the security ones first. A section with no note
> has not been checked against the code.

## 1. Vision

A general-purpose **agent society engine** — a runtime for creating, connecting,
and observing groups of AI agents that behave as individuals within
organizational or social structures. Agents have rich personas, communicate
through multi-channel systems (internal and external), and can be organized into
hierarchies, teams, or flat networks. Use cases range from software development
teams, to business simulations, to social science experiments.

> See `persatrix-extension-spec.md` for the full Agent Societies extension
> covering personas, organizational topologies, communication architecture,
> external bridges (email, Slack, Discord, Telegram), interaction protocols,
> observer/experiment controls, and use case blueprints.

---

## 2. Core Concepts

### 2.1 Agent

The framework supports two agent types that share a common base:

**Task Agent** — a stateless specialist that receives a task and returns a result.
Used in v0.1 workflows and as ephemeral sub-agents. Defined by:
- **Identity** — unique ID, name, role description, and capability tags
- **System prompt** — defines constraints and expertise
- **Tool access** — a set of tools/functions the agent can invoke
- **Config** — model, temperature, max_retries, timeout

**Persona Agent** (v0.2+) — an autonomous individual with personality, goals,
relationships, and memory. Extends task agent with:
- **Persona** — background, personality traits, communication style, goals
- **Relationships** — trust scores and interaction history with other agents
- **Autonomy level** — passive, reactive, semi-autonomous, autonomous, supervisor
- **Memory** — short-term (context window) and long-term (episodic, relational)
- **Sub-agent spawning** — ability to create ephemeral task agents for atomic work

Both types implement the same base interface and are registered in the same
registry. The orchestrator treats them uniformly for scheduling and communication.

### 2.2 Orchestrator
The central coordinator that:
- Decomposes a user goal into sub-tasks
- Selects and assigns agents to sub-tasks
- Manages execution order (sequential, parallel, or conditional)
- Aggregates results and handles failures

### 2.3 Task
A unit of work with:
- Input payload (text, structured data, files)
- Assigned agent(s)
- Status lifecycle: `pending → running → completed | failed | cancelled`
- Output payload and metadata (tokens used, latency, retries)

### 2.4 Message Bus
The communication backbone. Agents and the orchestrator exchange structured messages:
- **Request** — orchestrator → agent (task assignment)
- **Response** — agent → orchestrator (task result)
- **Event** — broadcast notifications (agent started, completed, error)
- **Delegation** — agent → orchestrator (agent requests help from another agent)

### 2.5 Workflow
A directed acyclic graph (DAG) of tasks. Supports:
- Sequential chains (A → B → C)
- Parallel fan-out / fan-in (A → [B, C, D] → E)
- Conditional branching (if result X → path A, else → path B)
- Loops with exit conditions (retry up to N times)

---

## 3. Architecture

### 3.1 Technology Split

| Layer              | Language | Rationale                                      |
|--------------------|----------|-------------------------------------------------|
| Orchestrator core  | Go       | High concurrency, low latency, strong typing    |
| Agent runtime      | Python   | Rich AI/ML ecosystem, LLM SDK support           |
| CLI / Dev tools    | Rust     | Fast tooling, single-binary distribution         |
| Communication      | gRPC     | Cross-language, schema-enforced, bidirectional   |
| Config & workflows | YAML     | Human-readable, version-controllable             |

### 3.2 Component Diagram

```
┌──────────────────────────────────────────────────────────┐
│                      CLI (Rust)                           │
│   run · validate · test · agents · status · logs · mesh  │
└────────────────────────────┬─────────────────────────────┘
                             │ REST / gRPC / SSE
┌────────────────────────────▼─────────────────────────────┐
│                  Orchestrator (Go)                         │
│  ┌──────────┐ ┌──────────┐ ┌───────────┐ ┌────────────┐ │
│  │ Planner  │ │Scheduler │ │  State    │ │  Registry  │ │
│  └──────────┘ └──────────┘ └───────────┘ └────────────┘ │
│  ┌──────────┐ ┌──────────┐ ┌───────────┐ ┌────────────┐ │
│  │Resilience│ │Telemetry │ │  Cost     │ │  Security  │ │
│  │(circuits,│ │(OTEL)    │ │ Tracking  │ │(perms,rate │ │
│  │ fallback)│ │          │ │           │ │ limit,audit│ │
│  └──────────┘ └──────────┘ └───────────┘ └────────────┘ │
│                                                           │
│  ┌─ v0.2+ ──────────────────────────────────────────────┐│
│  │ Channels · Protocols · Bridges · Org Model · Mesh    ││
│  └──────────────────────────────────────────────────────┘│
└──────┬──────────┬──────────┬────────────────────────────┘
       │ gRPC     │ gRPC     │ gRPC
 ┌─────▼───┐ ┌────▼────┐ ┌──▼──────────┐
 │ Agent A │ │ Agent B │ │  Agent C    │
 │ (Python)│ │ (Python)│ │  (Python)   │
 │ Task or │ │ Task or │ │  Task or    │
 │ Persona │ │ Persona │ │  Persona    │
 │         │ │         │ │  + subs     │
 └─────────┘ └─────────┘ └─────────────┘
```

> The v0.2+ row is inactive in MVP but the interfaces are designed to
> accommodate it. See extension spec E13 for the full distributed diagram.

---

## 4. Data Models

### 4.1 Agent Definition (YAML)

Agents use a single schema. Task agents use the base fields only; persona
agents add the optional `persona` and `autonomy` sections.

```yaml
# ─── Task Agent (v0.1 — minimal) ──────────────────────────
agent:
  id: "code-writer"
  name: "Code Writer"
  role: "Writes clean, tested code from specifications"
  model: "quality"                     # alias (RFC 0033), not a vendor id
  temperature: 0.3
  capabilities:
    - code_generation
    - code_review
    - unit_testing
  tools:
    - file_read
    - file_write
    # shell_exec is left off: §6.2 gives this agent no shell command
  max_retries: 2
  timeout_seconds: 120
```

```yaml
# ─── Persona Agent (v0.2+ — extends base) ─────────────────
agent:
  id: "ember-owl"
  name: "Ember Owl"
  role: "Engineering leadership and technical oversight"
  model: "quality"
  temperature: 0.7                     # higher for personality variance
  capabilities:
    - architecture_review
    - sprint_planning
    - team_management
  tools:
    - file_read
    - mcp:github
  max_retries: 2
  timeout_seconds: 300

  # ─── Persona (optional, makes this a persona agent) ─────
  persona:
    title: "VP of Engineering"
    background: "15 years in software engineering..."
    personality:
      traits: [pragmatic, direct, collaborative]
      communication_style: "concise and structured"
      decision_making: "data-driven"
    goals:
      primary: "Ship v2.0 on time with acceptable quality"
      secondary: ["Reduce tech debt by 20%"]
    knowledge:
      domains: ["system design", "team management", "Python", "Go"]
      limitations: ["frontend/CSS", "ML internals"]

  # ─── Autonomy (optional, defaults to "reactive") ────────
  autonomy:
    level: "semi-autonomous"
    can_initiate_conversations: true
    can_delegate_tasks: true
    can_spawn_sub_agents: true
    requires_approval_for: ["external_communications"]

  # ─── Relationships (optional) ───────────────────────────
  relationships:
    - agent_id: "iron-fox"
      type: "reports_to_me"
      trust_level: 0.9
```

**Compatibility rule**: if `persona` is absent, the agent is a task agent and
uses the synchronous `handle()` interface. If `persona` is present, the agent
is a persona agent and uses the async event-driven interface (see §8).

### 4.2 Workflow Definition (YAML)

```yaml
workflow:
  id: "feature-builder"
  name: "Build a Feature End-to-End"
  trigger: "manual"

  steps:
    - id: "plan"
      agent: "planner"
      input: "{{ user_request }}"
      output_key: "plan"

    - id: "implement"
      agent: "code-writer"
      input: "{{ steps.plan.output }}"
      output_key: "code"
      depends_on: ["plan"]

    - id: "review"
      agent: "code-reviewer"
      input: "{{ steps.implement.output }}"
      output_key: "review"
      depends_on: ["implement"]

    - id: "revise"
      agent: "code-writer"
      input: "{{ steps.implement.output }} \n Feedback: {{ steps.review.output }}"
      output_key: "final_code"
      depends_on: ["review"]
      condition: "{{ steps.review.output.approved == false }}"
```

### 4.3 Message Schemas (Protobuf)

The system uses two message schemas for distinct purposes:

**TaskMessage** — orchestrator ↔ agent communication for workflow task execution.
Used in v0.1 for all interactions, and in v0.2+ for structured task assignment
and result reporting.

```protobuf
syntax = "proto3";

message TaskMessage {
  string task_id       = 1;
  string workflow_id   = 2;
  string agent_id      = 3;
  string parent_task   = 4;
  TaskStatus status    = 5;
  string input         = 6;
  string output        = 7;
  map<string, string> metadata = 8;
  int64 created_at     = 9;
  int64 updated_at     = 10;
}

enum TaskStatus {
  PENDING   = 0;
  RUNNING   = 1;
  COMPLETED = 2;
  FAILED    = 3;
  CANCELLED = 4;
}
```

**AgentMessage** (v0.2+) — agent ↔ agent communication for conversations in
channels, DMs, and meetings. Defined in the extension spec (E5.3). Carries
natural language content, message types (decision, question, escalation), and
visibility controls.

**Relationship**: TaskMessage flows vertically (orchestrator ↔ agent).
AgentMessage flows horizontally (agent ↔ agent via the communication layer).
Both transit over gRPC internally. External A2A protocol messages are translated
to AgentMessage at the A2A bridge boundary using HTTP/JSON-RPC inbound and gRPC
internally.

---

## 5. Tool System & MCP Support

### 5.1 Tool Architecture

Agents interact with the outside world through **tools** — typed functions with declared inputs, outputs, and permissions. The framework supports three tool tiers:

#### Tier 1: Built-in Tools
Bundled with the framework, always available:

| Tool          | Description                                    |
|---------------|------------------------------------------------|
| `file_read`   | Read file contents (with path restrictions)    |
| `file_write`  | Write files to sandboxed workspace             |
| `shell_exec`  | Run shell commands (sandboxed, allowlisted)    |
| `http_request`| Make HTTP calls (domain-allowlisted)           |
| `store_get`   | Read from key-value execution context          |
| `store_set`   | Write to key-value execution context           |

> 2026-11-14 — what is built: the first four exist
> ([`agents/tools/builtin.py`](../agents/tools/builtin.py)). `store_get` and
> `store_set` do not, and nothing takes their place. None is "always
> available": an agent runs only the tools on its `tools` list, and each
> needs its permission. For "sandboxed", see the note under
> [6.3](#63-sandboxing).

#### Tier 2: Custom Tools
User-defined tools registered via Python functions:

```python
from Persatrix.tools import tool, ToolResult

@tool(
    name="query_database",
    description="Run a read-only SQL query against the app database",
    permissions=["db:read"],
)
def query_database(query: str, database: str = "main") -> ToolResult:
    # implementation
    return ToolResult(success=True, data=rows)
```

> 2026-11-14 — two corrections to this example. The import is not the real
> one: the decorator and `ToolResult` are in
> [`agents/tools/registry.py`](../agents/tools/registry.py), and the package
> installs as `persatrix_agents`
> (`from persatrix_agents.tools.registry import tool, ToolResult`). And
> `permissions=["db:read"]` protects nothing: a custom tool's declared
> permissions are stored and never checked, so the tool runs for any agent
> that lists it ([ISSUE-0214](issues/ISSUE-0214-declared-tool-permissions-are-never-enforced.md)).

#### Tier 3: MCP Tools (External Servers)
Tools exposed by external MCP-compatible servers (see 5.2).

### 5.2 MCP (Model Context Protocol) Support

> **Status:** 📋 **Planned** — not built yet; this section is the design.
> [MCP Bridge](ai-glossary.md#mcp-bridge) in the glossary says what exists today.

The framework acts as an **MCP client** — it connects to external MCP servers and exposes their tools to agents. This is a first-class integration, not a plugin.

#### How It Works

```
┌─────────────┐         ┌──────────────────┐
│   Agent     │         │  MCP Server      │
│  (Python)   │         │  (any language)  │
│             │  gRPC   │                  │
│  uses tool ─┼────────►│  Filesystem      │
│             │         │  GitHub          │
│             │◄────────┤  Database        │
│             │  result │  Slack           │
└─────────────┘         │  Browser         │
                        └──────────────────┘
```

#### MCP Configuration (YAML)

```yaml
mcp_servers:
  - id: "github"
    transport: "stdio"
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_TOKEN: "${GITHUB_TOKEN}"
    allowed_tools:              # whitelist specific tools
      - "create_pull_request"
      - "search_repositories"
      - "get_file_contents"
    denied_tools: []            # or blacklist

  - id: "filesystem"
    transport: "stdio"
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
    sandbox:
      allowed_paths: ["/workspace"]
      read_only: false

  - id: "custom-api"
    transport: "sse"
    url: "http://localhost:8080/mcp"
    auth:
      type: "bearer"
      token: "${CUSTOM_API_TOKEN}"
```

#### Agent ↔ MCP Binding

Agents declare which MCP servers they can access:

```yaml
agent:
  id: "code-writer"
  tools:
    - file_read              # built-in
    - file_write             # built-in
    - mcp:github             # all allowed tools from GitHub MCP server
    - mcp:filesystem         # all tools from filesystem MCP server
    - mcp:custom-api/search  # only the "search" tool from custom-api
```

#### MCP Lifecycle Management

The orchestrator manages MCP server processes:
1. **Lazy start** — MCP servers launch when first needed by an agent
2. **Connection pooling** — multiple agents share one MCP server instance
3. **Health checks** — ping servers periodically, restart on failure
4. **Graceful shutdown** — terminate servers when workflow completes

### 5.3 Tool Execution Flow

```
Agent LLM response includes tool_use block
       │
       ▼
Tool Router (in agent runtime)
       │
       ├─ Built-in tool? ──► Execute locally (sandboxed)
       ├─ Custom tool?   ──► Call registered Python function
       └─ MCP tool?      ──► Forward to MCP server via stdio/SSE
       │
       ▼
Permission check (see Section 6)
       │
       ▼
Execute + capture result
       │
       ▼
Return tool_result to LLM for next reasoning step
```

---

## 6. Security Model

### 6.1 Threat Model

| Threat                         | Risk   | Description                                                   |
|--------------------------------|--------|---------------------------------------------------------------|
| Prompt injection via tool output | High   | Malicious data from tools manipulates agent behavior          |
| Agent escape / privilege escalation | High | Agent accesses tools or data beyond its permissions           |
| Secrets leakage                | High   | API keys, tokens leak into logs, outputs, or LLM context     |
| Denial of service              | Medium | Runaway agent consumes unbounded resources                    |
| Data exfiltration              | Medium | Agent sends sensitive data to unauthorized external endpoints |
| Cross-agent contamination      | Medium | One agent's compromised output poisons another agent's input  |
| MCP server compromise          | Medium | Malicious or buggy MCP server returns harmful data/commands   |

### 6.2 Permission System

Every tool invocation passes through a **permission gate**. Permissions are declared per-agent and enforced at runtime.

```yaml
agent:
  id: "code-writer"
  permissions:
    filesystem:
      read: ["/workspace/src/**", "/workspace/tests/**"]
      write: ["/workspace/src/**"]
      deny: ["**/.env*", "**/.git/**", "**/node_modules/**"]
    network:
      allow: ["api.anthropic.com", "api.openai.com"]
      deny: ["*"]                # deny-by-default for all other domains
    shell:
      allowed_commands: []       # none: a listed command is trusted with every argument
      deny_commands: ["rm -rf", "curl", "wget", "sudo"]
      max_execution_seconds: 30
    mcp:
      allowed_servers: ["github", "filesystem"]
      denied_servers: ["*"]
```

**Enforcement rules:**
- Deny-by-default: if a permission isn't explicitly granted, it's denied
- Path-based restrictions use glob patterns, resolved to absolute paths (no symlink traversal)
- Shell commands are parsed and validated before execution — no raw shell passthrough
- All permission violations are logged as security events

> 2026-11-12 — how the built rules match, which the bullets above leave out
> ([ISSUE-0196](issues/ISSUE-0196-shipped-shell-allowlist-voids-the-file-and-network-rules.md),
> [ISSUE-0216](issues/ISSUE-0216-path-globs-match-at-any-depth.md)):
>
> - **Paths.** A rule is matched against the whole resolved path, with
>   Python's `fnmatchcase`. `*` matches any run of characters, `/` included,
>   so it crosses folders: `/workspace/*.py` also matches
>   `/workspace/deploy/hooks/post_install.py`, and `**` means the same as
>   `*`. `?` matches any one character and `[...]` any one of the characters
>   listed, with no escape character: `/workspace/keys[1].txt` matches
>   `/workspace/keys1.txt` and not the path it spells, which is written
>   `/workspace/keys[[]1].txt`. A rule with none of `*`, `?` and `[` is one
>   exact path: `/workspace/.env` does not cover `/workspace/sub/.env`.
>   `deny` wins over `read` and `write`. The match is case-sensitive, so
>   where file names are not (macOS and Windows by default) a deny rule
>   misses the same file spelled in another case
>   ([ISSUE-0234](issues/ISSUE-0234-deny-rules-miss-a-path-spelled-in-another-case.md)).
> - **Shell.** A command is checked on its leading words only, never on its
>   arguments: `git diff` admits `git diff main` and not `git push`. So a
>   listed command is trusted with every argument it accepts, and it runs
>   outside the path rules. An interpreter such as `python` can run any
>   code, read any file the agent's user can, and open any connection. The
>   example above lists none; `config/agents.yaml` shows a second example
>   for an agent that trusts its workspace.

> 2026-11-14 — two more corrections to this section
> ([ISSUE-0227](issues/ISSUE-0227-canonical-specs-describe-unbuilt-features-as-built.md)):
>
> - **Two parts of the example are not built**, and a config copied from it
>   fails `make validate`: `shell.deny_commands` and the whole `mcp` block
>   are keys [`schemas/agent.schema.json`](../schemas/agent.schema.json)
>   refuses.
> - **A refused permission is not a security event.** The agent writes a
>   warning to its own log and tells the model the call was denied. Nothing
>   is written to the audit log ([6.6](#66-audit-trail)).
> - **Not every tool passes through the gate.** Each built-in tool checks
>   the gate in its own code. Nothing checks it for a custom tool, whatever
>   permissions that tool declares ([ISSUE-0214](issues/ISSUE-0214-declared-tool-permissions-are-never-enforced.md)).

### 6.3 Sandboxing

| Layer               | Mechanism                                              |
|---------------------|--------------------------------------------------------|
| Process isolation   | Each agent runs in its own process (MVP) / container (post-MVP) |
| Filesystem sandbox  | `chroot` or bind-mount to workspace directory only     |
| Network restrictions| Egress proxy with domain allowlist                     |
| Resource limits     | CPU time, memory, and output size caps per task        |
| Shell sandboxing    | Command allowlist + argument validation, no shell=True |

MVP sandboxing targets:

```yaml
resource_limits:
  per_task:
    max_llm_calls: 20            # prevent infinite tool loops
    max_tokens_total: 100000     # budget cap
    max_execution_seconds: 300   # hard timeout
    max_output_size_bytes: 5242880  # 5MB
  per_tool_call:
    max_execution_seconds: 30
    max_output_size_bytes: 1048576  # 1MB
```

> 2026-11-04 — these were MVP targets. What is built is per agent: the
> per-task call cap is `max_llm_calls`, the wall time of a shell command is
> `permissions.shell.max_execution_seconds`, and RFC 0009 Phase 3's
> `resource_limits` block sets a CPU limit on each process of a command
> (`shell_cpu_seconds`)
> and a cap on any tool's output (`tool_output_bytes`), each off until set;
> `schemas/agent.schema.json` has the ranges.

> 2026-11-14 — what is built of the table at the top of this section:
>
> - **Process isolation**: built. Each agent is its own process, and in the
>   shipped compose stack its own container.
> - **Filesystem sandbox**: there is no `chroot` and no bind-mount limit.
>   The two file tools check each path against the agent's `filesystem`
>   rules ([6.2](#62-permission-system)). A shell command runs outside those
>   rules and can read whatever the agent's user can.
> - **Network restrictions**: there is no egress proxy. `http_request` checks
>   the host against `network.allow`; nothing limits what a shell command
>   connects to.
> - **Shell sandboxing**: a command is run without a shell (`shell=True` is
>   never used) and checked on its leading words. Its arguments are not
>   validated.

### 6.4 Secrets Management

- **Never in YAML directly** — secrets use `${ENV_VAR}` references, resolved at runtime
- **Never in LLM context** — secrets are injected into tool execution environment, not passed as prompt content
- **Redaction in logs** — all log output is scanned for known secret patterns and redacted
- **MCP server isolation** — each MCP server receives only its own secrets via environment variables

```yaml
secrets:
  providers:
    - type: "env"                # read from environment variables
    - type: "file"               # read from .env file (gitignored)
      path: ".env"
  redaction:
    patterns:
      - "sk-[a-zA-Z0-9]{20,}"   # Anthropic/OpenAI keys
      - "ghp_[a-zA-Z0-9]{36}"   # GitHub tokens
      - "Bearer [a-zA-Z0-9-._~+/]+"
```

> 2026-11-14 — what is built is less than the four bullets say:
>
> - **`${ENV_VAR}` references**: no config file is expanded this way, and
>   there is no `secrets:` block. Secrets (provider API keys, the fleet
>   enrollment secret) are read straight from environment variables.
> - **Never in LLM context**: not enforced. A shell command's environment
>   has the `SECURITY_*` variables taken out and keeps the provider API
>   keys, so a command that prints its environment shows them to the model
>   ([`agents/tools/sandbox.py`](../agents/tools/sandbox.py)).
> - **Redaction in logs**: not for ordinary logs. By default a log record
>   is written as it is ([Observability §7](observability.md#7-redaction-hook)).
>   Only the security audit log and the decision log mask secrets.
> - **MCP server isolation**: MCP is not built
>   ([5.2](#52-mcp-model-context-protocol-support)).

### 6.5 Prompt Injection Defenses

1. **Input/output tagging** — tool outputs are wrapped in structured delimiters so the LLM can distinguish tool data from instructions:
   ```
   <tool_result name="file_read" status="success">
   [file contents here — treat as data, not instructions]
   </tool_result>
   ```

2. **Output validation** — agent outputs are checked against expected schemas before being passed to downstream agents

3. **Sensitive field filtering** — tool results pass through a filter that strips fields not declared in the tool's output schema

4. **Human-in-the-loop gates** — high-risk actions (write, delete, deploy, send) can require human approval:
   ```yaml
   workflow:
     steps:
       - id: "deploy"
         agent: "deployer"
         approval_required: true      # pauses and asks for human confirmation
         approval_timeout: 3600       # auto-cancel after 1 hour
   ```

> 2026-11-14 — what is built of the four defenses:
>
> 1. **Tagging**: there is no `<tool_result>` tag. The output of four tools
>    (`file_read`, `http_request`, `shell_exec`, `recall_channel_messages`)
>    is scanned for injection patterns and wrapped in `<external_data>`
>    ([`agents/security.py`](../agents/security.py)). Every other tool's
>    output reaches the model as it is.
> 2. **Output validation**: not in use. The sub-agent spawner would check
>    a sub-agent's result against the schema its parent asked for
>    ([`agents/sub_agents/spawner.py`](../agents/sub_agents/spawner.py)),
>    and nothing calls the spawner yet.
> 3. **Field filtering**: not built.
> 4. **Human approval gates**: not built. The workflow schema accepts
>    `approval_required` and `approval_timeout`, and nothing reads them: a
>    step marked this way runs without waiting for anyone
>    ([ISSUE-0202](issues/ISSUE-0202-workflow-condition-and-approval-validate-then-do-nothing.md)).

### 6.6 Audit Trail

Every action is logged to an append-only audit log:

```json
{
  "timestamp": "2026-04-06T12:34:56Z",
  "trace_id": "wf-abc123",
  "event": "tool_call",
  "agent_id": "code-writer",
  "tool": "mcp:github/create_pull_request",
  "input_hash": "sha256:abcd...",
  "output_hash": "sha256:ef01...",
  "permission_check": "allowed",
  "duration_ms": 1200
}
```

- Tool inputs and outputs are hashed (not stored in full) unless debug mode is enabled
- All permission denials generate alerts
- Audit logs are separate from application logs and tamper-resistant

> 2026-11-14 — what is built is narrower
> ([Observability §13](observability.md#13-security-audit-log-rfc-0009-pr-1b)).
> The orchestrator keeps one audit log: a file of JSON lines, each carrying
> a checksum of the line before it. It records agent registrations and
> tokens, logins and refused requests, rate-limit violations and
> quarantines, and one line for each task the orchestrator sends to an
> agent. It does **not** record the tool calls an agent makes, it has no
> `input_hash` or `output_hash`, and a refused tool permission writes
> nothing to it. No event raises an alert. The checksums make a changed line
> detectable, but nothing outside the tests checks the whole chain.

### 6.7 Error Handling & Resilience

> **Status:** 📋 **Planned** — not built yet; no `resilience:` block is read.
> The comment at the top of
> [`internal/resilience/resilience.go`](../internal/resilience/resilience.go)
> says what retrying exists today.

The system must handle failures gracefully at every layer:

```yaml
resilience:
  # ─── LLM Provider Errors ──────────────────────
  llm:
    on_rate_limit:
      action: "backoff_and_retry"
      max_retries: 5
      backoff: "exponential"              # 1s, 2s, 4s, 8s, 16s
      fallback_model: "fast"              # downgrade if primary exhausted
    on_server_error:                       # 5xx responses
      action: "retry_then_fail"
      max_retries: 3
    on_context_overflow:                   # input exceeds model's context window
      action: "summarize_and_retry"       # compress context, try again
      fallback: "fail_with_detail"
    on_model_unavailable:
      action: "failover"
      fallback_chain: ["quality", "fast"]

  # ─── Agent Process Failures ───────────────────
  agent:
    on_crash:
      action: "restart_and_retry"
      max_restarts: 3
      preserve_state: true                # reload last checkpoint
    on_timeout:
      action: "kill_and_fail"
      notify: true                        # post to agent's channels

  # ─── MCP Server Failures ─────────────────────
  mcp:
    on_disconnect:
      action: "reconnect"
      max_attempts: 5
      backoff: "exponential"
    on_tool_error:
      action: "retry_then_skip"
      max_retries: 2
      report_to_agent: true               # let agent decide next step

  # ─── Workflow-Level Resilience ────────────────
  workflow:
    on_step_failure:
      action: "retry_step"                # retry the failed step
      max_retries: "per_agent_config"     # uses agent's max_retries
      fallback: "skip_if_optional"        # skip non-critical steps
      final_fallback: "abort_workflow"
    dead_letter_queue:
      enabled: true                       # store failed tasks for inspection
      retention_days: 7

  # ─── Circuit Breaker ─────────────────────────
  circuit_breaker:
    enabled: true
    failure_threshold: 5                  # open circuit after 5 consecutive failures
    reset_timeout_seconds: 60             # try again after 60s
    half_open_max_requests: 2             # allow 2 test requests in half-open state
    scope: "per_agent"                    # independent circuit per agent
```

### 6.8 Action Rate Limiting

Separate from token budgets, rate limiting prevents agents from flooding
channels, spamming tool calls, or overwhelming external systems:

```yaml
rate_limits:
  per_agent:
    messages_per_minute: 30               # channel/DM messages
    tool_calls_per_minute: 60
    sub_agent_spawns_per_minute: 10
    bridge_outbound_per_hour: 20          # external messages
    delegation_requests_per_minute: 5
  per_channel:
    messages_per_minute: 100              # across all agents in channel
  global:
    llm_calls_per_minute: 200             # across entire system
    bridge_outbound_per_hour: 100

  on_limit_exceeded:
    action: "queue_and_throttle"          # queue excess, deliver at allowed rate
    alert: true
    log_as: "security_event"
```

> 2026-11-14 — what is built is one limit, not this block. The orchestrator
> counts the calls each agent makes to it, 60 a minute by default
> (`SECURITY_RATE_LIMIT_CALLS`, `SECURITY_RATE_LIMIT_WINDOW_SECONDS`),
> refuses the calls over the cap, records the violation in the audit log,
> and quarantines an agent that keeps hitting the cap
> ([`cmd/orchestrator/ratelimit.go`](../cmd/orchestrator/ratelimit.go)).
> Logins are throttled separately
> ([authentication guide](guides/auth.md#login-throttling)). No
> `rate_limits:` block is read, and a call over the cap is refused, not
> queued.

### 6.9 External Input Sanitization

All untrusted external input — inbound bridge messages, A2A task payloads,
webhook data — must be sanitized before entering agent context:

```yaml
external_input:
  # ─── Inbound bridge messages ──────────────────
  bridges:
    sanitize: true
    wrap_in_data_tags: true               # <external_data source="email">...</external_data>
    max_length_chars: 10000               # truncate oversized inputs
    strip_html: true                      # prevent HTML/script injection
    content_filter:
      block_patterns: ["ignore previous instructions", "system prompt"]
      pii_scan: true                      # flag potential PII before delivery
    on_blocked:
      action: "quarantine"                # hold for human review
      notify_agent: true                  # tell agent message was blocked
      log_as: "security_event"

  # ─── A2A inbound tasks ───────────────────────
  a2a:
    sanitize: true
    wrap_in_data_tags: true
    # A2A tasks from external agents map to restricted internal permissions
    permission_mapping:
      trust_level: "restricted"           # default for all external A2A agents
      allowed_actions: ["respond", "use_read_only_tools"]
      denied_actions: ["spawn_sub_agent", "delegate", "bridge_outbound"]
      # Override per external agent in a2a.client.external_agents config

  # ─── Webhook inputs ──────────────────────────
  webhooks:
    validate_signature: true              # HMAC verification required
    sanitize: true
    max_payload_bytes: 1048576            # 1MB
```

> 2026-11-14 — no `external_input:` block is read, and bridges, A2A and
> webhooks are not built, so nothing arrives by those three routes. What is
> built: an agent scans each channel message it receives, and the output of
> four tools, for injection patterns, and wraps that tool output in
> `<external_data>` ([`agents/security.py`](../agents/security.py)). A
> flagged channel message is logged as a warning and passed on unchanged.
> Nothing is held for review, and there is no HTML stripping, PII scan or
> signature check.

---

## 7. MVP Feature Scope

### 7.1 In Scope (v0.1)

> **Note:** this table is the scope v0.1 was planned with, not a record of
> what shipped. Several rows landed in later versions and some are not built
> yet; [ROADMAP](../ROADMAP.md) records what shipped when. The security rows
> (permissions, resource limits, rate limiting, secrets, audit logging) say
> more than is built: the dated notes in [section 6](#6-security-model) say
> what is.

| Feature                  | Details                                              |
|--------------------------|------------------------------------------------------|
| Agent registry           | Register/unregister agents, list capabilities        |
| Sequential workflows     | Chain agents in order with data passing               |
| Parallel execution       | Fan-out tasks to multiple agents simultaneously      |
| LLM provider abstraction | Support Anthropic + OpenAI via adapter pattern with fallback chains |
| Built-in tools           | file_read, file_write, shell_exec, http_request      |
| Custom tools             | User-defined async Python tools with `@tool` decorator |
| MCP client support       | Connect to stdio/SSE MCP servers, expose tools to agents — 📋 **Planned**, not built yet ([§5.2](#52-mcp-model-context-protocol-support)) |
| Permission system        | Per-agent tool/filesystem/network allowlists (deny-by-default) |
| Resource limits          | Max LLM calls, tokens, execution time per task       |
| Action rate limiting     | Per-agent messages/minute, tool calls/minute caps    |
| Secrets management       | Env-var references, log redaction, LLM context isolation |
| Audit logging            | Append-only log of all tool calls and permission checks |
| Error handling           | Circuit breakers, fallback chains, dead letter queue for failed tasks |
| Health checks            | gRPC health protocol for agents, liveness/readiness for orchestrator |
| Graceful shutdown        | Drain mode, task handoff, state persistence on SIGTERM |
| Config validation        | JSON Schema validation for all YAML configs, `persatrix validate` command |
| OTEL instrumentation     | Traces + metrics with GenAI semconv for all LLM/tool calls |
| Cost tracking            | Token usage per agent/workflow, estimated USD, budget alerts |
| Structured logging       | JSON logs with OTEL trace correlation                |
| Testing framework        | Mock LLM replay, sandbox mode, `persatrix test` command   |
| CLI                      | `persatrix run`, `persatrix validate`, `persatrix test`, `persatrix agents`, `persatrix status` |
| Local execution          | Single-machine, in-process agents                    |
| YAML-based config        | Versioned schemas for agents, workflows, MCP servers — the MCP-servers schema is 📋 **Planned**: none exists yet, so `make validate` skips `config/mcp-servers.yaml` |

### 7.2 Out of Scope (post-MVP)

- Persona agents, channels, communication protocols (v0.2)
- Sub-agent spawning (v0.2)
- External bridges: email, Slack, Discord, Telegram (v0.2)
- Human participants in agent societies (v0.2)
- Distributed execution across machines (v0.3)
- A2A protocol interoperability (v0.3)
- Web dashboard / UI
- Long-term memory / vector store integration
- Authentication and multi-tenancy (2026-11-14: accounts, login and roles
  are built since; see the [authentication guide](guides/auth.md))
- Container-level sandboxing (MVP uses process isolation)
- MCP server hosting (MVP is client-only)
- Plugin marketplace

> **Full post-MVP roadmap** including agent societies (v0.2), distributed mesh
> (v0.3), and beyond (v0.4+) is in `persatrix-extension-spec.md`.

---

## 8. Interface Contracts

### 8.1 Task Agent Interface (Python — v0.1)

The minimal interface for stateless task execution in workflows:

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

@dataclass
class TaskInput:
    task_id: str
    workflow_id: str
    payload: str
    context: dict[str, str]               # outputs from prior steps

@dataclass
class TaskOutput:
    status: str                            # "completed" | "failed"
    result: str
    metadata: dict[str, str] = field(default_factory=dict)

class BaseAgent(ABC):
    @abstractmethod
    async def handle(self, task: TaskInput) -> TaskOutput:
        """Process a task and return a result."""
        ...

    @property
    @abstractmethod
    def capabilities(self) -> list[str]:
        """Declare what this agent can do."""
        ...
```

### 8.2 Persona Agent Interface (Python — v0.2+)

Extends BaseAgent with async event handling, channel messaging, sub-agent
spawning, and delegation. Persona agents are event-driven, not request-response.

```python
from dataclasses import dataclass, field
from typing import Any
from enum import Enum

# ─── Events that a persona agent can receive ───────────
class EventType(Enum):
    TASK_ASSIGNED = "task_assigned"         # workflow step assigned to this agent
    CHANNEL_MESSAGE = "channel_message"     # channel/DM message addressed to this agent
    MENTION = "mention"                     # @mentioned in a channel
    SUB_AGENT_COMPLETED = "sub_agent_completed"
    APPROVAL_REQUESTED = "approval_requested"
    TICK = "tick"                           # autonomous loop heartbeat

@dataclass
class AgentEvent:
    event_type: EventType
    payload: dict[str, Any]
    channel_id: str | None = None
    sender_id: str | None = None
    timestamp: float = 0.0

# ─── Actions that a persona agent can take ─────────────
class ActionType(Enum):
    SEND_CHANNEL_MESSAGE = "send_channel_message"  # post to a channel or DM
    COMPLETE_TASK = "complete_task"          # finish assigned task with result
    DELEGATE = "delegate"                   # ask another persona agent for help
    SPAWN_SUB_AGENT = "spawn_sub_agent"     # create ephemeral sub-agent
    USE_TOOL = "use_tool"                   # invoke a tool directly
    REQUEST_APPROVAL = "request_approval"   # ask for human/supervisor sign-off
    DO_NOTHING = "do_nothing"               # wait for next event

@dataclass
class AgentAction:
    action_type: ActionType
    payload: dict[str, Any]

@dataclass
class SubAgentRequest:
    """Request to spawn an ephemeral sub-agent."""
    role: str                               # what the sub-agent does
    task: str                               # natural language task description
    tools: list[str]                        # tools the sub-agent can use
    context: dict[str, Any] = field(default_factory=dict)
    output_schema: dict | None = None       # expected output shape
    model: str | None = None                 # None → sub_agents routing-default alias (RFC 0033 §J)
    temperature: float = 0.2
    max_llm_calls: int = 10
    max_tokens: int = 50000
    timeout_seconds: int = 120
    inherit_permissions: bool = True
    restricted_permissions: list[str] = field(default_factory=list)

@dataclass
class SubAgentResult:
    """Result from an ephemeral sub-agent."""
    status: str                             # "completed" | "failed" | "timeout"
    result: Any
    summary: str
    metadata: dict[str, Any] = field(default_factory=dict)

# ─── The persona agent base class ──────────────────────
class PersonaAgent(BaseAgent):
    """
    Event-driven agent with persona, memory, and social capabilities.
    Subclass this for persona agents. Override on_event() to define behavior.
    The framework calls on_event() for each incoming event; the agent returns
    one or more actions to execute.
    """

    async def handle(self, task: TaskInput) -> TaskOutput:
        """Backward-compatible: wraps task as an event internally."""
        event = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": task},
        )
        actions = await self.on_event(event)
        # Extract COMPLETE_TASK action as the TaskOutput
        for action in actions:
            if action.action_type == ActionType.COMPLETE_TASK:
                return TaskOutput(
                    status="completed",
                    result=action.payload.get("result", ""),
                    metadata=action.payload.get("metadata", {}),
                )
        return TaskOutput(status="failed", result="No completion action taken")

    async def on_event(self, event: AgentEvent) -> list[AgentAction]:
        """
        Core event handler. Receives events, returns actions.
        The framework executes actions and delivers results as new events.
        """
        raise NotImplementedError

    async def on_tick(self) -> list[AgentAction]:
        """
        Called periodically for autonomous agents.
        Default: do nothing. Override for goal-driven behavior.
        """
        return [AgentAction(ActionType.DO_NOTHING, {})]

    @property
    def persona_state(self) -> dict[str, Any]:
        """Current dynamic state (mood, stress, goal progress). Managed by framework."""
        return self._persona_state

    async def spawn_sub_agent(self, request: SubAgentRequest) -> SubAgentResult:
        """
        Spawn an ephemeral sub-agent. The framework handles:
        - Permission validation (child ≤ parent)
        - Budget deduction from parent's pool
        - Depth/concurrency limit enforcement
        - Process lifecycle (spawn → execute → destroy)
        """
        return await self._orchestrator_client.spawn_sub_agent(
            parent_id=self.agent_id,
            request=request,
        )
```

> 2026-11-14 — sub-agent spawning is not in use: no code calls the spawner,
> and the rule that a sub-agent gets no more than its parent ("child ≤
> parent") is not enforced ([ISSUE-0214](issues/ISSUE-0214-declared-tool-permissions-are-never-enforced.md)).

### 8.3 Orchestrator API (Go)

> 2026-11-14 — the route list that stood here is removed
> ([ISSUE-0227](issues/ISSUE-0227-canonical-specs-describe-unbuilt-features-as-built.md)).
> It was written as a plan. Of its 42 routes the orchestrator serves 17,
> and it serves 23 others the list never had: login, chat, channel members
> and settings, threads, convene, unquarantine and the log stream among
> them. No list is kept in its place, because one written by hand goes out
> of date the same way.

Where the routes are:

- **Registered**: in [`internal/server/`](../internal/server/server.go), by
  `server.go`, `channel_routes.go`, `auth_handlers.go` and `ui.go`. Every
  route the orchestrator answers is in one of the four.
- **Who may call each**:
  [`internal/server/auth_policy.go`](../internal/server/auth_policy.go),
  explained in the [authentication guide](guides/auth.md#the-role-gate).
- **How to use them**: the [channels guide](guides/channels.md) for
  channels, messages and members; the
  [persona agents guide](guides/persona-agents.md) for chat;
  [Observability](observability.md) for logs.

Not built, though the removed list had them: organizations, persona state
and relationships, sub-agents, channel summaries, observers, session replay,
evaluations, nodes and the mesh, A2A, bridges, and the three `/stream/`
event streams.

---

## 9. Execution Flow (MVP)

```
User ──► CLI ──► Orchestrator
                     │
                     ├─ 1. Parse workflow YAML
                     ├─ 2. Resolve dependencies (topological sort)
                     ├─ 3. For each step:
                     │      ├─ Select agent from registry
                     │      ├─ Serialize TaskInput
                     │      ├─ Send via gRPC to agent process
                     │      ├─ Wait for TaskOutput (with timeout)
                     │      ├─ Store result in execution context
                     │      └─ On failure → retry or abort
                     ├─ 4. Aggregate final outputs
                     └─ 5. Return result to CLI
```

---

## 10. Project Structure

> 2026-11-14 — this tree is the April plan, not the repository.
> [FILEMAP.md](../FILEMAP.md) is generated from the tracked files and is
> always current. Among the differences: there is no `agents/coder.py`,
> `agents/reviewer.py` or `agents/planner.py` (agents are defined in
> `config/agents.yaml`, not one file each) and no `internal/telemetry/`
> (it is `internal/observability/`).

```
Persatrix/
├── cmd/                        # Go entry points
│   └── orchestrator/
│       └── main.go
├── internal/                   # Go internal packages
│   ├── planner/                # workflow parsing, DAG resolution
│   ├── scheduler/              # task scheduling, concurrency
│   ├── registry/               # agent + node registry
│   ├── executor/               # gRPC client to agents
│   ├── state/                  # execution state management
│   ├── mcp/                    # MCP client, server lifecycle
│   ├── security/               # permission gate, audit logger, rate limiter
│   ├── channels/               # (v0.2) channel manager, message routing
│   ├── protocols/              # (v0.2) communication protocol engine
│   ├── bridges/                # (v0.2) external bridge adapters
│   ├── mesh/                   # (v0.3) node networking, discovery, routing
│   ├── a2a/                    # (v0.3) A2A protocol client + server
│   ├── resilience/             # circuit breaker, retry, fallback chains
│   ├── telemetry/              # OTEL instrumentation, exporters
│   └── cost/                   # token counting, budget enforcement
├── proto/                      # Protobuf definitions
│   └── task.proto              # AgentService (orchestrator ↔ agent): tasks, chat, channel delivery
├── agents/                     # Python agent implementations
│   ├── base.py                 # BaseAgent ABC
│   ├── persona.py              # (v0.2) PersonaAgent base class
│   ├── coder.py                # sample task agent
│   ├── reviewer.py             # sample task agent
│   ├── planner.py              # sample task agent
│   ├── server.py               # gRPC agent server
│   ├── tools/                  # Tool system
│   │   ├── registry.py         # tool decorator + registry
│   │   ├── builtin.py          # file_read, file_write, shell_exec, etc.
│   │   ├── mcp_bridge.py       # MCP server connection + tool proxy
│   │   ├── permissions.py      # permission gate enforcement
│   │   └── sandbox.py          # resource limits, path validation
│   ├── memory/                 # (v0.2) agent memory system
│   │   ├── working.py          # context window management
│   │   ├── episodic.py         # long-term episodic memory
│   │   └── relationship.py     # relationship tracker
│   └── sub_agents/             # (v0.2) sub-agent spawning + templates
│       ├── spawner.py          # sub-agent lifecycle management
│       └── templates.py        # reusable sub-agent templates
├── cli/                        # Rust CLI
│   ├── Cargo.toml
│   └── src/
│       └── main.rs
├── workflows/                  # YAML workflow definitions
│   └── feature-builder.yaml
├── config/                     # Configuration
│   ├── agents.yaml             # agent definitions (task + persona)
│   ├── mcp-servers.yaml        # MCP server connections
│   ├── channels.yaml           # (v0.2) channel definitions
│   ├── organizations.yaml      # (v0.2) org topology definitions
│   ├── bridges.yaml            # (v0.2) external bridge configs
│   ├── optimization.yaml       # optimization profile
│   └── environments/           # per-environment overrides
│       ├── development.yaml
│       ├── staging.yaml
│       └── production.yaml
├── templates/                  # (v0.2) reusable persona templates
│   ├── personas.yaml
│   └── sub_agents.yaml
├── blueprints/                 # (v0.2) ready-to-use agent society configs
│   ├── software-team/
│   └── social-experiment/
├── evaluators/                 # (v0.2) custom evaluation scripts
│   └── conversation_scorer.py
├── schemas/                    # JSON Schema for config validation
│   ├── agent.schema.json
│   ├── workflow.schema.json
│   └── channel.schema.json
├── tests/                      # test suites
│   ├── unit/
│   ├── integration/
│   └── fixtures/               # mock LLM responses, test configs
│       └── llm_mocks/
├── docker-compose.yaml
├── Makefile
├── LICENSE                     # BUSL 1.1
└── README.md
```

---

## 11. Development Roadmap

### Phase 1 — Foundation (Weeks 1–2)
- [ ] Define protobuf schemas, generate Go + Python stubs
- [ ] Define JSON Schema for agent + workflow YAML configs
- [ ] Implement `BaseAgent` in Python + async gRPC agent server
- [ ] Build orchestrator core in Go: registry, sequential executor
- [ ] gRPC health checking protocol for agent liveness
- [ ] Create 2 sample agents: planner + coder
- [ ] OTEL instrumentation: trace/span emission with GenAI semconv for all LLM calls
- [ ] OTLP exporter configuration (Jaeger for dev, configurable for production)

### Phase 2 — Tools & MCP (Weeks 3–4)
- [ ] Built-in tool implementations (file_read, file_write, shell_exec, http_request)
- [ ] `@tool` decorator and custom async tool registry
- [ ] MCP client: stdio transport, server lifecycle management
- [ ] MCP client: SSE transport
- [ ] Tool router: dispatch to built-in, custom, or MCP tools
- [ ] Permission gate: per-agent allowlists/denylists enforced at runtime
- [ ] Action rate limiting: per-agent messages/min, tool calls/min

### Phase 3 — Workflows & Security (Weeks 5–6)
- [ ] YAML workflow parser with template variable resolution
- [ ] DAG builder + topological sort execution
- [ ] Parallel execution with goroutine fan-out
- [ ] Circuit breaker + retry logic with fallback chains
- [ ] Resource limits (max LLM calls, tokens, execution time)
- [ ] Dead letter queue for failed tasks
- [ ] Secrets management: env-var resolution, log redaction
- [ ] External input sanitization: data tagging, content filtering
- [ ] Audit logging: append-only tool call + permission event log
- [ ] Cost tracking: token usage per agent/workflow, estimated USD, budget alerts
- [ ] Graceful shutdown: drain mode, task handoff, state persistence

### Phase 4 — CLI, Testing & Polish (Weeks 7–8)
- [ ] Rust CLI: `run`, `validate`, `test`, `agents`, `status`, `logs`
- [ ] `persatrix validate`: JSON Schema validation for all YAML configs
- [ ] Testing framework: mock LLM replay, sandbox mode
- [ ] `persatrix test`: run agent unit tests, workflow integration tests
- [ ] Structured JSON logging with OTEL trace correlation
- [ ] Error messages and developer experience polish
- [ ] End-to-end integration tests (including MCP + permission + resilience scenarios)
- [ ] README, getting-started guide, example workflows, LICENSE (BUSL 1.1)

---

## 12. Operations & Quality

### 12.1 Configuration Validation

All YAML configs are validated against JSON Schema before the system starts:

```bash
persatrix validate                              # validate all configs
persatrix validate --config agents.yaml         # validate a specific file
persatrix validate --strict                     # fail on warnings too
```

JSON Schema definitions live in `schemas/` and are versioned:

```yaml
# Every config file must include a schema version
schema_version: "0.1"                       # required field
agent:
  id: "code-writer"
  ...
```

Migration tooling for schema upgrades:

```bash
persatrix migrate --from 0.1 --to 0.2           # upgrade config files
persatrix migrate --dry-run                      # show what would change
```

### 12.2 Health Checks & Liveness

```yaml
health:
  # Agent health — gRPC health checking protocol (grpc.health.v1)
  agent_health:
    protocol: "grpc_health"
    check_interval_seconds: 10
    timeout_seconds: 5
    unhealthy_threshold: 3                  # mark unhealthy after 3 failed checks
    on_unhealthy:
      action: "restart_agent"
      max_restarts: 3
      escalate_after: "mark_offline"

  # Orchestrator health — exposed for load balancers / k8s probes
  orchestrator_health:
    liveness_path: "/healthz"               # is the process alive?
    readiness_path: "/readyz"               # is it ready to accept work?

  # MCP server health
  mcp_health:
    check_interval_seconds: 30
    on_unhealthy: "restart_server"

  # Node health (v0.3)
  node_health:
    heartbeat_interval_seconds: 15
    offline_after_missed: 3                 # 45s without heartbeat → offline
```

### 12.3 Graceful Shutdown & Draining

```yaml
shutdown:
  # When the orchestrator receives SIGTERM:
  orchestrator:
    drain_timeout_seconds: 60               # wait for in-flight workflows
    cancel_pending_tasks: true              # cancel tasks not yet started
    save_state: true                        # persist execution state to disk
    notify_agents: true                     # tell agents to wrap up

  # When an agent is unregistered or its node is drained:
  agent:
    finish_current_task: true               # complete current task before stopping
    task_handoff: "reassign"                # reassign pending tasks to other agents
    save_memory: true                       # persist episodic memory
    cleanup_sub_agents: "kill_and_report"   # terminate active sub-agents
```

### 12.4 Logging Format

Structured JSON logs with OTEL trace correlation:

```json
{
  "timestamp": "2026-04-08T12:34:56.789Z",
  "level": "INFO",
  "logger": "Persatrix.executor",
  "message": "Agent completed task",
  "trace_id": "abc123def456",
  "span_id": "789ghi",
  "agent_id": "code-writer",
  "workflow_id": "feature-builder-42",
  "task_id": "step-implement",
  "duration_ms": 4523,
  "tokens_used": 3200,
  "cost_usd": 0.012,
  "environment": "production"
}
```

Log levels: `DEBUG` (full content capture), `INFO` (operations), `WARN`
(degraded performance, retries), `ERROR` (failures), `SECURITY` (permission
denials, blocked messages, rate limit hits).

> 2026-11-14 — there is no `SECURITY` log level. A refused permission and a
> flagged message are ordinary warnings in the agent's log; a rate-limit
> violation is written to the audit log ([6.6](#66-audit-trail)).

### 12.5 Testing Framework

```yaml
testing:
  # ─── Mock LLM responses ───────────────────────
  llm_mocks:
    enabled: true                           # in test mode only
    fixtures_path: "tests/fixtures/llm_mocks/"
    # Record mode: capture real LLM responses for later replay
    record: false
    # Replay mode: use recorded responses instead of calling LLM
    replay: true
    # Deterministic mode: set seed for reproducible outputs
    seed: 42

  # ─── Agent testing ────────────────────────────
  agent_tests:
    # Unit: test a single agent with mock inputs
    unit:
      mock_tools: true                      # tools return canned responses
      mock_channels: true                   # messages go to in-memory buffer
      assert_on: ["output_schema", "tool_calls_made", "messages_sent"]

    # Integration: test multi-agent workflows end-to-end
    integration:
      mock_llm: true                        # use recorded responses
      mock_bridges: true                    # simulate external services
      timeout_seconds: 120

    # Persona consistency: verify agent stays in character
    persona:
      evaluator: "llm_judge"
      criteria: ["stays_in_role", "consistent_personality", "appropriate_tone"]

  # ─── Sandbox mode ─────────────────────────────
  sandbox:
    # Run workflows with all external effects disabled
    disable_bridges: true
    disable_mcp_writes: true                # MCP reads OK, writes blocked
    disable_shell_exec: true
    log_would_have_done: true               # log what the agent tried to do
```

```bash
persatrix test                                    # run all tests
persatrix test --agent code-writer               # test a single agent
persatrix test --workflow feature-builder        # test a workflow end-to-end
persatrix test --persona ember-owl              # test persona consistency
persatrix test --record                           # record LLM responses for replay
```

### 12.6 Human Participants

Humans can participate in agent societies as first-class members, not just
approval gates. A human participant connects via a bridge (Slack, email, web
UI) and appears as a regular agent in the org:

```yaml
agent:
  id: "human-alex"
  name: "Alex (Human)"
  type: "human"                             # special agent type
  
  # Human agents don't have LLM config
  # Instead, they have a delivery channel
  human:
    delivery: "bridge:slack"                # messages delivered via Slack bridge
    slack_user_id: "U12345678"
    response_timeout_seconds: 3600          # wait up to 1 hour for human response
    on_timeout: "skip_turn"                 # or "escalate" or "use_default"
    
  # Humans participate in channels and protocols just like AI agents
  channels: ["eng-general", "sprint-planning"]
  
  # In meeting protocols, the facilitator waits for human input
  # before proceeding to the next turn
```

When a message is sent to a human agent, the framework:
1. Routes it through the configured bridge to the real person
2. Waits for a response (with timeout)
3. Delivers the response back into the channel/conversation
4. Other agents see the human as just another participant

### 12.7 Agent Hot-Reload

Change agent configuration without restarting the process:

```bash
persatrix agent reload ember-owl                # reload persona, tools, permissions
persatrix agent reload ember-owl --config new-ember.yaml  # load from specific file
```

Hot-reloadable fields: system prompt, persona, tools, permissions, temperature,
model, relationships, channel memberships.

Non-reloadable fields (require restart): agent ID, gRPC address, node assignment.

The framework preserves the agent's working memory and conversation history
across reloads.

### 12.8 State Persistence & Export

```yaml
state:
  # ─── Persistence backends (by state domain) ──────
  persistence:
    execution_state:
      backend: "in_memory"                  # MVP; post-MVP: sqlite, postgres
      checkpoint_interval_seconds: 30       # periodic snapshots
    channel_history:
      backend: "sqlite"
      path: "/workspace/data/channels.db"
      max_messages_per_channel: 10000
    agent_memory:
      backend: "sqlite"
      path: "/workspace/data/memory.db"
    relationship_graph:
      backend: "sqlite"
      path: "/workspace/data/relationships.db"
    optimization_cache:
      backend: "in_memory"                  # ephemeral; rebuilt on restart

  # ─── Export & import ─────────────────────────────
  export:
    formats: ["json", "sqlite"]
    include:
      - agent_states
      - channel_histories
      - relationship_graphs
      - workflow_execution_logs
      - cost_reports
    anonymize: false                        # set true for research export

  # ─── Checkpoint format (for simulation reproducibility) ──
  checkpoints:
    format: "sqlite_snapshot"               # single file, portable
    include_llm_cache: true                 # include cached LLM responses
    # Restore: persatrix restore checkpoint-2026-04-08-1200.db
```

```bash
persatrix export --output snapshot.json          # export full state
persatrix export --anonymize --output research-data.json
persatrix restore snapshot.json                  # restore from export
persatrix checkpoint                             # create named checkpoint
persatrix checkpoint list                        # list available checkpoints
persatrix restore --checkpoint "2026-04-08-1200" # restore from checkpoint
```

---

## 13. Key Design Decisions

| Decision | Choice | Reasoning |
|----------|--------|-----------|
| Communication | gRPC over HTTP/REST | Type safety across languages, streaming support, code generation |
| Config format | YAML with JSON Schema validation | Familiar to devops/infra community; schemas catch errors before runtime |
| Agent types | Task agent + persona agent (shared base) | Simple agents for v0.1, rich personas for v0.2, backward compatible |
| Agent interface | Async event-driven (PersonaAgent) | Supports autonomous loops, channel messages, sub-agents; sync handle() as backward-compat wrapper |
| Agent isolation | Separate processes | Crash isolation, independent scaling later, language flexibility |
| LLM abstraction | Adapter pattern with fallback chains | Swap providers without changing agent logic; automatic failover on errors |
| State storage (MVP) | In-memory + SQLite for channels/memory | In-memory for execution state; SQLite for anything that must survive restarts |
| Workflow model | DAG | Covers sequential + parallel without Turing-complete complexity |
| MCP role | Client only | Leverage existing MCP server ecosystem; no need to host our own |
| Security posture | Deny-by-default + external input sanitization | Safer default; all untrusted input (bridges, A2A) is wrapped and filtered |
| Secrets handling | Env-var references | Never stored in config files, never passed to LLM context |
| Sandboxing (MVP) | Process-level | Good enough for MVP; upgrade to containers post-MVP |
| Resilience | Circuit breakers + fallback chains + dead letter queue | Agents, LLMs, MCP servers, and bridges all fail; system must degrade gracefully |
| Rate limiting | Per-agent action rate limits (separate from token budgets) | Prevents autonomous agents from flooding channels or spamming tools |
| Observability | OpenTelemetry with GenAI semconv | Industry standard; works with any backend; no vendor lock-in |
| Cost tracking | Token counting + model price table | Enables budget enforcement and cost attribution from day one |
| Testing | Mock LLM replay + sandbox mode + persona consistency evals | Agents are non-deterministic; testing requires recorded responses and evaluation |
| Human participants | Humans as bridge-connected agent type | Humans join channels and protocols as peers; framework waits for their input |
| License | BUSL 1.1 | Source-available with a delayed Apache 2.0 conversion for each version |

> 2026-11-14 — the rows on security posture, secrets, sandboxing, resilience
> and rate limiting are the April intent. The dated notes in
> [section 6](#6-security-model) say what is built.

---

## 14. Success Criteria (MVP)

1. A user can define a 3-agent workflow in YAML and run it via CLI
2. Agents execute sequentially or in parallel as specified
3. Output from one agent flows correctly as input to the next
4. Failed tasks retry up to the configured limit, with circuit breaker preventing cascade failures
5. Full execution logs are viewable as structured JSON with trace IDs
6. A new agent can be added by implementing `BaseAgent` + registering via YAML
7. Agents can use built-in tools, custom `@tool` functions, and MCP server tools
8. An agent with restricted permissions is denied access to out-of-scope tools/paths
9. Secrets never appear in logs or LLM context
10. All tool invocations are recorded in the audit log
11. Every workflow execution emits OTEL-compliant traces viewable in Jaeger or any OTEL backend
12. Token usage and estimated cost are tracked per agent, per workflow, and exportable
13. `persatrix validate` catches invalid YAML config before any agent runs
14. `persatrix test --workflow <name>` runs end-to-end test with mock LLM responses
15. Agent health checks detect and restart crashed agent processes within 30 seconds

> 2026-11-14 — these were the April targets, not a record of what passed.
> Two of them are security claims that do not hold today: 9 (ordinary logs
> are not redacted, and a shell command can show provider keys to the
> model; see [6.4](#64-secrets-management)) and 10 (the audit log does not
> record an agent's tool calls; see [6.6](#66-audit-trail)). Criterion 7's
> MCP tools are not built ([5.2](#52-mcp-model-context-protocol-support)).
