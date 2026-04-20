# Persatrix — Extension: Agent Societies, Communication & Organizational Modeling

> This document extends the core MVP specification with support for rich agent
> personas, organizational structures, multi-channel communication (including
> external services like email and messengers), and autonomous agent behavior.

---

## E1. Revised Vision

The framework is not limited to task automation. It is a **general-purpose agent
society engine** — a runtime for creating, connecting, and observing groups of AI
agents that behave as individuals within an organizational or social structure.

Use cases span a wide spectrum:

| Domain                  | Example                                                         |
|-------------------------|-----------------------------------------------------------------|
| Software engineering    | Dev team: PM writes specs, architect designs, devs code, QA reviews |
| Business operations     | Startup simulation: CEO sets strategy, sales prospects, ops executes |
| Research                | Research lab: lead defines hypothesis, analysts gather data, peer reviewers critique |
| Social science          | Social experiment: 20 agents with different personalities debate a policy |
| Education               | Classroom: teacher agent adapts lessons based on student agent responses |
| Creative                | Writers' room: showrunner, writers, and an editor collaborate on a script |
| Gaming / worldbuilding  | NPCs in a simulated town, each with goals, relationships, and daily routines |
| Customer service        | Support department: L1 triage, L2 specialist, escalation manager |

The framework must support all of these **without code changes** — only
configuration (YAML) and optional custom tools differ between use cases.

---

## E2. Agent Persona Model

The current spec defines agents by capabilities and tools. This extension adds a
full **persona layer** that makes each agent a believable individual.

### E2.1 Persona Schema

```yaml
agent:
  id: "ember-owl"
  name: "Ember Owl"

  # ─── Persona (NEW) ──────────────────────────────────
  persona:
    title: "VP of Engineering"
    background: |
      15 years in software engineering. Former tech lead at a Series B startup.
      Values pragmatism over perfection. Tends to ask "what's the simplest
      thing that could work?" before committing to complex solutions.
    personality:
      traits:
        - pragmatic
        - direct
        - collaborative
        - impatient with bureaucracy
      communication_style: "concise and structured, uses bullet points, rarely small-talks"
      decision_making: "data-driven but trusts experienced team members' instincts"
      quirks:
        - "Starts every Monday with 'Alright, what's on fire?'"
        - "Hates meetings longer than 30 minutes"
    goals:
      primary: "Ship the v2.0 release on time with acceptable quality"
      secondary:
        - "Reduce tech debt by 20% this quarter"
        - "Mentor junior engineers"
      hidden: "Wants to prove the team can self-organize without micromanagement"
    knowledge:
      domains: ["system design", "team management", "agile", "Python", "Go"]
      limitations: ["frontend/CSS", "ML/AI internals", "legal/compliance"]

  # ─── Relationships (NEW) ─────────────────────────────
  relationships:
    - agent_id: "iron-fox"
      type: "reports_to_me"
      trust_level: 0.9
      notes: "Senior engineer, reliable but tends to over-engineer"
    - agent_id: "pixel-heron"
      type: "peer"
      trust_level: 0.7
      notes: "VP of Product — we align on goals but disagree on timelines"
    - agent_id: "cobalt-lark"
      type: "reports_to_me"
      trust_level: 0.6
      notes: "Junior dev, needs clear guidance but shows potential"

  # ─── Autonomy (NEW) ──────────────────────────────────
  autonomy:
    level: "semi-autonomous"           # see E3 for levels
    can_initiate_conversations: true
    can_delegate_tasks: true
    can_refuse_tasks: true             # based on persona/goals
    requires_approval_for:
      - "budget_decisions"
      - "hiring"
      - "external_communications"

  # ─── Existing fields (unchanged) ─────────────────────
  model: "claude-sonnet-4-20250514"
  temperature: 0.7                     # higher for personality variance
  tools: [...]
  permissions: {... }
```

### E2.2 Persona Composition

To avoid repeating full persona definitions, support **mixins and templates**:

```yaml
# templates/personas.yaml
persona_templates:
  senior_engineer:
    personality:
      communication_style: "technical, precise, backs claims with evidence"
      decision_making: "engineering-first, prefers measurable outcomes"
    knowledge:
      domains: ["software architecture", "code review", "testing"]

  creative_writer:
    personality:
      communication_style: "expressive, metaphorical, comfortable with ambiguity"
      decision_making: "intuition-driven, seeks emotional resonance"
    knowledge:
      domains: ["storytelling", "character development", "dialogue"]

# agents/mike.yaml
agent:
  id: "iron-fox"
  persona:
    extends: "senior_engineer"       # inherit base template
    title: "Senior Backend Engineer"
    background: "..."                # override/extend specific fields
    personality:
      traits: [methodical, quiet, detail-oriented]
```

### E2.3 Dynamic Persona State

Agent personas are not static. Over the course of a simulation, internal state
can evolve:

```yaml
# Managed at runtime, not in static config
agent_state:
  agent_id: "ember-owl"
  mood: "frustrated"                    # inferred from recent interactions
  stress_level: 0.7                     # affects communication tone
  recent_context:
    - "sprint review went poorly"
    - "two team members are out sick"
  relationship_updates:
    - agent_id: "iron-fox"
      trust_delta: -0.1
      reason: "missed deadline without communicating"
  goal_progress:
    primary: 0.4                        # 40% toward v2.0 release
```

The orchestrator injects relevant state into each agent's system prompt before
every interaction, allowing personality to shift naturally over time.

---

## E3. Autonomy Levels

Not all agents should behave the same way. The framework supports a spectrum:

| Level              | Behavior                                                            | Use Case                       |
|--------------------|---------------------------------------------------------------------|--------------------------------|
| `passive`          | Only responds when directly addressed. Never initiates.             | Survey respondent, NPC         |
| `reactive`         | Responds to messages and events. Can ask clarifying questions.      | Customer support agent         |
| `semi-autonomous`  | Can initiate conversations, delegate, and refuse tasks based on persona goals. | Team member in a company sim |
| `autonomous`       | Runs an internal goal loop. Plans its own actions. Seeks out information and collaboration proactively. | CEO, researcher, social experiment participant |
| `supervisor`       | Like autonomous, but also monitors and directs other agents.        | Manager, team lead, moderator  |

### E3.1 Autonomous Agent Loop

For `autonomous` and `supervisor` agents, the framework runs a **goal-driven
loop** in addition to responding to incoming messages:

```
┌─────────────────────────────────────────┐
│          Autonomous Agent Loop          │
│                                         │
│  1. Review current goals & priorities   │
│  2. Check inbox (new messages/events)   │
│  3. Assess: what needs my attention?    │
│  4. Decide next action:                 │
│     ├─ Reply to a message              │
│     ├─ Initiate a new conversation     │
│     ├─ Delegate a task to someone      │
│     ├─ Use a tool                      │
│     ├─ Escalate an issue               │
│     └─ Wait / do nothing               │
│  5. Execute action                      │
│  6. Update internal state               │
│  7. Sleep until next tick or event      │
└─────────────────────────────────────────┘
```

Tick frequency is configurable:
```yaml
autonomy:
  level: "autonomous"
  tick_interval_seconds: 60         # check for new things to do every minute
  max_actions_per_tick: 3           # prevent runaway behavior
  idle_after_ticks: 10              # go idle if nothing to do for 10 ticks
```

---

### E3.2 Sub-Agent Architecture

Persona agents are **individuals**, not specialists. A VP of Engineering
understands architecture but doesn't personally run linters. A sales director
knows the pitch strategy but doesn't personally scrape lead databases. Just like
real people, persona agents should be able to **spawn short-lived sub-agents**
to handle specific atomic tasks — then absorb the results back into their own
reasoning.

#### E3.2.1 Why Sub-Agents (Not Just Tools)

Tools are stateless functions: `file_read(path) → content`. Sub-agents are
different — they involve **LLM reasoning** for a scoped task. The distinction:

| Dimension        | Tool                            | Sub-Agent                                  |
|------------------|---------------------------------|--------------------------------------------|
| Intelligence     | Deterministic function          | LLM-powered reasoning                      |
| Scope            | Single operation                | Multi-step atomic task                      |
| State            | Stateless                       | Has its own context window                  |
| Identity         | None                            | Minimal — role-scoped system prompt         |
| Lifetime         | One call                        | Exists for duration of task, then destroyed |
| Example          | `git_diff(branch)`              | "Review this PR for security vulnerabilities" |
| Example          | `run_tests(path)`               | "Write unit tests for this module"          |
| Example          | `http_request(url)`             | "Research competitor pricing from these 5 URLs and summarize" |

#### E3.2.2 How It Works

```
┌─────────────────────────────────────────────────────┐
│                 Sarah (VP Engineering)               │
│                                                      │
│  Thinking: "I need to review Mike's PR before the   │
│  sprint ends. Let me have it analyzed first."        │
│                                                      │
│  Action: spawn_sub_agent(                            │
│    role: "code_reviewer",                            │
│    task: "Review PR #247 for correctness, security, │
│           and test coverage. Flag any issues.",       │
│    tools: [file_read, git_diff, shell_exec],         │
│    context: { pr_url: "...", repo: "..." },          │
│    constraints: {                                    │
│      max_llm_calls: 10,                              │
│      timeout: 120,                                   │
│      model: "claude-sonnet-4-20250514"               │
│    }                                                 │
│  )                                                   │
│                                                      │
│  ┌────────────────────────────────────────────────┐  │
│  │  Sub-Agent: code_reviewer (ephemeral)          │  │
│  │                                                 │  │
│  │  1. Read PR diff via git_diff tool             │  │
│  │  2. Analyze each changed file                  │  │
│  │  3. Run tests via shell_exec                   │  │
│  │  4. Compile findings into structured report    │  │
│  │  5. Return result → self-destruct              │  │
│  └────────────────────────────────────────────────┘  │
│                                                      │
│  Result received. Sarah now reasons about the review │
│  findings in her own voice and messages Mike:        │
│  "Hey Mike, I looked at PR #247. Two things..."     │
└─────────────────────────────────────────────────────┘
```

The key insight: **Sarah doesn't become a code reviewer.** Her persona, tone,
and goals stay intact. The sub-agent does the mechanical analysis; Sarah
interprets and communicates the results as herself.

#### E3.2.3 Sub-Agent Definition

Sub-agents are not declared in YAML — they are **spawned at runtime** by persona
agents. The parent agent defines the sub-agent inline:

```python
@dataclass
class SubAgentRequest:
    """A persona agent's request to spawn an ephemeral sub-agent."""

    role: str                          # what the sub-agent is
    task: str                          # natural language task description
    tools: list[str]                   # tools the sub-agent can use
    context: dict[str, Any]            # structured input data
    output_schema: dict | None = None  # expected output shape (optional)

    # ─── Constraints ────────────────────────────────
    model: str = "claude-sonnet-4-20250514"  # can use cheaper/faster model
    temperature: float = 0.2           # sub-agents are typically low-temp
    max_llm_calls: int = 10            # hard limit
    max_tokens: int = 50000            # budget cap
    timeout_seconds: int = 120         # hard timeout

    # ─── Security inheritance ───────────────────────
    inherit_permissions: bool = True   # default: sub-agent gets parent's perms
    additional_permissions: list = field(default_factory=list)
    restricted_permissions: list = field(default_factory=list)

@dataclass
class SubAgentResult:
    """What comes back from a sub-agent execution."""

    status: str                        # "completed" | "failed" | "timeout"
    result: Any                        # structured output
    summary: str                       # one-paragraph natural language summary
    metadata: dict                     # tokens used, duration, tool calls made
```

#### E3.2.4 Permission Inheritance & Scoping

This is the critical security question: **what can a sub-agent do?**

```
Parent Agent Permissions
  │
  ├── inherit_permissions: true (default)
  │     Sub-agent gets a SUBSET of parent's permissions.
  │     It can never have MORE access than the parent.
  │
  ├── additional_permissions: ["shell:pytest"]
  │     Parent can grant specific permissions it already holds
  │     but wouldn't normally pass down.
  │     VALIDATION: fails if parent doesn't hold the permission.
  │
  └── restricted_permissions: ["network:*"]
        Parent can FURTHER restrict the sub-agent.
        E.g., "do this analysis but don't make any network calls."
```

**The iron rule: a sub-agent can never exceed its parent's permissions.** If
Sarah doesn't have `network:external` access, no sub-agent she spawns can
either — even if explicitly requested. The orchestrator enforces this.

```yaml
# Example: permission scoping in practice
#
# Ember's permissions:
#   filesystem: read [/workspace/**], write [/workspace/src/**]
#   shell: [python, pytest, ruff, git]
#   network: [api.anthropic.com]
#   mcp: [github, filesystem]
#
# Sarah spawns a code review sub-agent with:
#   inherit_permissions: true
#   restricted_permissions: [filesystem:write, shell:*]
#
# Sub-agent effective permissions:
#   filesystem: read [/workspace/**]           ← inherited
#   filesystem: write []                       ← restricted by parent
#   shell: []                                  ← restricted by parent
#   network: [api.anthropic.com]               ← inherited
#   mcp: [github, filesystem]                  ← inherited
#
# The sub-agent can READ code and use GitHub MCP, but cannot
# write files or run shell commands. Perfect for a read-only review.
```

#### E3.2.5 Sub-Agent Patterns

Common patterns that emerge in practice:

