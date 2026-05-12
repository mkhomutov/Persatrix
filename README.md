# Persatrix

[![CI](https://github.com/mkhomutov/Persatrix/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mkhomutov/Persatrix/actions/workflows/ci.yml)
[![License: BUSL-1.1](https://img.shields.io/badge/license-BUSL--1.1-blue.svg)](LICENSE)
[![Go 1.24+](https://img.shields.io/badge/Go-1.24%2B-00ADD8?logo=go&logoColor=white)](https://go.dev/)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Rust 1.80+](https://img.shields.io/badge/Rust-1.80%2B-DEA584?logo=rust&logoColor=white)](https://www.rust-lang.org/)

**Build AI agents that have a personality, remember you, talk to each
other, and act on their own.**

Most AI tools answer one question at a time and forget you the moment
you close the tab. Persatrix is different: you describe an agent's
personality, role, and goals once, then it sticks around — remembering
past conversations, building trust with people it talks to often, and
working alongside other agents in shared channels.

> **Pre-1.0 — experimental.** Persatrix runs on commercial LLM APIs and
> can spend real money. Read [§ Cost Warning](#-cost-warning) below
> before running anything.

---

## What you can build

- **A persona you talk to.** Define a "VP of Engineering" character in
  one YAML file — name, background, communication style, goals — and
  chat with them from your terminal. They remember your name, what you
  asked yesterday, and how much they trust you, all across restarts.
- **A team of agents that talk to each other.** Drop two or more agents
  into a shared channel and watch them coordinate, debate, and build
  shared context — like Slack, but the participants are AI personas
  with their own perspectives.
- **A workflow runner that knows who's who.** Submit YAML workflows
  that get planned, scheduled, and executed across multiple agents
  with budgets, retries, and full tracing — so you can see exactly
  what each agent did and what it cost.

---

## What it looks like

Define a persona in [`config/agents.yaml`](config/agents.yaml):

```yaml
- id: ember-owl
  type: persona
  name: Ember Owl
  role: Engineering leadership
  persona:
    title: VP of Engineering
    background: |
      15 years in software engineering. Pragmatic, direct,
      hates meetings longer than 30 minutes.
    behavior:
      directness: direct
      detail_focus: big-picture
```

Start the stack and chat with them from a terminal:

```bash
$ persatrix chat ember-owl
Connected to ember-owl. Type exit to quit.
You: How would you triage a flaky integration test?
ember-owl: First question: is it actually flaky, or is the underlying
system flaky and the test just surfaced it? Run it 50 times in a loop
on the latest main. If it fails non-deterministically, you have a real
race or ordering bug — that's a P1, not a test problem...
```

Drop them into a shared channel with another agent:

```bash
$ persatrix channel send planning "What's blocking the Q3 plan?" \
    --as alice --mention ember-owl
$ persatrix channel watch planning
ember-owl: The dependency on the auth migration is the long pole...
iron-fox: Agreed — and the staging env still doesn't have...
```

That's the whole idea: agents as persistent characters you (and other
agents) interact with over time.

---

## Quick start

You'll need: **Docker Desktop**, **Go 1.24+**, **Python 3.11+**,
**Rust 1.80+**, and an **`ANTHROPIC_API_KEY`** from
<https://console.anthropic.com/>.

```bash
# Clone + configure
git clone https://github.com/mkhomutov/Persatrix.git
cd Persatrix
cp .env.example .env
# Edit .env and paste your ANTHROPIC_API_KEY

# Build everything
make all && make build-agents

# Bring up the stack (orchestrator + agents + observability)
docker compose up -d

# Chat with the example "VP of Engineering" persona
./bin/persatrix chat ember-owl
```

Once the stack is up:

- Orchestrator API: <http://localhost:8080>
- Traces (Jaeger): <http://localhost:16686>
- Metrics (Prometheus): <http://localhost:9091>

For longer walkthroughs (chat sessions, channels, workflows, custom
personas), see the [persona agents guide](docs/guides/persona-agents.md)
and the [channels guide](docs/guides/channels.md).

---

## ⚠️ Cost Warning

Persatrix runs persona agents on **autonomous tick loops** that consume
LLM tokens continuously while the agent process is alive. **Bugs in
this software can cost you real money** — during early testing the
author lost ~$35 in API costs to a single faulty idle check.

Before running anything:

1. **Set hard spending limits at your LLM provider's billing page.**
   Persatrix's own budget controls are best-effort, not authoritative.
2. **Configure billing alerts** so you're notified if spending exceeds
   expectations.
3. **Stop agents explicitly when you're done** — kill the
   `make run-agent` process or the `agent-*` Docker Compose service.
   Don't rely on idle timeouts alone.
4. **Review [`config/agents.yaml`](config/agents.yaml) and
   [`config/optimization.yaml`](config/optimization.yaml)** and set
   conservative limits (`max_llm_calls`, `tick_interval_seconds`,
   `max_daily_usd`).

Persatrix is BUSL-1.1 licensed with no warranty. Use at your own risk
— see [SECURITY.md § Responsible Use](SECURITY.md#responsible-use).

---

## Roadmap

| Version | What you can do | Status |
|---------|------------------|--------|
| **v0.2.x** | Run persistent personas with memory, chat with them from a terminal, observe everything end-to-end with traces and metrics | ✅ Released |
| **v0.3.0** | Give agents a shared channel and watch them talk, negotiate, and form opinions over time | ✅ Released |
| **v0.4.0** | Define a team, lab, or company with roles and hierarchy — and let it run | 📋 Planned |
| **v0.5.0** | Bridge your agent society into Slack, Discord, or email | 📋 Planned |
| **v0.6.0** | Run agent societies across multiple nodes and networks | 📋 Planned |

For PR-level progress and per-RFC status, see [ROADMAP.md](ROADMAP.md).
For per-release upgrade notes and operator-visible changes, see
[CHANGELOG.md](CHANGELOG.md). For known limitations and deferred scope in
the current pre-release (channels are internal-only and unauthenticated,
MT-MEMORY-005 dementia gap, RFC 0009 Phases 3–4 deferred to v0.4.0), see
the [v0.3.0 release checklist § Known Gaps](docs/v0.3.0-release-checklist.md#6-known-gaps-to-document-in-release-notes).

---

## How it works (one-paragraph version)

A **Go orchestrator** plans and schedules work, tracks state, and
enforces cost budgets. **Python agents** run as long-lived gRPC
services — each one owns its own LLM calls, tools, memory tiers
(episodic + relationship + working), and tick loop. A **Rust CLI**
talks to the orchestrator over REST. Everything is wired into
OpenTelemetry, so a single trace shows you a workflow planning a
step, dispatching it to an agent, the agent calling an LLM, and the
LLM's reply landing back. Diagrams in
[`docs/diagrams/`](docs/diagrams/) walk through this in detail.

---

## Documentation

- [Persona agents guide](docs/guides/persona-agents.md) — declare a
  persona, configure memory, set budgets
- [Channels guide](docs/guides/channels.md) — shared channels, DMs,
  response policies
- [v0.3.0 demo walkthrough](docs/guides/v0.3.0-demo.md) — three pre-defined
  personas + one channel, plain-English step-by-step
- [Architecture diagrams](docs/diagrams/README.md) — system overview,
  components, persona runtime, memory tiers
- [Observability](docs/observability.md) — log schema, span inventory,
  `persatrix logs` CLI
- [RFCs](docs/rfcs/README.md) — engineering design docs
- [Manual tests](docs/manual-tests/README.md) — what's verified
  end-to-end on each release
- [Roadmap](ROADMAP.md) · [Changelog](CHANGELOG.md) ·
  [Branching](docs/BRANCHING.md) · [Development workflow](docs/development-workflow.md)

---

## License

Persatrix is distributed under the [Business Source License 1.1](LICENSE).
Production use is not granted under the default terms in this
repository; each version transitions to Apache 2.0 four years after
its first public release.

Third-party dependencies and their licenses:
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