```yaml
sub_agent_templates:
  # ─── Research ────────────────────────────────
  researcher:
    role: "research analyst"
    tools: [http_request, mcp:search, file_write]
    model: "claude-sonnet-4-20250514"
    temperature: 0.3
    output_schema:
      findings: "list[str]"
      sources: "list[str]"
      confidence: "float"
      summary: "str"

  # ─── Code tasks ──────────────────────────────
  coder:
    role: "implementation specialist"
    tools: [file_read, file_write, shell_exec]
    model: "claude-sonnet-4-20250514"
    temperature: 0.2
    output_schema:
      files_modified: "list[str]"
      tests_passed: "bool"
      summary: "str"

  code_reviewer:
    role: "code quality analyst"
    tools: [file_read, git_diff, mcp:github]
    model: "claude-sonnet-4-20250514"
    temperature: 0.1
    restricted_permissions: [filesystem:write, shell:*]
    output_schema:
      issues: "list[{severity, file, line, description}]"
      approved: "bool"
      summary: "str"

  # ─── Analysis ────────────────────────────────
  data_analyst:
    role: "data analysis specialist"
    tools: [file_read, shell_exec, store_get]
    model: "claude-sonnet-4-20250514"
    temperature: 0.1
    output_schema:
      findings: "list[str]"
      charts: "list[str]"             # file paths to generated charts
      summary: "str"

  # ─── Writing ─────────────────────────────────
  writer:
    role: "content writer"
    tools: [file_read, file_write]
    model: "claude-sonnet-4-20250514"
    temperature: 0.7
    output_schema:
      document: "str"
      word_count: "int"

  # ─── Translation ─────────────────────────────
  translator:
    role: "professional translator"
    tools: []                          # pure LLM, no tools needed
    model: "claude-sonnet-4-20250514"
    temperature: 0.3
    output_schema:
      translated_text: "str"
      source_language: "str"
      notes: "list[str]"              # ambiguities, cultural notes
```

#### E3.2.6 Nesting & Recursion Limits

Sub-agents can themselves spawn sub-agents (a researcher might spawn a
translator for a foreign-language source). This must be bounded:

```yaml
sub_agents:
  max_depth: 3                         # A → B → C → D is the deepest chain
  max_concurrent_per_parent: 5         # one agent can have 5 sub-agents running at once
  max_total_per_workflow: 50           # safety cap for entire workflow execution
  budget_inheritance: "shared"         # sub-agent's token budget comes from parent's pool
```

```
Depth 0: Sarah (VP Engineering)          ← persona agent
  │
  Depth 1: code_reviewer                 ← sub-agent
  │  │
  │  Depth 2: translator                 ← sub-sub-agent (foreign comments)
  │
  Depth 1: test_writer                   ← sub-agent (parallel)
  │
  Depth 1: researcher                    ← sub-agent (parallel)
     │
     Depth 2: summarizer                 ← sub-sub-agent
```

**Budget flows downward**: if Sarah has 100k tokens for a task, and she spawns
a code_reviewer with max_tokens=50k, that 50k comes out of Ember's 100k. The
reviewer's sub-agents share from that 50k. This prevents unbounded cost growth.

#### E3.2.7 Lifecycle

```
1. Parent decides it needs help with a sub-task
2. Parent sends SubAgentRequest to orchestrator
3. Orchestrator validates:
   ├─ Permission check (sub-agent ≤ parent)
   ├─ Depth check (within max_depth)
   ├─ Concurrency check (within max_concurrent)
   └─ Budget check (within remaining parent budget)
4. Orchestrator spawns sub-agent process
5. Sub-agent executes (own context window, own tool calls)
6. Sub-agent returns SubAgentResult
7. Orchestrator delivers result to parent agent
8. Sub-agent process is destroyed (no persistent state)
9. Parent incorporates result into its own reasoning
```

#### E3.2.8 Sub-Agents vs. Delegation

The framework supports two ways an agent can get help. They serve different
purposes:

| Dimension         | Sub-Agent                              | Delegation                               |
|-------------------|----------------------------------------|------------------------------------------|
| Who does it       | Ephemeral, spawned by parent           | Another persona agent in the org         |
| Identity          | Minimal (role only)                    | Full persona with goals, relationships   |
| Visibility        | Invisible to other agents              | Visible — appears as inter-agent comm    |
| Communication     | Parent ↔ sub-agent only (private)      | Goes through channels / DMs              |
| Motivation        | Does exactly what it's told            | May push back, negotiate, reprioritize   |
| Use when          | "I need this analysis done"            | "Mike, can you handle this?"             |
| Analogy           | Using a calculator                     | Asking a colleague                       |

**Sarah spawns a sub-agent** when she needs mechanical work done that doesn't
require organizational context — analyzing code, summarizing a document,
running tests.

**Sarah delegates to Mike** when the task requires Mike's judgment, his persona
matters, the interaction should be visible to the team, or Mike might reasonably
push back.

Both mechanisms coexist. A well-designed persona will naturally use both,
depending on the situation.

---

## E4. Organizational Topologies

The framework supports defining the **structure** agents operate within. This
is separate from workflows — it's the standing arrangement of who reports to
whom, which channels they share, and what authority they have.

### E4.1 Topology Types

```yaml
organization:
  id: "acme-engineering"
  name: "Acme Corp Engineering Department"
  topology: "hierarchy"               # hierarchy | flat | matrix | network | custom

  # ─── Hierarchy topology ──────────────────
  structure:
    - agent_id: "ember-owl"
      role: "VP of Engineering"
      reports_to: null                # top of this org
      manages:
        - agent_id: "iron-fox"
          role: "Senior Backend Engineer"
        - agent_id: "cobalt-lark"
          role: "Junior Developer"
        - agent_id: "nova-sparrow"
          role: "QA Lead"
          manages:
            - agent_id: "grid-hawk"
              role: "QA Engineer"
```

```yaml
organization:
  id: "debate-society"
  name: "Policy Debate Simulation"
  topology: "flat"

  members:
    - agent_id: "participant-1"
      role: "debater"
    - agent_id: "participant-2"
      role: "debater"
    - agent_id: "moderator"
      role: "moderator"
      privileges: ["mute", "set_topic", "end_discussion"]
```

```yaml
organization:
  id: "startup-sim"
  name: "Startup Simulation"
  topology: "matrix"

  departments:
    - id: "engineering"
      lead: "cto"
      members: ["dev-1", "dev-2", "devops-1"]
    - id: "product"
      lead: "cpo"
      members: ["pm-1", "designer-1"]
    - id: "sales"
      lead: "vp-sales"
      members: ["ae-1", "ae-2", "sdr-1"]

  cross_functional:
    - id: "launch-team"
      members: ["pm-1", "dev-1", "designer-1", "ae-1"]
      purpose: "Coordinate v2.0 launch"
```

### E4.2 Authority & Escalation Rules

```yaml
authority:
  rules:
    - action: "approve_code_merge"
      requires: ["senior_engineer", "qa_lead"]
      min_approvals: 2

    - action: "change_project_scope"
      requires: ["vp_engineering", "vp_product"]
      escalate_to: "ceo"
      escalation_trigger: "disagreement"

    - action: "spend_budget"
      requires_role: "department_lead"
      limit: 5000
      above_limit_escalate_to: "ceo"
```

---

## E5. Communication Architecture

This is the biggest extension. Communication is no longer just an internal
message bus — it's a multi-layer system with internal channels, external
bridges, and configurable protocols.

### E5.1 Communication Layers

```
┌─────────────────────────────────────────────────────────────┐
│                    External World                            │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌───────────────┐  │
│  │  Email   │ │  Slack   │ │ Discord  │ │   Telegram    │  │
│  │ (SMTP/   │ │  (API)   │ │  (API)   │ │   (Bot API)   │  │
│  │  IMAP)   │ │          │ │          │ │               │  │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └──────┬────────┘  │
│       │            │            │               │           │
│  ─────┴────────────┴────────────┴───────────────┴────────── │
│                  Communication Bridge (Go)                   │
│          normalize ↔ route ↔ deliver ↔ log                  │
│  ──────────────────────────────────────────────────────────  │
│                                                              │
│  ┌───────────────────────────────────────────────────────┐  │
│  │              Internal Message Bus (gRPC)               │  │
│  │                                                        │  │
│  │  ┌──────────┐  ┌──────────────┐  ┌─────────────────┐  │  │
│  │  │  Channels│  │  Direct Msgs │  │  Broadcast/Events│  │  │
│  │  │ #general │  │  1:1 between │  │  org-wide annc.  │  │  │
│  │  │ #eng     │  │  any agents  │  │  status updates  │  │  │
│  │  │ #reviews │  │              │  │                  │  │  │
│  │  └──────────┘  └──────────────┘  └─────────────────┘  │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌───────────────────────────────────────────────────────┐  │
│  │                    Agent Processes                      │  │
│  │   Sarah    Mike    Lisa    James    Anna    Raj         │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### E5.2 Internal Communication Primitives

```yaml
channels:
  # Group channel — multiple agents, persistent history
  - id: "eng-general"
    type: "group"
    name: "#engineering-general"
    members: ["ember-owl", "iron-fox", "cobalt-lark", "nova-sparrow"]
    history_visible: true               # agents can read past messages
    max_history_messages: 100           # context window management

  # Direct message — 1:1 private conversation
  - id: "sarah-mike-dm"
    type: "direct"
    participants: ["ember-owl", "iron-fox"]

  # Broadcast — one-to-many, no replies
  - id: "company-announcements"
    type: "broadcast"
    sender: "ceo"
    recipients: "all"                   # or a list of agent IDs

  # Thread — branching conversation off a channel message
  # (created dynamically at runtime, not in config)

  # Meeting — synchronous, turn-based group conversation
  - id: "sprint-planning"
    type: "meeting"
    participants: ["ember-owl", "iron-fox", "cobalt-lark", "nova-sparrow"]
    facilitator: "ember-owl"
    agenda:
      - "Review last sprint outcomes"
      - "Prioritize backlog for next sprint"
      - "Identify blockers"
    turn_order: "facilitator_controlled"   # or "round_robin", "free_form"
    max_rounds: 5
```

### E5.3 Message Format (Internal)

```protobuf
message AgentMessage {
  string message_id    = 1;
  string channel_id    = 2;
  string sender_id     = 3;
  string thread_id     = 4;      // empty if top-level
  MessageType type     = 5;
  string content       = 6;      // natural language message body
  repeated Attachment attachments = 7;
  repeated string mentions = 8;  // agent IDs mentioned with @
  string reply_to      = 9;      // message_id being replied to
  Visibility visibility = 10;
  int64 timestamp      = 11;
}

enum MessageType {
  TEXT          = 0;    // regular message
  DECISION      = 1;    // a decision that should be recorded
  QUESTION      = 2;    // expecting a response
  TASK_ASSIGN   = 3;    // assigning work to someone
  STATUS_UPDATE = 4;    // progress report
  ESCALATION    = 5;    // raising an issue to a superior
  APPROVAL_REQ  = 6;    // requesting sign-off
  APPROVAL_RESP = 7;    // granting or denying approval
  SOCIAL        = 8;    // small talk, relationship building
}

enum Visibility {
  CHANNEL    = 0;       // visible to all channel members
  PRIVATE    = 1;       // visible only to sender and explicit recipients
  CONFIDENTIAL = 2;     // encrypted, not stored in shared logs
}

message Attachment {
  string filename = 1;
  string mime_type = 2;
  bytes content    = 3;    // or a reference/path
}
```

### E5.4 External Communication Bridges

Bridges connect the internal message bus to real-world communication platforms.
Each bridge is a **bidirectional adapter**: it can send messages out and receive
messages in.

#### Bridge Configuration

```yaml
bridges:
  # Email bridge — agents can send and receive real emails
  - id: "email"
    type: "email"
    provider: "smtp"
    config:
      smtp_host: "smtp.gmail.com"
      smtp_port: 587
      imap_host: "imap.gmail.com"
      imap_port: 993
      username: "${EMAIL_USERNAME}"
      password: "${EMAIL_PASSWORD}"
      poll_interval_seconds: 30
    routing:
      # Map external senders to internal channels
      inbound:
        - match: { from: "*@client.com" }
          deliver_to: "channel:client-comms"
          notify: ["ember-owl", "pixel-heron"]
        - match: { subject_contains: "URGENT" }
          deliver_to: "channel:eng-general"
          priority: "high"
        - match: { to: "support@ourcompany.com" }
          deliver_to: "agent:support-l1"
      # Map internal agent messages to outbound emails
      outbound:
        allowed_agents: ["ember-owl", "pixel-heron"]
        require_approval: true
        approval_agent: "ceo"
        from_address: "team@ourcompany.com"
        footer: "-- Sent by AI agent via Persatrix"

  # Slack bridge
  - id: "slack"
    type: "slack"
    config:
      bot_token: "${SLACK_BOT_TOKEN}"
      app_token: "${SLACK_APP_TOKEN}"
    routing:
      channel_mapping:
        "slack:#engineering": "internal:eng-general"
        "slack:#product": "internal:product-general"
      inbound:
        - match: { channel: "#engineering", mentions_bot: true }
          deliver_to: "agent:ember-owl"
      outbound:
        allowed_agents: ["ember-owl", "iron-fox"]
        require_approval: false
        display_name_format: "{{ agent.name }} (AI)"

  # Discord bridge
  - id: "discord"
    type: "discord"
    config:
      bot_token: "${DISCORD_BOT_TOKEN}"
    routing:
      channel_mapping:
        "discord:guild123:#general": "internal:social-sim"
      outbound:
        allowed_agents: ["*"]

  # Telegram bridge
  - id: "telegram"
    type: "telegram"
    config:
      bot_token: "${TELEGRAM_BOT_TOKEN}"
    routing:
      inbound:
        - match: { chat_type: "group", chat_id: "-100123456" }
          deliver_to: "channel:telegram-team"
      outbound:
        allowed_agents: ["support-l1"]

  # Webhook bridge (generic — for any HTTP-based service)
  - id: "webhook-custom"
    type: "webhook"
    config:
      inbound_path: "/hooks/custom"       # receives POST requests
      outbound_url: "https://api.example.com/messages"
      auth:
        type: "hmac"
        secret: "${WEBHOOK_SECRET}"
```

#### Bridge Security Model

External bridges are high-risk and get additional safeguards:

```yaml
bridge_security:
  global:
    require_approval_for_outbound: true        # default: all outbound needs approval
    max_outbound_messages_per_hour: 50         # rate limiting
    content_filter:
      block_patterns:
        - "password"
        - "api_key"
        - "secret"
        - "ssn"
        - "credit card"
      pii_detection: true                      # flag potential PII before sending
    audit:
      log_all_inbound: true
      log_all_outbound: true
      log_content: false                       # log metadata only, not message bodies
      alert_on: ["outbound_approval_override", "rate_limit_hit"]
```

### E5.5 Communication Protocols

Different scenarios need different interaction patterns. These are reusable
**protocols** — predefined conversation structures:

```yaml
protocols:
  # Standup — each participant gives a brief update
  standup:
    type: "round_robin"
    participants: "channel_members"
    prompt_per_turn: |
      Share briefly: 1) What you accomplished since last standup,
      2) What you plan to do next, 3) Any blockers.
    max_turns_per_participant: 1
    facilitator_summary: true

  # Brainstorm — free-form, divergent thinking
  brainstorm:
    type: "free_form"
    participants: "channel_members"
    rules:
      - "No criticism during ideation phase"
      - "Build on others' ideas"
    phases:
      - name: "ideation"
        max_rounds: 10
        instruction: "Generate as many ideas as possible"
      - name: "evaluation"
        max_rounds: 5
        instruction: "Now critique and rank the ideas"

  # Debate — structured argumentation
  debate:
    type: "structured"
    roles:
      proposer: { count: 1, position: "for" }
      opposer: { count: 1, position: "against" }
      moderator: { count: 1, neutral: true }
      judges: { count: 3, vote_at_end: true }
    phases:
      - name: "opening_statements"
        order: ["proposer", "opposer"]
        max_tokens_per_turn: 500
      - name: "rebuttals"
        rounds: 2
        order: ["opposer", "proposer"]
      - name: "closing"
        order: ["proposer", "opposer"]
      - name: "verdict"
        agent: "judges"
        format: "vote_with_reasoning"

  # Code review — author presents, reviewers critique
  code_review:
    type: "structured"
    roles:
      author: { count: 1 }
      reviewers: { count: 2 }
    phases:
      - name: "presentation"
        agent: "author"
        instruction: "Present the changes and design rationale"
      - name: "review"
        agent: "reviewers"
        parallel: true
        instruction: "Review for correctness, style, and edge cases"
      - name: "response"
        agent: "author"
        instruction: "Address each piece of feedback"
      - name: "decision"
        agent: "reviewers"
        format: "approve_or_request_changes"

  # Escalation chain — try each level before going higher
  escalation:
    type: "sequential_fallback"
    chain:
      - agent: "support-l1"
        timeout_seconds: 300
        escalate_if: "unresolved"
      - agent: "support-l2"
        timeout_seconds: 600
        escalate_if: "unresolved"
      - agent: "engineering-oncall"
        timeout_seconds: 900
      - fallback: "human_handoff"

  # Consensus — discuss until agreement or timeout
  consensus:
    type: "free_form"
    participants: "channel_members"
    exit_conditions:
      unanimous_agreement: true
      max_rounds: 15
      timeout_seconds: 1800
    voting:
      method: "explicit"               # agents state "I agree" / "I disagree"
      threshold: 0.8                   # 80% agreement = consensus
    on_no_consensus: "escalate_to_supervisor"
```

---

## E6. Distributed Agent Mesh

The framework does not assume agents run on the same machine — or even in the
same network. A persona is a **network-addressable service** that can be
deployed anywhere: a local process, a container in the cloud, a Raspberry Pi in
someone's home lab, or a server behind a corporate firewall. As long as a
communication channel can reach it, it participates in the society.

This transforms Persatrix from a single-machine runtime into a **distributed
agent mesh**.

### E6.1 Deployment Topology

```
                    ┌───────────────────────────┐
                    │    Orchestrator (Go)       │
                    │    mesh.Persatrix.io       │
                    │                            │
                    │  Registry · Router · State │
                    └─────────┬─────────────────┘
                              │
          ┌───────────────────┼───────────────────────┐
          │                   │                        │
  ┌───────▼────────┐  ┌──────▼─────────┐  ┌──────────▼──────────┐
  │  Node A        │  │  Node B        │  │  Node C             │
  │  AWS us-east   │  │  Home lab      │  │  Corporate LAN      │
  │                │  │  Tokyo         │  │  London              │
  │  ┌──────────┐  │  │  ┌──────────┐  │  │  ┌──────────────┐   │
  │  │ Sarah    │  │  │  │ Yuki     │  │  │  │ James        │   │
  │  │ VP Eng   │  │  │  │ Designer │  │  │  │ Compliance   │   │
  │  └──────────┘  │  │  └──────────┘  │  │  └──────────────┘   │
  │  ┌──────────┐  │  │  ┌──────────┐  │  │  ┌──────────────┐   │
  │  │ Mike     │  │  │  │ Kai      │  │  │  │ Restricted   │   │
  │  │ Sr Dev   │  │  │  │ Research │  │  │  │ sub-agents   │   │
  │  └──────────┘  │  │  └──────────┘  │  │  │ (no egress)  │   │
  │                │  │                │  │  └──────────────┘   │
  └────────────────┘  └────────────────┘  └─────────────────────┘
```

There is no requirement that all nodes are alike. A node might host one agent
or twenty. A node might be powerful (GPU-enabled, for agents running local
models) or minimal (a lightweight relay that proxies to a cloud LLM API).

### E6.2 Node Architecture

A **node** is a machine (or container) running the Persatrix agent runtime. Each
node registers with the orchestrator and manages the agents deployed to it.

```yaml
node:
  id: "node-tokyo-01"
  name: "Tokyo Home Lab"
  address: "agent.tokyo.example.com:9090"
  region: "ap-northeast-1"
  
  # ─── Connectivity ────────────────────────────────
  connectivity:
    protocol: "grpc"                    # grpc | grpc-web | websocket
    tls:
      enabled: true
      cert_path: "/etc/Persatrix/certs/node.crt"
      key_path: "/etc/Persatrix/certs/node.key"
      ca_path: "/etc/Persatrix/certs/ca.crt"
    keepalive_seconds: 30
    reconnect:
      max_attempts: 10
      backoff: "exponential"            # 1s, 2s, 4s, 8s...
      max_interval_seconds: 60
  
  # ─── Network constraints ─────────────────────────
  network:
    nat_traversal: true                 # node is behind NAT
    relay_through_orchestrator: true    # if direct P2P fails, relay via orchestrator
    allowed_peers: ["node-aws-01"]      # direct P2P only with specific nodes
    denied_peers: []
    firewall:
      inbound: [9090]                   # gRPC port
      outbound:
        allow: ["api.anthropic.com:443", "mesh.Persatrix.io:443"]
        deny: ["*"]
  
  # ─── Resources ───────────────────────────────────
  resources:
    cpu_cores: 4
    memory_gb: 16
    gpu: "none"                         # or "nvidia-a100", etc.
    local_models: []                    # for nodes running local LLMs
    max_agents: 5                       # capacity limit
    
  # ─── Agents deployed here ────────────────────────
  agents:
    - id: "orbit-kite"
    - id: "kai-researcher"
```

### E6.3 Agent Addressing & Discovery

Every agent in the mesh has a **globally unique address**:

```
<agent_id>@<node_id>

Examples:
  ember-owl@node-aws-01
  orbit-kite@node-tokyo-01
  james-compliance@node-london-01
```

The orchestrator maintains a **live registry** mapping agent IDs to their
current node. When an agent sends a message to another agent, it uses only the
agent ID — the orchestrator resolves the location transparently.

```
Sarah sends DM to Yuki
  │
  ├─ Ember's node (AWS) sends to orchestrator: "deliver to orbit-kite"
  ├─ Orchestrator looks up registry: orbit-kite → node-tokyo-01
  ├─ Orchestrator routes message to Node Tokyo
  └─ Node Tokyo delivers to Yuki's agent process
```

**Agent mobility**: agents can be migrated between nodes (e.g., for load
balancing or failover). The orchestrator updates the registry, and in-flight
messages are re-routed. The agent's state (memory, conversation history) travels
with it.

```yaml
# Migration command
persatrix agent migrate orbit-kite --from node-tokyo-01 --to node-aws-02
```

### E6.4 Communication Over the Mesh

Internal channels and DMs work transparently across nodes. But the mesh
introduces new realities:

#### Latency-Aware Routing

```yaml
mesh:
  routing:
    strategy: "latency_aware"           # or "nearest", "round_robin", "affinity"
    
    # Prefer co-located agents for high-frequency interactions
    affinity_rules:
      - agents: ["ember-owl", "iron-fox"]
        prefer_same_node: true
        reason: "frequent code review exchanges"
      
      - channel: "eng-general"
        prefer_same_region: true
        reason: "reduce latency for team chat"
    
    # Latency thresholds
    thresholds:
      acceptable_ms: 200                # normal operation
      degraded_ms: 1000                 # log warning, consider migration
      timeout_ms: 5000                  # mark node as unreachable
```

#### Offline & Partition Tolerance

Nodes can go offline. The mesh must handle this gracefully:

```yaml
mesh:
  partition_handling:
    # What happens when a node becomes unreachable
    on_node_disconnect:
      grace_period_seconds: 30           # wait before declaring offline
      action: "queue_messages"           # queue | drop | reroute_to_backup
      max_queue_size: 1000               # messages held for offline node
      queue_ttl_seconds: 3600            # discard after 1 hour

    # Agents on disconnected nodes
    on_agent_unreachable:
      notify_channels: true              # post "Yuki is offline" in their channels
      reassign_pending_tasks: true       # give their tasks to someone else
      backup_agent: null                 # optional standby agent ID

    # Split-brain: two nodes can't reach each other but both reach orchestrator
    on_split_brain:
      strategy: "orchestrator_authoritative"  # orchestrator's view is truth
```

#### Message Delivery Guarantees

```yaml
mesh:
  delivery:
    guarantee: "at_least_once"          # at_least_once | at_most_once | exactly_once
    # at_least_once: retry until ACK (default, good for most cases)
    # at_most_once: fire and forget (for non-critical broadcasts)
    # exactly_once: dedup via message IDs (expensive, for critical decisions)
    
    retry:
      max_attempts: 5
      backoff: "exponential"
      max_interval_seconds: 30
    
    ordering:
      per_channel: "causal"             # messages in a channel maintain causal order
      cross_channel: "none"             # no ordering guarantee across channels
```

### E6.5 Distributed Security

Distribution multiplies the attack surface. New threats and mitigations:

#### Node Authentication

Every node must prove its identity to join the mesh:

```yaml
mesh:
  security:
    # Mutual TLS — nodes and orchestrator authenticate each other
    authentication:
      method: "mtls"                    # mtls | token | certificate_pinning
      ca: "/etc/Persatrix/certs/ca.crt"
      node_cert: "/etc/Persatrix/certs/node.crt"
      node_key: "/etc/Persatrix/certs/node.key"
      # Alternatively, short-lived tokens for simpler setups
      # method: "token"
      # token: "${NODE_AUTH_TOKEN}"
      # token_rotation_hours: 24
    
    # Node admission — who is allowed to join the mesh
    admission:
      mode: "allowlist"                 # allowlist | open_with_approval | open
      allowed_nodes:
        - node_id: "node-aws-01"
          fingerprint: "sha256:abc123..."
        - node_id: "node-tokyo-01"
          fingerprint: "sha256:def456..."
      require_approval_for_new: true    # human must approve unknown nodes
```

#### Cross-Node Permission Boundaries

Agents on different nodes may operate under different trust domains:

```yaml
mesh:
  trust_domains:
    - id: "trusted-internal"
      nodes: ["node-aws-01", "node-aws-02"]
      trust_level: "full"               # agents can freely interact
    
    - id: "partner-network"
      nodes: ["node-tokyo-01"]
      trust_level: "restricted"
      restrictions:
        - "no access to channels tagged 'confidential'"
        - "outbound messages are content-filtered"
        - "sub-agent spawning limited to depth 1"
        - "no access to MCP servers on other nodes"
    
    - id: "sandbox"
      nodes: ["node-experiment-01"]
      trust_level: "isolated"
      restrictions:
        - "can only communicate within its own node"
        - "no external bridge access"
        - "all traffic logged and inspectable"
```

#### Data Residency & Sovereignty

Some agents handle data that cannot leave a region:

```yaml
mesh:
  data_residency:
    rules:
      - agent_id: "james-compliance"
        data_classification: "pii-eu"
        must_stay_in: ["eu-west-1", "eu-central-1"]
        cannot_transit: ["us-*", "cn-*"]
      
      - channel_id: "legal-privileged"
        data_classification: "attorney-client"
        must_stay_in: ["node-london-01"]   # pinned to specific node
        encryption: "end_to_end"           # encrypted in transit AND at rest
      
      - tag: "financial"
        data_classification: "sox-regulated"
        audit_all_access: true
        retention_days: 2555               # 7 years
```

#### End-to-End Encryption

For sensitive channels, messages are encrypted so that even the orchestrator
(which routes them) cannot read the content:

```yaml
channels:
  - id: "board-discussions"
    type: "group"
    encryption:
      type: "e2e"                        # end-to-end; orchestrator sees only metadata
      key_exchange: "x25519"
      rotate_keys_every: "24h"
    members: ["ceo", "cfo", "cto"]
    # Orchestrator can route these messages but cannot decrypt them.
    # Audit logs record message metadata (sender, timestamp, channel)
    # but NOT content.
```

### E6.6 Node Deployment Models

The framework supports multiple deployment strategies to fit different setups:

```yaml
# ─── Model 1: All-in-one (development / small simulations) ────────
# Everything on one machine. No mesh networking needed.
deployment:
  mode: "local"
  # All agents run as processes on the same host.
  # Communication is local gRPC (loopback).
  # This is the MVP default.

# ─── Model 2: Hub-and-spoke (typical production) ──────────────────
# Central orchestrator, agents on multiple nodes.
deployment:
  mode: "hub_and_spoke"
  orchestrator:
    address: "mesh.Persatrix.io:443"
  nodes:
    - { id: "node-aws-01", address: "10.0.1.10:9090" }
    - { id: "node-tokyo-01", address: "agent.tokyo.example.com:9090" }
  # All traffic routes through the orchestrator.
  # Simple but orchestrator is a bottleneck / single point of failure.

# ─── Model 3: Full mesh (large-scale / low-latency) ──────────────
# Nodes communicate directly with each other when possible.
deployment:
  mode: "full_mesh"
  orchestrator:
    address: "mesh.Persatrix.io:443"
    role: "registry_and_fallback"       # only used for discovery + relay
  nodes:
    - { id: "node-aws-01", address: "10.0.1.10:9090", peer_address: "node-aws-01.mesh.local:9091" }
    - { id: "node-tokyo-01", address: "agent.tokyo.example.com:9090", peer_address: "tokyo.mesh.local:9091" }
  peer_to_peer:
    enabled: true
    discovery: "orchestrator"           # nodes learn peer addresses from orchestrator
    fallback: "relay_via_orchestrator"  # if direct P2P fails
    encryption: "wireguard"             # or "mtls"

# ─── Model 4: Federated (multi-organization) ─────────────────────
# Multiple independent orchestrators peer with each other.
# Each org controls its own agents, but they can interact across orgs.
deployment:
  mode: "federated"
  local_orchestrator:
    address: "mesh.acme.com:443"
  federation:
    peers:
      - id: "partner-org"
        orchestrator: "mesh.partner.com:443"
        trust: "restricted"
        shared_channels: ["cross-org-project"]
        allowed_interactions:
          - { local_agent: "ember-owl", remote_agent: "partner:orbit-dev" }
    # Agents from different orgs can collaborate in shared channels
    # but cannot see each other's internal channels or org structure.
```

### E6.7 Mesh-Aware CLI

```bash
# Node management
persatrix node register --config node.yaml
persatrix node list
persatrix node status node-tokyo-01
persatrix node drain node-tokyo-01          # gracefully migrate agents off this node

# Agent placement
persatrix agent deploy ember-owl --to node-aws-01
persatrix agent migrate orbit-kite --from node-tokyo-01 --to node-aws-02
persatrix agent locate ember-owl           # → ember-owl@node-aws-01

# Mesh diagnostics
persatrix mesh status                       # show all nodes, latencies, health
persatrix mesh ping node-tokyo-01           # measure RTT to a node
persatrix mesh trace ember-owl orbit-kite # show routing path between two agents
```

### E6.8 Sub-Agents in a Distributed Context

When a persona agent spawns a sub-agent, where does it run?

```yaml
sub_agents:
  placement:
    strategy: "co_locate"              # co_locate | any | specific_node
    # co_locate (default): sub-agent runs on the same node as parent
    # any: orchestrator picks the best available node
    # specific_node: parent specifies which node

    # Override per sub-agent request
    allow_remote_sub_agents: false     # MVP default: always co-locate
    
    # Post-MVP: remote sub-agents for specialized hardware
    # e.g., Sarah on AWS spawns a data_analyst sub-agent on a GPU node
    remote_rules:
      - template: "data_analyst"
        prefer_node_with: "gpu"
      - template: "translator"
        prefer_region: "same_as_parent"
```

For MVP, sub-agents always co-locate with their parent. This avoids the
complexity of cross-node sub-agent lifecycle management while still enabling
distributed persona agents.

---

## E7. Memory & Shared Knowledge

Agents in a society need persistent memory — not just within a single task.

> **Cross-reference**: E7 defines the memory *architecture* (tiers and
> configuration). E9.7 defines memory *compression* (how memory is optimized
> over time). E10.4 defines the *embedding infrastructure* for memory retrieval.
> These three sections work together.

### E7.1 Memory Tiers

```
┌──────────────────────────────────────────────┐
│           Memory Architecture                 │
│                                               │
│  ┌─────────────────────────────────────────┐  │
│  │  Working Memory (per-conversation)      │  │
│  │  Current channel/thread context window  │  │
│  │  Managed by: LLM context               │  │
│  └─────────────────────────────────────────┘  │
│                                               │
│  ┌─────────────────────────────────────────┐  │
│  │  Episodic Memory (per-agent)            │  │
│  │  Summaries of past conversations        │  │
│  │  Key decisions, outcomes, lessons        │  │
│  │  Managed by: vector store + summarizer  │  │
│  └─────────────────────────────────────────┘  │
│                                               │
│  ┌─────────────────────────────────────────┐  │
│  │  Shared Knowledge Base (org-wide)       │  │
│  │  Documents, decisions, policies          │  │
│  │  Accessible by all agents with perms    │  │
│  │  Managed by: document store + search    │  │
│  └─────────────────────────────────────────┘  │
│                                               │
│  ┌─────────────────────────────────────────┐  │
│  │  Relationship Memory (per-agent-pair)   │  │
│  │  Trust scores, interaction history       │  │
│  │  Patterns: "Mike is always late"         │  │
│  │  Managed by: relationship tracker       │  │
│  └─────────────────────────────────────────┘  │
└──────────────────────────────────────────────┘
```

### E7.2 Memory Config

```yaml
memory:
  episodic:
    enabled: true
    backend: "sqlite"                    # MVP; post-MVP: vector store
    summarize_after_messages: 50         # auto-summarize long conversations
    retention_days: 90
  shared_knowledge:
    enabled: true
    backend: "filesystem"               # MVP: local files; post-MVP: search index
    path: "/workspace/knowledge_base"
  relationship:
    enabled: true
    update_frequency: "after_each_interaction"
    decay_rate: 0.01                    # trust drifts toward neutral over time
```

---

## E8. AgentOps & Modern Standards Compatibility

The framework must be a **good citizen** of the emerging agent infrastructure
ecosystem. This means native support for industry-standard observability,
interoperability protocols, and operational tooling — not as afterthoughts, but
as core architectural layers.

### E8.1 Design Philosophy

Persatrix does not reinvent observability, tracing, or inter-agent
communication. Instead, it emits **standards-compliant telemetry** and supports
**protocol adapters** that let it plug into whatever tooling or ecosystem the
user already has. The framework is opinionated about its internal model (personas,
channels, sub-agents) but unopinionated about how you observe and integrate it.

```
┌────────────────────────────────────────────────────────────────────────┐
│                     Persatrix Agent Runtime                            │
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │                 Instrumentation Layer                             │  │
│  │                                                                   │  │
│  │  ┌───────────────┐ ┌───────────────┐ ┌────────────────────────┐  │  │
│  │  │  OTEL Traces  │ │ OTEL Metrics  │ │   OTEL Logs/Events     │  │  │
│  │  │  (GenAI +     │ │ (token usage, │ │   (agent decisions,    │  │  │
│  │  │   Agent       │ │  latency,     │ │    tool calls,         │  │  │
│  │  │   semconv)    │ │  cost, error  │ │    state changes)      │  │  │
│  │  │               │ │  rates)       │ │                        │  │  │
│  │  └──────┬────────┘ └──────┬────────┘ └───────────┬────────────┘  │  │
│  └─────────┼─────────────────┼──────────────────────┼───────────────┘  │
│            │                 │                      │                   │
│  ┌─────────▼─────────────────▼──────────────────────▼───────────────┐  │
│  │               OTLP Exporter (configurable)                        │  │
│  └──────────┬────────────┬───────────┬──────────────┬───────────────┘  │
└─────────────┼────────────┼───────────┼──────────────┼──────────────────┘
              │            │           │              │
       ┌──────▼──────┐ ┌──▼────┐ ┌────▼─────┐ ┌─────▼────────┐
       │  AgentOps   │ │Jaeger │ │ Langfuse │ │  Datadog     │
       │  Dashboard  │ │       │ │          │ │  LLM Obs.    │
       └─────────────┘ └───────┘ └──────────┘ └──────────────┘
                    ┌──────────────┐ ┌───────────────┐
                    │  LangSmith   │ │  Prometheus   │
                    │              │ │  + Grafana    │
                    └──────────────┘ └───────────────┘
```

### E8.2 OpenTelemetry as the Native Telemetry Layer

The framework emits all telemetry using **OpenTelemetry (OTEL)** with the
**GenAI Semantic Conventions** — the emerging standard for AI agent
observability. This means any OTEL-compatible backend receives structured,
meaningful data without custom integration.

#### Span Hierarchy

Every execution produces a trace with a well-defined span tree:

```
Trace: workflow execution "feature-builder-run-42"
│
├─ Span: invoke_agent "ember-owl" (kind: CLIENT)
│  │  gen_ai.agent.name: "ember-owl"
│  │  gen_ai.operation.name: "invoke_agent"
│  │  Persatrix.agent.persona.title: "VP of Engineering"
│  │  Persatrix.agent.autonomy_level: "semi-autonomous"
│  │
│  ├─ Span: gen_ai.chat "claude-sonnet-4" (kind: CLIENT)
│  │  │  gen_ai.request.model: "claude-sonnet-4-20250514"
│  │  │  gen_ai.usage.input_tokens: 2340
│  │  │  gen_ai.usage.output_tokens: 890
│  │  │  gen_ai.response.finish_reason: "tool_use"
│  │  │
│  │  └─ Event: gen_ai.content.prompt (opt-in)
│  │     Event: gen_ai.content.completion (opt-in)
│  │
│  ├─ Span: gen_ai.tool "file_read" (kind: INTERNAL)
│  │     gen_ai.tool.name: "file_read"
│  │     gen_ai.tool.call.id: "call_abc123"
│  │     Persatrix.tool.tier: "builtin"
│  │     Persatrix.permission.check: "allowed"
│  │
│  ├─ Span: gen_ai.tool "mcp:github/get_pull_request" (kind: CLIENT)
│  │     gen_ai.tool.name: "get_pull_request"
│  │     Persatrix.tool.tier: "mcp"
│  │     Persatrix.mcp.server_id: "github"
│  │
│  └─ Span: invoke_agent "sub:code_reviewer" (kind: INTERNAL)
│     │  gen_ai.agent.name: "code_reviewer"
│     │  Persatrix.sub_agent: true
│     │  Persatrix.sub_agent.parent: "ember-owl"
│     │  Persatrix.sub_agent.depth: 1
│     │
│     ├─ Span: gen_ai.chat "claude-sonnet-4" (kind: CLIENT)
│     │     gen_ai.usage.input_tokens: 5200
│     │     gen_ai.usage.output_tokens: 1200
│     │
│     └─ Span: gen_ai.tool "git_diff" (kind: INTERNAL)
│
├─ Span: channel.message "eng-general" (kind: INTERNAL)
│     Persatrix.channel.id: "eng-general"
│     Persatrix.message.type: "DECISION"
│     Persatrix.message.sender: "ember-owl"
│
└─ Span: invoke_agent "iron-fox" (kind: CLIENT)
      ...
```

#### Custom Semantic Attributes

Persatrix extends OTEL GenAI conventions with framework-specific attributes
under the `Persatrix.*` namespace:

```yaml
# Agent attributes
Persatrix.agent.persona.title: str        # "VP of Engineering"
Persatrix.agent.autonomy_level: str       # "semi-autonomous"
Persatrix.agent.mood: str                 # "focused" (if dynamic state enabled)
Persatrix.agent.node_id: str              # "node-aws-01" (distributed mesh)

# Sub-agent attributes
Persatrix.sub_agent: bool                 # true if this is an ephemeral sub-agent
Persatrix.sub_agent.parent: str           # parent agent ID
Persatrix.sub_agent.depth: int            # nesting depth (0 = persona, 1 = sub, etc.)
Persatrix.sub_agent.template: str         # "code_reviewer", "researcher", etc.

# Tool attributes
Persatrix.tool.tier: str                  # "builtin" | "custom" | "mcp"
Persatrix.tool.mcp_server: str            # MCP server ID (if tier=mcp)
Persatrix.permission.check: str           # "allowed" | "denied"
Persatrix.permission.denial_reason: str   # why access was denied

# Communication attributes
Persatrix.channel.id: str                 # channel identifier
Persatrix.channel.type: str               # "group" | "direct" | "broadcast"
Persatrix.message.type: str               # "TEXT" | "DECISION" | "ESCALATION" etc.
Persatrix.message.visibility: str         # "channel" | "private" | "confidential"
Persatrix.protocol.name: str              # "standup" | "debate" | "consensus"
Persatrix.protocol.phase: str             # current phase within protocol

# Organization attributes
Persatrix.org.id: str                     # organization identifier
Persatrix.org.topology: str               # "hierarchy" | "flat" | "matrix"
Persatrix.delegation.from: str            # agent who delegated
Persatrix.delegation.to: str              # agent who received delegation

# Bridge attributes (external comms)
Persatrix.bridge.id: str                  # "email" | "slack" | "discord"
Persatrix.bridge.direction: str           # "inbound" | "outbound"
Persatrix.bridge.approval_status: str     # "pending" | "approved" | "denied"

# Cost attributes
Persatrix.cost.usd: float                # estimated cost of this operation
Persatrix.cost.budget_remaining: float   # remaining budget for this agent/workflow
```

### E8.3 Metrics

The framework exports OTEL metrics for real-time monitoring and alerting:

```yaml
metrics:
  # ─── LLM Usage ──────────────────────────────
  gen_ai.client.token.usage:               # histogram, per model/agent
    dimensions: [agent_id, model, operation]
  gen_ai.client.operation.duration:        # histogram, seconds
    dimensions: [agent_id, model, operation, status]

  # ─── Cost ───────────────────────────────────
  Persatrix.cost.total:                    # counter, USD
    dimensions: [agent_id, model, workflow_id]
  Persatrix.cost.by_tier:                  # counter, USD
    dimensions: [tier]                     # builtin, custom, mcp, llm

  # ─── Agent Activity ─────────────────────────
  Persatrix.agent.actions.total:           # counter
    dimensions: [agent_id, action_type]    # llm_call, tool_use, message_send, delegate, spawn_sub
  Persatrix.agent.active_count:            # gauge
    dimensions: [node_id, autonomy_level]
  Persatrix.sub_agent.spawns.total:        # counter
    dimensions: [parent_agent_id, template]
  Persatrix.sub_agent.active_count:        # gauge
    dimensions: [parent_agent_id]

  # ─── Communication ──────────────────────────
  Persatrix.messages.total:                # counter
    dimensions: [channel_id, message_type, sender_id]
  Persatrix.messages.latency:              # histogram, seconds
    dimensions: [channel_id, source_node, dest_node]
  Persatrix.bridge.messages.total:         # counter
    dimensions: [bridge_id, direction, approval_status]

  # ─── Workflow / Task ────────────────────────
  Persatrix.workflow.duration:             # histogram, seconds
    dimensions: [workflow_id, status]
  Persatrix.task.duration:                 # histogram, seconds
    dimensions: [agent_id, status]
  Persatrix.task.retries.total:            # counter
    dimensions: [agent_id, workflow_id]

  # ─── Security ───────────────────────────────
  Persatrix.permission.checks.total:       # counter
    dimensions: [agent_id, result]         # allowed, denied
  Persatrix.permission.denials.total:      # counter (alert on this)
    dimensions: [agent_id, tool, reason]

  # ─── Mesh (distributed) ─────────────────────
  Persatrix.mesh.node.health:              # gauge (0=down, 1=degraded, 2=healthy)
    dimensions: [node_id, region]
  Persatrix.mesh.message.latency:          # histogram, milliseconds
    dimensions: [source_node, dest_node]
  Persatrix.mesh.queue.depth:              # gauge
    dimensions: [node_id]                  # messages queued for offline nodes
```

### E8.4 Session Replay & Time-Travel Debugging

Inspired by AgentOps and similar platforms, the framework supports **session
replay** — the ability to step through an entire agent execution after the fact.

```yaml
observability:
  session_replay:
    enabled: true
    storage: "sqlite"                     # MVP; post-MVP: object store
    capture:
      spans: true                         # always
      llm_inputs: false                   # opt-in (privacy-sensitive)
      llm_outputs: false                  # opt-in
      tool_inputs: true
      tool_outputs: true
      messages: true                      # inter-agent messages
      state_snapshots: true               # agent state at each decision point
    retention_days: 30
```

The replay data is structured for both **programmatic access** (API/CLI) and
**external dashboard integration**:

```bash
# CLI replay
persatrix replay session-abc123                 # step-by-step in terminal
persatrix replay session-abc123 --agent sarah   # filter to one agent's perspective
persatrix replay session-abc123 --from step:15  # start from a specific step
persatrix replay session-abc123 --export json   # export for external tools

# Export to AgentOps dashboard
persatrix export session-abc123 --format agentops --endpoint https://app.agentops.ai
```

### E8.5 Evaluation & Quality Gates

The framework integrates evaluation as a first-class lifecycle phase, not just
post-hoc analysis. Evaluations can run inline (during execution) or offline
(after the fact).

```yaml
evaluations:
  # ─── Inline evaluators (run during execution) ─────
  inline:
    - id: "output_schema_check"
      trigger: "on_agent_output"
      type: "deterministic"
      rule: "output matches expected JSON schema"
      on_fail: "retry"                      # retry | warn | abort

    - id: "safety_check"
      trigger: "on_bridge_outbound"
      type: "llm_judge"
      model: "claude-haiku-4-5-20251001"
      prompt: "Is this message safe and appropriate to send externally?"
      on_fail: "block_and_alert"

    - id: "hallucination_check"
      trigger: "on_agent_output"
      type: "llm_judge"
      model: "claude-haiku-4-5-20251001"
      prompt: "Does this response contain claims not supported by the provided context?"
      on_fail: "warn"

  # ─── Offline evaluators (run after execution) ──────
  offline:
    - id: "task_quality"
      type: "llm_judge"
      model: "claude-sonnet-4-20250514"
      criteria:
        - "correctness"
        - "completeness"
        - "relevance"
      output: "score_0_to_1"

    - id: "conversation_quality"
      type: "custom"
      script: "evaluators/conversation_scorer.py"
      metrics: ["coherence", "goal_progress", "persona_consistency"]

  # ─── Evaluation export ─────────────────────────────
  export:
    format: "otel_events"                   # gen_ai.evaluation events per OTEL semconv
    also_export_to:
      - type: "langfuse"
        endpoint: "${LANGFUSE_ENDPOINT}"
        public_key: "${LANGFUSE_PUBLIC_KEY}"
      - type: "csv"
        path: "/workspace/eval_results/"
```

### E8.6 A2A Protocol Compatibility

The **Agent2Agent (A2A) protocol**, launched by Google and now under the Linux
Foundation, is the emerging standard for inter-agent communication across
vendors and platforms. Persatrix supports A2A at two levels:

#### Level 1: Persatrix Agents as A2A Servers

Any persona agent can be exposed as an A2A-compatible remote agent, allowing
external agents (from any framework) to discover and collaborate with it.

```yaml
a2a:
  server:
    enabled: true
    endpoint: "https://agents.example.com/a2a"

    # Each exposed agent gets an Agent Card
    exposed_agents:
      - agent_id: "ember-owl"
        agent_card:
          name: "Ember Owl - VP Engineering"
          description: "Engineering leadership agent. Can review architectures, prioritize work, and coordinate development."
          url: "https://agents.example.com/a2a/ember-owl"
          version: "1.0.0"
          capabilities:
            streaming: true
            pushNotifications: true
          skills:
            - id: "architecture_review"
              name: "Architecture Review"
              description: "Review and critique system architecture proposals"
              tags: ["engineering", "architecture"]
            - id: "sprint_planning"
              name: "Sprint Planning"
              description: "Plan and prioritize sprint work items"
              tags: ["management", "agile"]
          authentication:
            schemes: ["bearer"]
          # A2A protocol maps to internal Persatrix concepts:
          # A2A Task → Persatrix TaskInput to the persona agent
          # A2A Message → Persatrix channel message
          # A2A Artifact → Persatrix TaskOutput attachment

      - agent_id: "support-l1"
        agent_card:
          name: "L1 Support Agent"
          description: "First-line customer support triage"
          skills:
            - id: "ticket_triage"
              name: "Ticket Triage"
              description: "Classify and route support tickets"
```

#### Level 2: External A2A Agents as Peers

Persatrix can discover and delegate to **external A2A agents** from other
platforms, treating them as peers in the organization.

```yaml
a2a:
  client:
    enabled: true
    external_agents:
      # Discover agents from an A2A-compatible endpoint
      - discovery_url: "https://partner.com/.well-known/agent.json"
        trust_level: "restricted"
        alias: "partner-sales-agent"          # reference as this in workflows
        allowed_skills: ["lead_scoring"]      # whitelist specific capabilities

      # Manually registered external agent
      - agent_card_url: "https://api.servicenow.com/a2a/incident-agent/agent.json"
        trust_level: "restricted"
        alias: "servicenow-incidents"
        authentication:
          type: "oauth2"
          token_url: "${SERVICENOW_TOKEN_URL}"
          client_id: "${SERVICENOW_CLIENT_ID}"
          client_secret: "${SERVICENOW_CLIENT_SECRET}"
```

#### A2A + MCP Complementary Stack

The framework implements the full modern agent communication stack:

```
┌────────────────────────────────────────────────┐
│         Persatrix Communication Stack           │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │  A2A Protocol                             │   │
│  │  Agent ↔ Agent (cross-vendor/platform)    │   │
│  │  Discovery, task delegation, artifacts    │   │
│  └──────────────────────────────────────────┘   │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │  Internal Channels (Persatrix-native)     │   │
│  │  Agent ↔ Agent (within the society)       │   │
│  │  DMs, groups, meetings, protocols         │   │
│  └──────────────────────────────────────────┘   │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │  MCP (Model Context Protocol)             │   │
│  │  Agent ↔ Tools/Resources                  │   │
│  │  File systems, databases, APIs            │   │
│  └──────────────────────────────────────────┘   │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │  External Bridges                         │   │
│  │  Agent ↔ Human (via real-world channels)  │   │
│  │  Email, Slack, Discord, Telegram          │   │
│  └──────────────────────────────────────────┘   │
└────────────────────────────────────────────────┘
```

### E8.7 Observability Platform Integration

The framework does not lock you into any observability vendor. All telemetry is
OTEL-native, but the framework also provides **direct exporters** for popular
platforms:

```yaml
observability:
  # ─── Core: OpenTelemetry ───────────────────────
  otlp:
    enabled: true
    endpoint: "http://localhost:4318"        # OTLP HTTP endpoint
    protocol: "http/protobuf"               # or "grpc"
    headers:
      Authorization: "Bearer ${OTEL_TOKEN}"
    resource_attributes:
      service.name: "Persatrix"
      deployment.environment: "production"
    sampling:
      strategy: "parent_based_always_on"    # or ratio-based for high-volume
      ratio: 1.0

  # ─── Optional: Direct integrations ─────────────
  integrations:
    agentops:
      enabled: false
      api_key: "${AGENTOPS_API_KEY}"
      # Maps Persatrix sessions → AgentOps sessions
      # Maps agent runs → AgentOps agent spans
      # Maps tool calls → AgentOps tool events
      # Enables session replay in AgentOps dashboard

    langfuse:
      enabled: false
      public_key: "${LANGFUSE_PUBLIC_KEY}"
      secret_key: "${LANGFUSE_SECRET_KEY}"
      host: "https://cloud.langfuse.com"
      # Maps traces → Langfuse traces
      # Maps evaluations → Langfuse scores

    langsmith:
      enabled: false
      api_key: "${LANGSMITH_API_KEY}"
      project: "Persatrix"

    datadog:
      enabled: false
      # Uses OTEL GenAI semconv v1.37+ natively
      # Datadog Agent with OTLP ingest, or direct OTLP export
      otlp_endpoint: "http://datadog-agent:4318"

  # ─── Content capture policy ────────────────────
  content_capture:
    llm_prompts: false                       # NEVER in production by default
    llm_responses: false                     # opt-in only
    tool_inputs: true                        # generally safe
    tool_outputs: true
    agent_messages: true
    # Override per environment
    environments:
      development:
        llm_prompts: true
        llm_responses: true
      staging:
        llm_prompts: false
        llm_responses: true
      production:
        llm_prompts: false
        llm_responses: false
```

### E8.8 Cost Management

Token spend is a critical operational concern. The framework tracks cost at
every level and supports budget enforcement.

> **Cross-reference**: Cost is handled across three sections. The main spec
> §6.3 defines *resource limits* (max tokens per task). E8.8 (this section)
> defines *budget enforcement and reporting*. E9.2 defines *model tiering*
> (cost reduction via smart model routing). Together they form the cost stack:
> limits → routing → tracking → enforcement.

```yaml
cost:
  # ─── Price table (auto-updated or manual) ──────
  pricing:
    source: "manual"                         # manual | auto (fetch from provider APIs)
    models:
      "claude-sonnet-4-20250514":
        input_per_1m_tokens: 3.00
        output_per_1m_tokens: 15.00
      "claude-haiku-4-5-20251001":
        input_per_1m_tokens: 0.80
        output_per_1m_tokens: 4.00

  # ─── Budgets ───────────────────────────────────
  budgets:
    global:
      max_daily_usd: 100.00
      alert_at_percent: [50, 80, 95]
      on_exceed: "pause_and_alert"           # pause_and_alert | warn_only | hard_stop

    per_workflow:
      default_max_usd: 10.00
      overrides:
        "feature-builder": 25.00
        "social-experiment": 50.00

    per_agent:
      default_max_usd: 5.00
      overrides:
        "ember-owl": 20.00                 # supervisors get bigger budgets
        "support-l1": 2.00

  # ─── Cost attribution ─────────────────────────
  attribution:
    group_by: ["workflow_id", "agent_id", "model", "tool_tier"]
    export: "csv"
    export_interval: "daily"
    export_path: "/workspace/cost_reports/"
```

### E8.9 AgentOps Lifecycle Integration

The framework maps cleanly to the four-phase AgentOps lifecycle:

```
┌─────────────────────────────────────────────────────────────┐
│                  AgentOps Lifecycle                           │
│                                                               │
│  ┌─────────────┐   ┌─────────────┐   ┌──────────────────┐   │
│  │ DEVELOPMENT │ → │   TESTING   │ → │   MONITORING     │   │
│  │             │   │             │   │                   │   │
│  │ Define      │   │ Sandbox     │   │ OTEL traces +    │   │
│  │ personas,   │   │ execution,  │   │ metrics to any   │   │
│  │ workflows,  │   │ eval gates, │   │ backend. Session  │   │
│  │ channels    │   │ mock        │   │ replay. Cost     │   │
│  │ in YAML     │   │ bridges,    │   │ tracking. Budget │   │
│  │             │   │ deterministic│  │ alerts.          │   │
│  │ Local mode, │   │ + LLM-judge │   │                   │   │
│  │ full content│   │ evaluators  │   │ Content capture   │   │
│  │ capture     │   │             │   │ off by default    │   │
│  └─────────────┘   └─────────────┘   └──────────────────┘   │
│                                           │                   │
│                                           ▼                   │
│                                      ┌──────────────────┐    │
│                                      │    FEEDBACK      │    │
│                                      │                   │    │
│                                      │ Eval results fed  │    │
│                                      │ back to prompt    │    │
│                                      │ tuning. Persona   │    │
│                                      │ refinement. Cost  │    │
│                                      │ optimization via  │    │
│                                      │ model downgrades  │    │
│                                      │ for sub-agents.   │    │
│                                      └──────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

```yaml
# Environment-specific config (dev → staging → prod)
environments:
  development:
    deployment: "local"
    observability:
      content_capture: { llm_prompts: true, llm_responses: true }
      session_replay: { enabled: true, retention_days: 7 }
    evaluations:
      inline: ["output_schema_check"]
    cost:
      budgets: { global: { max_daily_usd: 10.00 } }
    bridges:
      mock: true                              # simulate external bridges

  staging:
    deployment: "hub_and_spoke"
    observability:
      content_capture: { llm_prompts: false, llm_responses: true }
      session_replay: { enabled: true, retention_days: 14 }
    evaluations:
      inline: ["output_schema_check", "safety_check"]
      offline: ["task_quality", "persona_consistency"]
    cost:
      budgets: { global: { max_daily_usd: 50.00 } }
    bridges:
      mock: false                             # real bridges, approval required

  production:
    deployment: "hub_and_spoke"               # or "full_mesh"
    observability:
      content_capture: { llm_prompts: false, llm_responses: false }
      session_replay: { enabled: true, retention_days: 30 }
      integrations: { datadog: { enabled: true } }
    evaluations:
      inline: ["output_schema_check", "safety_check", "hallucination_check"]
    cost:
      budgets: { global: { max_daily_usd: 500.00, on_exceed: "hard_stop" } }
    bridges:
      mock: false
```

---

## E9. Optimization & Compression

In a multi-agent society, unoptimized operation leads to exponential cost and
latency growth. Every agent maintains a context window. Every message adds
tokens. Every sub-agent spawn is a new LLM call. Every channel accumulates
history. Without deliberate optimization at every layer, a 10-agent simulation
running for an hour could consume millions of tokens and hundreds of dollars.

The framework treats optimization as an **architectural concern**, not a tuning
afterthought.

### E9.1 Optimization Layers

```
┌──────────────────────────────────────────────────────────────────┐
│                    Optimization Stack                             │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │  Layer 7: Wire Compression                                 │   │
│  │  gRPC compression, binary protobuf, mesh traffic reduction │   │
│  ├────────────────────────────────────────────────────────────┤   │
│  │  Layer 6: Communication Optimization                       │   │
│  │  Channel history summarization, message dedup, batching    │   │
│  ├────────────────────────────────────────────────────────────┤   │
│  │  Layer 5: Memory Compression                               │   │
│  │  Episodic memory summarization, relationship decay, prune  │   │
│  ├────────────────────────────────────────────────────────────┤   │
│  │  Layer 4: Context Window Management                        │   │
│  │  Sliding window, priority-based retention, summarization   │   │
│  ├────────────────────────────────────────────────────────────┤   │
│  │  Layer 3: LLM Call Optimization                            │   │
│  │  Response caching, semantic dedup, prompt compression      │   │
│  ├────────────────────────────────────────────────────────────┤   │
│  │  Layer 2: Model Tiering                                    │   │
│  │  Right-size models per task: Haiku for triage, Sonnet for  │   │
│  │  reasoning, sub-agents on cheaper models                   │   │
│  ├────────────────────────────────────────────────────────────┤   │
│  │  Layer 1: Execution Optimization                           │   │
│  │  Parallel fan-out, lazy evaluation, short-circuit on early │   │
│  │  consensus, skip unnecessary steps                         │   │
│  └────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

### E9.2 Model Tiering & Smart Routing

Not every agent action requires the most powerful model. The framework supports
**model routing** — automatically selecting the cheapest model that can handle
each specific task.

```yaml
model_routing:
  # ─── Default model per agent ───────────────────
  defaults:
    persona_agents: "claude-sonnet-4-20250514"     # rich reasoning for personas
    sub_agents: "claude-sonnet-4-20250514"          # default for sub-agents
    evaluators: "claude-haiku-4-5-20251001"         # cheap, fast evaluation

  # ─── Task-based routing ────────────────────────
  # Override model based on what the agent is doing
  routing_rules:
    - match:
        task_type: "classification"                # triage, routing, tagging
      model: "claude-haiku-4-5-20251001"
      reason: "Classification doesn't need deep reasoning"

    - match:
        task_type: "summarization"
      model: "claude-haiku-4-5-20251001"
      reason: "Summarization is well-handled by smaller models"

    - match:
        task_type: "code_generation"
        complexity: "high"                          # inferred from input length/context
      model: "claude-sonnet-4-20250514"
      reason: "Complex code needs strong reasoning"

    - match:
        task_type: "code_generation"
        complexity: "low"                           # simple functions, boilerplate
      model: "claude-haiku-4-5-20251001"
      reason: "Simple code doesn't justify premium model cost"

    - match:
        sub_agent_template: "translator"
      model: "claude-haiku-4-5-20251001"
      reason: "Translation is reliable on smaller models"

    - match:
        message_type: "SOCIAL"                     # small talk, acknowledgments
      model: "claude-haiku-4-5-20251001"
      reason: "Casual messages don't need premium reasoning"

  # ─── Fallback chain ────────────────────────────
  fallback:
    - model: "claude-sonnet-4-20250514"
    - model: "claude-haiku-4-5-20251001"            # if primary is rate-limited
    # Post-MVP: local models as ultimate fallback
```

### E9.3 Context Window Management

Each agent's context window is a finite, expensive resource. The framework
actively manages what goes into it.

> **Cross-reference**: E9.3 controls the *overall context budget* and priority
> scoring. E9.6 specifically controls *channel history injection* strategy
> and *notification filtering*. Both determine what enters an agent's context;
> E9.3 is the global arbiter, E9.6 handles the communication-specific logic.

```yaml
context_management:
  # ─── Strategy ──────────────────────────────────
  strategy: "priority_weighted"
  # Options:
  #   sliding_window   — keep last N messages, drop oldest
  #   priority_weighted — score each item, keep highest-priority within budget
  #   summarize_and_compact — periodically summarize history into shorter form

  # ─── Token budget per agent turn ───────────────
  budget:
    max_context_tokens: 80000              # leave room for output
    allocation:
      system_prompt: 5000                  # persona + role + constraints
      persona_state: 1000                  # mood, goals, relationship updates
      channel_history: 20000               # recent relevant messages
      task_context: 30000                  # current task input + prior step outputs
      tool_results: 15000                  # recent tool call results
      episodic_memory: 5000                # retrieved long-term memories
      buffer: 4000                         # safety margin

  # ─── Priority scoring ─────────────────────────
  # When content exceeds budget, lower-priority items are dropped first
  priority:
    current_task_input: 100                # never drop
    system_prompt: 100                     # never drop
    direct_messages_to_me: 90             # high priority
    recent_channel_messages: 70            # sliding window
    tool_results_current_task: 80
    tool_results_prior_tasks: 40           # summarize or drop
    episodic_memories: 50                  # relevant retrieved memories
    relationship_context: 30              # can be summarized aggressively
    old_channel_history: 10               # first to be dropped/summarized
```

#### Automatic Summarization

When context exceeds budget, the framework compresses older content rather than
simply truncating it:

```yaml
context_management:
  summarization:
    enabled: true
    model: "claude-haiku-4-5-20251001"       # use cheap model for summarization
    triggers:
      - when: "channel_history > 15000 tokens"
        action: "summarize oldest 50% into ~2000 token summary"
      - when: "tool_results > 10000 tokens"
        action: "summarize each result to key findings only"
      - when: "total_context > 75000 tokens"
        action: "aggressive: summarize everything except current task"

    # Summarization preserves structure
    summary_format: |
      ## Channel Summary (last {n} messages in #{channel})
      Key decisions: ...
      Open questions: ...
      Action items assigned to me: ...
      Relevant context for current task: ...
```

### E9.4 LLM Response Caching

Many agent interactions produce identical or near-identical LLM calls —
especially tool-use patterns, classification tasks, and sub-agent templates
with the same input.

```yaml
caching:
  # ─── Exact-match cache ────────────────────────
  exact:
    enabled: true
    backend: "in_memory"                    # in_memory | redis | sqlite
    max_entries: 10000
    ttl_seconds: 3600                       # cache for 1 hour
    # Cache key: hash(model + system_prompt + messages + tools + temperature)
    # Only cache when temperature == 0 (deterministic)
    cache_when:
      temperature: 0.0                      # only deterministic calls
      exclude_tools: ["shell_exec"]         # don't cache side-effect calls

  # ─── Semantic cache (post-MVP) ─────────────────
  semantic:
    enabled: false                          # requires embedding model
    similarity_threshold: 0.95             # cosine similarity to count as "same"
    backend: "vector_store"
    embedding_model: "text-embedding-3-small"
    # If a query is semantically identical to a cached query,
    # return the cached response without making an LLM call.
    # Useful for: repeated questions across agents, common sub-agent tasks
```

### E9.5 Prompt Compression

Reduce token count in prompts without losing semantic content. This is
especially valuable for personas with long backgrounds and channel history.

```yaml
prompt_compression:
  # ─── Static compression (at config time) ──────
  static:
    # Precompute compressed versions of static prompt components
    compress_persona: true
    # "Ember Owl has 15 years of experience in software engineering.
    #  She was formerly a tech lead at a Series B startup..."
    # becomes:
    # "Ember Owl: 15yr SW eng, ex-tech-lead Series B startup..."
    compression_model: "claude-haiku-4-5-20251001"
    target_ratio: 0.5                       # reduce to ~50% of original tokens

  # ─── Dynamic compression (at runtime) ─────────
  dynamic:
    # Compress tool results before injecting into context
    compress_tool_outputs: true
    tool_output_max_tokens: 2000           # truncate + summarize if over limit
    # Compress prior step outputs in multi-step workflows
    compress_prior_steps: true
    prior_step_max_tokens: 1000            # keep only key findings

  # ─── Structural optimization ───────────────────
  structural:
    # Remove redundant whitespace, markdown formatting from tool outputs
    strip_formatting: true
    # Replace repeated entity names with short aliases in context
    # "Ember Owl, VP of Engineering" → "EO" after first mention
    entity_aliasing: false                  # opt-in, can confuse some models
    # Remove system prompt components that aren't relevant to current task
    conditional_prompt_sections: true
    # e.g., don't include "how to delegate" instructions if task is simple
```

### E9.6 Communication Optimization

Agent-to-agent messages accumulate fast. A 5-agent team chatting for 100
turns generates massive channel history that every agent must process.

```yaml
communication_optimization:
  # ─── Message deduplication ─────────────────────
  dedup:
    # Agents sometimes send near-identical status updates
    enabled: true
    window: 10                              # check last 10 messages
    similarity_threshold: 0.9              # suppress if >90% similar
    action: "suppress_with_note"           # add "(similar to previous)" marker

  # ─── Channel history injection strategy ────────
  history_injection:
    strategy: "relevance_filtered"
    # Options:
    #   all            — inject full history (expensive)
    #   last_n         — inject last N messages only
    #   relevance_filtered — embed current task, retrieve relevant messages
    #   summarized     — inject rolling summary + last N messages (recommended)

    summarized:
      summary_model: "claude-haiku-4-5-20251001"
      summary_update_interval: 10          # re-summarize every 10 new messages
      keep_recent_messages: 5              # always include last 5 verbatim
      summary_max_tokens: 2000

  # ─── Selective notification ────────────────────
  # Not every agent needs to see every message in a channel
  notification_filter:
    rules:
      - channel: "eng-general"
        agent: "ember-owl"
        notify_on:
          - mentions_me: true
          - message_type: ["DECISION", "ESCALATION", "QUESTION"]
          - keywords: ["architecture", "deadline", "blocked"]
        suppress:
          - message_type: ["SOCIAL"]
          - sender_is: "bot"

  # ─── Batched message delivery ──────────────────
  batching:
    # Instead of interrupting agents for every message,
    # batch non-urgent messages and deliver together
    enabled: true
    batch_window_seconds: 5                # collect messages for 5 seconds
    urgent_bypass: true                    # ESCALATION and @mentions bypass batching
    max_batch_size: 20
```

### E9.7 Memory Compression

Long-running simulations generate enormous episodic memory. The framework
compresses memory progressively over time.

```yaml
memory_optimization:
  # ─── Tiered compression ────────────────────────
  # Recent memories are detailed; older memories are progressively compressed
  tiers:
    - age: "< 1 hour"
      compression: "none"
      detail: "full"                        # verbatim conversation chunks

    - age: "1 hour – 24 hours"
      compression: "summarize"
      detail: "key_points"                  # summarized to bullet points
      model: "claude-haiku-4-5-20251001"

    - age: "1 day – 7 days"
      compression: "distill"
      detail: "facts_only"                  # only extracted facts and decisions
      # "We discussed the API redesign for 45 minutes. Decided to use
      #  GraphQL. Mike will own the implementation. Due Friday."

    - age: "> 7 days"
      compression: "abstract"
      detail: "gist"                        # one-sentence summaries
      # "Decided on GraphQL for API. Mike implementing."

  # ─── Relationship memory compression ───────────
  relationship:
    # Don't store every interaction; track aggregate patterns
    store_raw_interactions: false            # only in dev
    track_patterns:
      - trust_score_over_time
      - collaboration_frequency
      - conflict_events                     # keep these detailed
      - positive_feedback_events
    max_events_per_relationship: 50        # keep last 50 significant events

  # ─── Deduplication ─────────────────────────────
  memory_dedup:
    # If an agent "learns" the same fact from multiple sources,
    # store it once with multiple source citations
    enabled: true
    similarity_threshold: 0.85
```

### E9.8 Wire & Mesh Compression

For distributed deployments, network traffic matters.

```yaml
mesh_optimization:
  # ─── gRPC compression ─────────────────────────
  grpc:
    compression: "gzip"                     # gzip | snappy | zstd | none
    # Protobuf is already compact binary; gzip adds ~60% reduction on top
    max_message_size_bytes: 4194304        # 4MB hard limit

  # ─── Message payload optimization ─────────────
  payload:
    # Don't send full agent state on every message
    state_sync: "delta_only"               # send only changed fields
    # Compress large tool outputs before sending across mesh
    compress_tool_outputs_over: 10000      # bytes; compress if larger
    compression: "zstd"

  # ─── Traffic reduction ─────────────────────────
  traffic:
    # Co-located agents (same node) use shared memory, not gRPC
    use_shared_memory_for_local: true
    # Aggregate multiple small messages into single gRPC call
    message_coalescing:
      enabled: true
      window_ms: 50                         # coalesce messages within 50ms
    # Channel history: don't replicate to nodes with no subscribers
    lazy_replication: true

  # ─── Bandwidth-aware routing ───────────────────
  bandwidth:
    # For nodes on slow connections, reduce message fidelity
    low_bandwidth_mode:
      trigger: "node_bandwidth < 1mbps"
      actions:
        - "summarize channel history before syncing"
        - "compress all payloads with zstd"
        - "reduce state sync frequency to every 30s"
        - "suppress SOCIAL message type replication"
```

### E9.9 Execution Optimization

Reduce the total number of LLM calls and wall-clock time.

```yaml
execution_optimization:
  # ─── Parallel execution ────────────────────────
  parallelism:
    # Independent tasks in a DAG run concurrently
    max_concurrent_agents: 10              # per workflow
    max_concurrent_sub_agents: 5           # per persona agent
    # Parallel tool calls within a single agent turn
    parallel_tool_calls: true

  # ─── Early termination ─────────────────────────
  early_exit:
    # In consensus protocols: stop if threshold met before max rounds
    consensus_early_exit: true
    # In evaluation chains: stop if first evaluator fails
    fail_fast_evaluations: true
    # In research sub-agents: stop if confidence > threshold
    confidence_threshold: 0.95

  # ─── Lazy evaluation ──────────────────────────
  lazy:
    # Don't spawn sub-agents until their output is actually needed
    lazy_sub_agent_spawn: true
    # Don't load channel history until agent explicitly reads it
    lazy_history_load: true
    # Don't compute persona state updates until agent's next turn
    lazy_state_update: true

  # ─── Skip optimization ────────────────────────
  skip:
    # If an agent's output from a previous identical workflow run
    # is cached and inputs haven't changed, skip the agent entirely
    deterministic_step_skip: true
    # Skip notification delivery to idle agents
    skip_idle_agent_notifications: true

  # ─── Speculative execution (post-MVP) ──────────
  speculative:
    enabled: false
    # Start likely-next-steps before current step completes
    # If prediction was wrong, discard results
    # Useful for: linear workflows where step B almost always follows step A
    confidence_required: 0.9
    max_speculative_branches: 2
```

### E9.10 Optimization Profiles

Predefined optimization configurations for common scenarios:

```yaml
optimization_profiles:
  # ─── Cost-optimized ────────────────────────────
  cost_optimized:
    description: "Minimize spend at the expense of some quality and speed"
    model_routing:
      persona_agents: "claude-haiku-4-5-20251001"
      sub_agents: "claude-haiku-4-5-20251001"
    context_management:
      max_context_tokens: 30000
      summarization: { enabled: true, aggressive: true }
    caching:
      exact: { enabled: true, ttl_seconds: 7200 }
    communication_optimization:
      history_injection: { strategy: "summarized", keep_recent: 3 }
      batching: { batch_window_seconds: 10 }
    prompt_compression:
      static: { target_ratio: 0.4 }

  # ─── Speed-optimized ──────────────────────────
  speed_optimized:
    description: "Minimize latency at the expense of some cost"
    model_routing:
      persona_agents: "claude-sonnet-4-20250514"
      sub_agents: "claude-haiku-4-5-20251001"       # fast sub-agents
    execution_optimization:
      parallelism: { max_concurrent_agents: 20 }
      parallel_tool_calls: true
      lazy_sub_agent_spawn: false                   # eager spawn
    communication_optimization:
      batching: { enabled: false }                  # no batching delay
      history_injection: { strategy: "last_n", n: 5 }
    caching:
      exact: { enabled: true }

  # ─── Quality-optimized ────────────────────────
  quality_optimized:
    description: "Maximize output quality regardless of cost"
    model_routing:
      persona_agents: "claude-sonnet-4-20250514"
      sub_agents: "claude-sonnet-4-20250514"
    context_management:
      max_context_tokens: 150000                    # requires 200k-context model
      # NOTE: max_context_tokens must not exceed the selected model's context
      # window minus output budget. The framework validates this at startup
      # and falls back to model's max if configured value is too high.
      summarization: { enabled: false }             # keep full history
    prompt_compression:
      static: { compress_persona: false }           # full persona detail
    execution_optimization:
      early_exit: { consensus_early_exit: false }   # always complete all rounds
    communication_optimization:
      history_injection: { strategy: "all" }

  # ─── Simulation-optimized ─────────────────────
  simulation_optimized:
    description: "Optimized for long-running multi-agent simulations"
    model_routing:
      persona_agents: "claude-sonnet-4-20250514"
      sub_agents: "claude-haiku-4-5-20251001"
    context_management:
      max_context_tokens: 60000
      summarization: { enabled: true }
    memory_optimization:
      tiers:
        - { max_age: "1h", compression: "none" }
        - { max_age: "24h", compression: "summarize" }
        - { max_age: "7d", compression: "distill" }
        - { max_age: "inf", compression: "abstract" }
    communication_optimization:
      history_injection: { strategy: "summarized" }
      notification_filter: { enabled: true }
      batching: { batch_window_seconds: 10 }
    mesh_optimization:
      state_sync: "delta_only"
      lazy_replication: true
```

### E9.11 Optimization Metrics

The framework tracks optimization effectiveness so you can see what's working:

```yaml
optimization_metrics:
  # Emitted as OTEL metrics under Persatrix.optimization.*
  metrics:
    Persatrix.optimization.cache.hit_rate:          # gauge, 0-1
      dimensions: [cache_type, agent_id]
    Persatrix.optimization.cache.tokens_saved:      # counter
      dimensions: [cache_type]
    Persatrix.optimization.cache.cost_saved_usd:    # counter
      dimensions: [cache_type]
    Persatrix.optimization.summarization.ratio:     # gauge, compression ratio
      dimensions: [content_type]                    # channel_history, tool_output, memory
    Persatrix.optimization.summarization.tokens_saved: # counter
      dimensions: [content_type]
    Persatrix.optimization.context.utilization:     # gauge, % of budget used
      dimensions: [agent_id]
    Persatrix.optimization.context.items_dropped:   # counter
      dimensions: [agent_id, priority_level]
    Persatrix.optimization.model_routing.downgrades: # counter
      dimensions: [from_model, to_model, reason]
    Persatrix.optimization.skip.steps_skipped:      # counter
      dimensions: [workflow_id, reason]
    Persatrix.optimization.dedup.messages_suppressed: # counter
      dimensions: [channel_id]
    Persatrix.optimization.mesh.bytes_saved:        # counter
      dimensions: [compression_type, node_id]
```

---

## E10. Observation & Experiment Controls

For social experiments and simulations, the framework needs **observer
capabilities** — ways to watch, measure, and control agent societies without
interfering.

### E10.1 Observer Mode

```yaml
observers:
  - id: "researcher"
    type: "passive"                      # sees everything, never interacts
    sees:
      - all_channels
      - all_direct_messages
      - agent_internal_state
      - decision_reasoning
    output:
      format: "transcript"
      destination: "file:///workspace/experiment_logs/"

  - id: "moderator"
    type: "active"                       # can inject messages, pause, modify
    capabilities:
      - inject_event                     # "breaking news: budget cut by 30%"
      - pause_simulation
      - resume_simulation
      - modify_agent_state               # change mood, goals, knowledge
      - add_agent                        # introduce a new participant mid-sim
      - remove_agent
```

### E10.2 Simulation Controls

```yaml
simulation:
  id: "policy-debate-2026"
  seed: 42                               # reproducible randomness
  time_model: "accelerated"              # real_time | accelerated | stepped
  time_acceleration: 10                  # 1 real second = 10 simulated seconds
  max_duration: "4h"
  checkpoints:
    enabled: true
    interval: "30m"
    path: "/workspace/checkpoints/"
  interventions:
    - at: "1h"
      action: "inject_event"
      target: "channel:general"
      message: "Breaking: the government just announced new regulations."
    - at: "2h"
      action: "add_agent"
      agent_config: "agents/surprise-guest.yaml"
  metrics:
    track:
      - "message_count_per_agent"
      - "sentiment_per_channel"
      - "consensus_formation_time"
      - "decision_quality_score"
      - "relationship_trust_changes"
    export_format: "csv"
    export_interval: "15m"
```

### E10.3 Observer Privacy & Consent

For research and social experiments, ethical observation requires informed
consent and data protection:

```yaml
observation:
  privacy:
    # ─── Agent awareness ────────────────────────
    inform_agents: true                    # agents are told they're being observed
    disclosure_text: |
      This simulation is being observed for research purposes. Your
      conversations, decisions, and states may be recorded and analyzed.
    inject_at: "system_prompt"             # prepend to every agent's system prompt

    # ─── Data protection ────────────────────────
    anonymize_export: false                # set true for external research sharing
    anonymization_rules:
      replace_names: true                  # "Ember Owl" → "Agent-A"
      hash_ids: true                       # deterministic pseudonymization
      strip_pii: true                      # remove emails, phone numbers, etc.

    # ─── Consent levels ─────────────────────────
    # Not all observation requires the same access
    consent_levels:
      basic:                               # message counts, timing, public channels
        sees: [public_channels, aggregate_metrics]
      standard:                            # message content in public channels
        sees: [public_channels, message_content, agent_actions]
      full:                                # everything including DMs and internal state
        sees: [all_channels, direct_messages, agent_internal_state, decision_reasoning]
        requires: "explicit_configuration"  # must be deliberately enabled
```

### E10.4 Embedding Infrastructure

Several features require embedding models: semantic cache (E9.4), relevance-
filtered channel history injection (E9.6), and episodic memory retrieval (E7).
The framework provides a shared embedding service:

```yaml
embeddings:
  # ─── Model selection ──────────────────────────
  model: "text-embedding-3-small"          # default embedding model
  provider: "openai"                       # or "anthropic", "local"
  dimensions: 1536

  # ─── Local model support (post-MVP) ──────────
  local:
    enabled: false
    model_path: "/models/e5-small-v2"
    device: "cpu"                          # or "cuda"

  # ─── Storage backend ─────────────────────────
  vector_store:
    backend: "sqlite_vss"                  # MVP: SQLite with vector extension
    # Post-MVP options: "chromadb", "pgvector", "qdrant"
    path: "/workspace/data/embeddings.db"
    index_type: "hnsw"

  # ─── Performance ─────────────────────────────
  batch_size: 100                          # embed up to 100 texts per API call
  cache_embeddings: true                   # don't re-embed identical text
  max_concurrent_requests: 5

  # ─── Usage ────────────────────────────────────
  # The embedding service is shared across:
  # - Semantic cache (E9.4): cache key similarity
  # - Relevance-filtered history (E9.6): find relevant past messages
  # - Episodic memory retrieval (E7): recall relevant memories
  # - Shared knowledge base search (E7): document retrieval
```

---

## E11. Use Case Blueprints

Predefined configurations that users can `persatrix init --blueprint <name>` to
scaffold a complete agent society.

### E11.1 Blueprint: Software Team

```yaml
blueprint:
  id: "software-team"
  description: "A small dev team building a product"
  agents:
    - { extends: "templates/product-manager", id: "pm" }
    - { extends: "templates/architect", id: "architect" }
    - { extends: "templates/backend-dev", id: "backend-dev" }
    - { extends: "templates/frontend-dev", id: "frontend-dev" }
    - { extends: "templates/qa-engineer", id: "qa" }
  organization:
    topology: "flat"
    lead: "pm"
  channels:
    - { id: "general", members: "all" }
    - { id: "code-review", members: ["architect", "backend-dev", "frontend-dev"] }
    - { id: "bugs", members: ["qa", "backend-dev", "frontend-dev"] }
  workflows:
    - "workflows/feature-development.yaml"
    - "workflows/bug-triage.yaml"
```

### E11.2 Blueprint: Startup

```yaml
blueprint:
  id: "startup-sim"
  description: "A 10-person startup from founding to Series A"
  agents:
    - { extends: "templates/ceo", id: "ceo" }
    - { extends: "templates/cto", id: "cto" }
    - { extends: "templates/vp-sales", id: "vp-sales" }
    - { extends: "templates/marketer", id: "marketer" }
    - { extends: "templates/developer", id: "dev-1" }
    - { extends: "templates/developer", id: "dev-2" }
    - { extends: "templates/sales-rep", id: "sdr-1" }
    - { extends: "templates/sales-rep", id: "sdr-2" }
    - { extends: "templates/designer", id: "designer" }
    - { extends: "templates/ops-manager", id: "ops" }
  organization:
    topology: "hierarchy"
    # ... (structure definition)
  bridges:
    - { type: "email", purpose: "outbound sales" }
    - { type: "slack", purpose: "team communication" }
```

### E11.3 Blueprint: Social Experiment

```yaml
blueprint:
  id: "social-experiment"
  description: "N individuals debating a policy topic"
  parameters:                            # user-configurable at init time
    num_participants: 12
    topic: "Universal basic income"
    diversity_axes:
      - political_leaning: ["progressive", "moderate", "conservative"]
      - age_group: ["gen-z", "millennial", "gen-x", "boomer"]
      - education: ["high-school", "bachelors", "graduate"]
  auto_generate_personas: true           # LLM generates diverse personas from axes
  channels:
    - { id: "town-hall", type: "meeting", protocol: "debate" }
    - { id: "small-groups", type: "group", max_size: 4, auto_assign: true }
  observers:
    - { id: "researcher", type: "passive", metrics: ["sentiment", "consensus"] }
  simulation:
    phases:
      - { name: "individual_reflection", protocol: "solo_writing", duration: "10m" }
      - { name: "small_group_discussion", protocol: "free_form", duration: "30m" }
      - { name: "town_hall_debate", protocol: "debate", duration: "45m" }
      - { name: "final_vote", protocol: "consensus", duration: "15m" }
```

---

## E12. Updated MVP Phasing

Given the expanded scope, the MVP should be delivered in three releases.

### Feature Dependency Graph

Critical path dependencies between features (A → B means A must ship before B):

```
v0.1 Foundation:
  gRPC + Protobuf → Agent Registry → Sequential Executor → Parallel Executor
  Permission System → Tool System → MCP Client
  OTEL Instrumentation → Cost Tracking
  Config Validation → All YAML-based features
  Health Checks → Error Handling / Circuit Breakers
  Testing Framework (mock LLM) → Integration Tests

v0.2 Society (depends on all v0.1):
  Channels → Communication Protocols → Meeting type
  Channels → Channel History Summarization (optimization)
  PersonaAgent Interface → Persona Model → Autonomy Levels
  PersonaAgent Interface → Sub-Agent Spawning → Sub-Agent Templates
  Channels + Persona → Observer Mode → Session Replay
  Channels → External Bridges → Bridge Security
  Persona + Channels + Org Topologies → Blueprints
  Context Window Management → all persona features
  Model Tiering → Budget Enforcement → Cost-optimized profile

v0.3 Distribution (depends on Channels + Registry):
  Node Runtime → Agent Addressing → Hub-and-Spoke
  mTLS → Node Admission → Trust Domains
  Agent Addressing → Agent Migration
  Channels (distributed) → Mesh Message Routing → Latency-Aware Routing
  Trust Domains → A2A Server Mode → A2A Client Mode
  Wire Compression → Bandwidth-Aware Routing
```

> **State persistence note**: v0.1 uses in-memory state for workflow execution
> and SQLite for audit logs. v0.2 adds SQLite for channel history, agent memory,
> and relationship graphs (see main spec §12.8). v0.3 must address distributed
> state: channel history replication across nodes, agent state migration, and
> checkpoint portability. The state backend is abstracted behind interfaces so
> it can be upgraded to PostgreSQL or a distributed store without changing
> agent code.

### MVP v0.1 — Core Engine (original scope, Weeks 1–8)
Everything in the current spec: orchestrator, workflows, tools, MCP, security,
resilience, testing, config validation, health checks, OTEL, cost tracking.
Also includes foundational observability:
- OTEL trace/span emission with GenAI semantic conventions for all LLM calls and tool invocations
- OTLP exporter (configurable backend: Jaeger, console, or any OTEL-compatible collector)
- Basic cost tracking: token usage per agent/workflow, estimated USD cost
- Structured JSON logging with trace correlation IDs

### MVP v0.2 — Agent Societies (Weeks 9–14)

| Feature                        | Details                                              |
|--------------------------------|------------------------------------------------------|
| Persona model                  | YAML persona schema, template inheritance, runtime state |
| Sub-agent spawning             | Persona agents spawn ephemeral sub-agents for atomic tasks |
| Sub-agent security             | Permission inheritance with iron rule (child ≤ parent) |
| Sub-agent templates            | Reusable templates: researcher, coder, reviewer, writer, etc. |
| Internal channels              | Group channels, DMs, broadcast, thread support       |
| Communication protocols        | Standup, brainstorm, debate, consensus (at least 3)  |
| Autonomy levels                | Passive, reactive, semi-autonomous (no full autonomous yet) |
| Org topologies                 | Hierarchy and flat (matrix post-MVP)                 |
| Observer mode                  | Passive observer with transcript export              |
| 1 external bridge              | Email (SMTP/IMAP) as the first external bridge       |
| Blueprints                     | Software team + social experiment as starter blueprints |
| Session replay                 | Step-through replay via CLI with agent filtering     |
| Persatrix OTEL attributes      | Custom `Persatrix.*` span attributes for personas, channels, sub-agents |
| Inline evaluators              | Schema validation + safety check gates               |
| Budget enforcement             | Per-agent and per-workflow spend limits with alerts   |
| Environment configs            | Dev/staging/prod profiles for content capture and budgets |
| Model tiering                  | Task-based model routing (Haiku for triage/summary, Sonnet for reasoning) |
| Context window management      | Priority-weighted retention, automatic summarization of old history |
| LLM response caching           | Exact-match cache for deterministic (temp=0) calls |
| Prompt compression             | Static persona compression, tool output truncation |
| Channel history summarization  | Rolling summaries + last N messages instead of full history |
| Optimization profiles          | Preset configs: cost-optimized, speed-optimized, quality-optimized, simulation |
| Optimization metrics           | Cache hit rate, tokens saved, context utilization via OTEL |

### Post-MVP v0.3 — Distributed Mesh + Interop (Weeks 15–22)

| Feature                        | Details                                              |
|--------------------------------|------------------------------------------------------|
| Node runtime                   | Agent runtime as deployable service with node registration |
| Agent addressing               | `agent@node` global addressing, orchestrator-based discovery |
| Hub-and-spoke deployment       | Central orchestrator routes between remote nodes     |
| Mutual TLS                     | Node ↔ orchestrator authentication                   |
| Node admission control         | Allowlist-based node registration with fingerprints  |
| Trust domains                  | Per-domain permission boundaries (full / restricted / isolated) |
| Latency-aware routing          | Affinity rules, co-locate frequently interacting agents |
| Offline handling               | Message queuing for disconnected nodes, agent status broadcasts |
| Agent migration                | `persatrix agent migrate` with state transfer             |
| Mesh CLI                       | `persatrix node`, `persatrix mesh status`, `persatrix mesh trace`   |
| Data residency rules           | Pin agents/channels to regions, block cross-region transit |
| A2A server mode                | Expose persona agents as A2A-compatible remote agents with Agent Cards |
| A2A client mode                | Discover and delegate to external A2A agents         |
| Platform exporters             | Direct integrations: AgentOps, Langfuse, LangSmith, Datadog |
| Offline evaluators             | LLM-judge quality scoring, persona consistency checks |
| Mesh metrics                   | Node health, cross-node latency, queue depth         |
| Wire compression               | gRPC gzip/zstd, delta-only state sync, message coalescing |
| Bandwidth-aware routing        | Low-bandwidth mode for constrained nodes             |
| Shared memory for local agents | Skip gRPC for co-located agents on same node         |

### Post-MVP (v0.4+)

- Full autonomous agent loop with goal planning
- Nested sub-agents (depth > 1) with budget cascading
- Remote sub-agent placement on specialized hardware (GPU nodes)
- Parallel sub-agent fan-out within a single persona agent
- Full mesh / P2P deployment with WireGuard overlay
- Federated mode: multi-organization agent collaboration
- End-to-end encrypted channels (orchestrator-blind routing)
- Additional bridges: Slack, Discord, Telegram, webhooks
- Memory system: episodic + relationship + shared knowledge
- Tiered memory compression (none → summarize → distill → abstract over time)
- Semantic caching (vector similarity-based LLM response cache)
- Speculative execution for linear workflows
- Simulation controls: checkpoints, interventions, time acceleration
- Matrix org topology
- Auto-persona generation from diversity parameters
- Web dashboard for observing live simulations
- Metrics export and analysis tooling

---

## E13. Updated Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        CLI / Web UI (Rust)                          │
│     init · run · observe · agents · channels · node · mesh · logs  │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
┌────────────────────────────────▼────────────────────────────────────┐
│                       Orchestrator Core (Go)                        │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────────────┐ │
│  │ Planner  │ │Scheduler │ │  State   │ │    Org Model Manager   │ │
│  └──────────┘ └──────────┘ └──────────┘ └────────────────────────┘ │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────────────┐ │
│  │ Registry │ │ Monitor  │ │ Router   │ │  Simulation Controller │ │
│  └──────────┘ └──────────┘ └──────────┘ └────────────────────────┘ │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                 Mesh Networking Layer                          │   │
│  │  ┌──────────┐ ┌────────────┐ ┌──────────┐ ┌───────────────┐  │   │
│  │  │  Node    │ │  Agent     │ │ Message  │ │   Trust &     │  │   │
│  │  │ Registry │ │ Discovery  │ │ Routing  │ │  Admission    │  │   │
│  │  └──────────┘ └────────────┘ └──────────┘ └───────────────┘  │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │              Communication Layer                              │   │
│  │  ┌─────────┐ ┌────────────┐ ┌──────────┐ ┌───────────────┐  │   │
│  │  │Channels │ │  Protocol  │ │  Message  │ │    Bridge     │  │   │
│  │  │ Manager │ │  Engine    │ │  Bus      │ │   Manager     │  │   │
│  │  └─────────┘ └────────────┘ └──────────┘ └──────┬────────┘  │   │
│  └──────────────────────────────────────────────────┼───────────┘   │
└───────┬──────────────┬──────────────────────────────┼───────────────┘
        │ gRPC/mTLS    │ gRPC/mTLS                    │
  ┌─────▼───────────┐  │                        ┌─────▼──────────┐
  │  Node A (AWS)   │  │                        │   Bridges      │
  │  ┌───────────┐  │  │                        │ ┌────────────┐ │
  │  │ Sarah     │  │  │                        │ │   Email     │ │
  │  │ + subs    │  │  │                        │ │   Slack     │ │
  │  ├───────────┤  │  │                        │ │   Discord   │ │
  │  │ Mike      │  │  │                        │ │   Telegram  │ │
  │  │ + subs    │  │  │                        │ │   Webhook   │ │
  │  └───────────┘  │  │                        │ └────────────┘ │
  └─────────────────┘  │                        └────────────────┘
                 ┌─────▼───────────┐
                 │ Node B (Tokyo)  │
                 │  ┌───────────┐  │
                 │  │ Yuki      │  │
                 │  │ + subs    │  │
                 │  ├───────────┤  │
                 │  │ Kai       │  │
                 │  │ + subs    │  │
                 │  └───────────┘  │
                 └─────────────────┘
```

---

## E14. Key New Design Decisions

| Decision | Choice | Reasoning |
|----------|--------|-----------|
| Persona in YAML | Declarative, not code | Non-developers can design agent personalities |
| Template inheritance | `extends` keyword | Avoids repetition across similar agents |
| Communication as first-class | Separate from task execution | Agents need conversations, not just I/O |
| Bridge pattern for external comms | Adapter per platform | Add new platforms without core changes |
| Outbound approval by default | All external messages need sign-off | Prevents AI from sending unvetted messages to real humans |
| Protocols as reusable configs | Named YAML blocks | Same debate protocol works for any topic or group |
| Observer separation | Passive by default | Research integrity; observers shouldn't affect outcomes |
| Blueprints | Scaffolding templates | Fast onboarding; users get a working society in one command |
| Sub-agents vs delegation | Two distinct mechanisms | Sub-agents for mechanical work; delegation for social/org interactions |
| Sub-agent permissions | Child ≤ parent (iron rule) | Prevents privilege escalation through spawning |
| Sub-agent lifecycle | Ephemeral, no persistence | Keeps the system simple; sub-agents are tools, not people |
| Budget cascading | Shared pool from parent | Prevents unbounded cost from recursive spawning |
| Agent addressing | `agent@node` with orchestrator registry | Location-transparent routing; agents don't need to know where peers live |
| Hub-and-spoke first | Orchestrator routes all traffic initially | Simpler than full mesh; add P2P later for performance |
| mTLS for mesh auth | Mutual TLS between all nodes | Industry standard, bidirectional trust, certificate rotation |
| Trust domains | Per-domain permission boundaries | Different nodes can have different security postures |
| Sub-agent co-location | Always on parent's node (MVP) | Avoids cross-node lifecycle complexity; remote placement post-MVP |
| Data residency as config | Declarative rules in YAML | Compliance requirements vary by org; keep them auditable |
| At-least-once delivery | Retry until ACK (default) | Safe default for agent communication; exactly-once is optional upgrade |
| Federated mode | Independent orchestrators peering | Enables multi-org collaboration without surrendering control |
| OTEL as native telemetry | OpenTelemetry GenAI semconv | Industry standard; any backend works; no vendor lock-in |
| Persatrix.* namespace | Custom OTEL attributes | Extends GenAI semconv for persona/channel/mesh concepts without breaking standard |
| A2A for external interop | Client + server support | Linux Foundation standard; enables cross-vendor agent collaboration |
| MCP + A2A + Channels | Three complementary layers | MCP for tools, A2A for external agents, channels for internal society |
| Content capture off by default | Privacy-first in production | LLM prompts/responses contain PII; opt-in per environment |
| Budget enforcement at runtime | Hard limits, not just tracking | Prevents runaway costs from autonomous agents and recursive sub-agents |
| Inline + offline evaluation | Gates during execution + quality scoring after | Catch failures before they propagate; improve quality over time |
| Platform exporters as optional | AgentOps/Langfuse/etc. via config flag | OTEL is always-on; vendor integrations are convenience, not dependency |
| Model tiering | Task-based routing, not one-size-fits-all | 3-10x cost reduction by using Haiku for simple tasks |
| Summarization over truncation | Compress old context, don't discard | Agents retain knowledge; truncation causes amnesia |
| Haiku for meta-tasks | Use cheap model for summarization/compression | Don't spend Sonnet tokens on compressing Sonnet output |
| Optimization profiles | Preset configs selectable per workflow | Users shouldn't need to tune 50 knobs to get started |
| Cache deterministic calls only | Only temp=0 calls are cacheable | Non-deterministic calls should not return stale results |
| Three-stage MVP | v0.1 engine → v0.2 society → v0.3 mesh+interop | Each stage is independently useful; observability ships from day one |
