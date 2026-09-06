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
  That memory is **session-scoped** (v0.3.5): a run reads only its own
  session's (room's) rows, so two concurrent conversations don't bleed
  into each other. A session is room continuity, not a clean slate — to
  re-run a scenario in a fresh world that inherits *nothing* (same room
  and user), bump the `epoch` run-isolation axis instead of nuking
  volumes. See the [sessions](docs/guides/sessions.md) and
  [epochs](docs/guides/epochs.md) guides.
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
**Rust 1.80+**, and **an LLM provider**. Persatrix is provider-agnostic
([RFC 0033](docs/rfcs/0033-model-alias-layer.md)) with **no default provider** —
you pick one explicitly. Run a **free** local / offline model with no key at all,
or use a cloud key you have (Anthropic or OpenAI). Each provider is selected the
same way: a one-command demo that points the model aliases at it (see the
[model providers guide](docs/guides/model-providers.md)).

```bash
# Clone + configure
git clone https://github.com/mkhomutov/Persatrix.git
cd Persatrix
cp .env.example .env
# Put the API key for the provider you'll use in .env (e.g. ANTHROPIC_API_KEY
# or OPENAI_API_KEY) — or skip keys entirely and use the free demos below.

# Build everything
make all && make build-agents

# Bring the stack up ON A PROVIDER — pick one (each mounts an alias config):
make demo-offline     # free: scripted mock, no key, no network  ← start here
#   make demo-ollama    # free: a real local model via Ollama, no cloud spend
#   make demo-anthropic # Claude  (needs ANTHROPIC_API_KEY; spends real money)
#   make demo-openai    # GPT-4o  (needs OPENAI_API_KEY; spends real money)
#   make demo-gemini    # Gemini  (needs GEMINI_API_KEY / GOOGLE_API_KEY; spends real money)
#   make demo-watsonx   # watsonx (needs WATSONX_API_KEY + a project_id; spends real money)

# Chat with the example "VP of Engineering" persona
./bin/persatrix chat ember-owl
```

> **No default provider.** A bare `docker compose up` ships with the model
> aliases **unconfigured**, so agents fail loud at startup until you pick a
> provider — run a `make demo-*` above, or configure the `quality` / `fast` /
> `summarizer` role aliases in [`config/optimization.yaml`](config/optimization.yaml).
> This is deliberate: provider choice is always explicit, never an accidental
> default that could spend money.

Once the stack is up:

- Orchestrator API: <http://localhost:8080>
- Traces (Jaeger): <http://localhost:16686>
- Metrics (Prometheus): <http://localhost:9091>

For longer walkthroughs (chat sessions, channels, workflows, custom
personas), see the [persona agents guide](docs/guides/persona-agents.md)
and the [channels guide](docs/guides/channels.md).

### Try it free — offline mode (no API key, no cost) 🆓

Want to see the society work before wiring up a paid API key? Run the whole
stack against the built-in **offline provider** — scripted, persona-accurate
replies with **zero LLM calls and zero spend**:

```bash
make demo-offline                 # routes every agent to the mock provider
./bin/persatrix chat ember-owl    # chat for free
```

Everything runs end to end — chat, multi-agent channels, memory, the
budget-lease path, and the OpenTelemetry traces — on canned replies. Edit the
replies in [`config/offline_responses.yaml`](config/offline_responses.yaml),
or point the `quality` alias at a real provider any time to switch
([model providers guide](docs/guides/model-providers.md)).

**"$0" means $0 of your real money** — no API key is used and no provider call
is ever issued. The *in-app* wallet still runs: every canned reply acquires and
settles an [RFC 0023 lease](docs/rfcs/0023-llm-call-leasing.md) against a
simulated per-agent budget, so you can watch the lease machinery work in the
OpenTelemetry traces. But the mock is a genuinely **$0** local model — each
lease settles at `$0`, so the budget **never accrues** and the wallet cap is
never reached in offline mode. To watch an agent **pause itself at the cap**
(the wallet doing its job), run on a **priced** provider instead — a cloud
alias like Anthropic, or `make demo-openai` — where real per-token cost
accrues against the `$5`/agent simulated budget. See [§ Cost Warning](#-cost-warning).

> Offline mode removes the **API-key** and **cost** barriers (the two scariest
> ones). You still need the Docker + Go + Rust toolchain to build the stack
> itself — a toolchain-free quickstart is on the roadmap.

### Try it in the browser — web console 🖥️

Prefer a UI over the CLI? The orchestrator ships an embedded **web console** —
open a URL, pick a persona, chat with it, and watch a channel, with zero CLI
knowledge (RFC 0048 Slice 1: Interactions, new in v0.3.6). The demo stack
enables it out of the box:

```bash
make demo-offline                 # (or `make run-ui` for the local, non-Docker path)
# open http://localhost:8080/ui
```

It is served same-origin from the Go binary (no separate web server), behind
`--enable-ui` (**default off**). Under the default `auth.mode: disabled` the
console makes the *unauthenticated* REST surface browser-discoverable, so it
binds `127.0.0.1` and **must not be exposed beyond localhost** without an
authenticating reverse proxy. Since v0.3.12, setting `auth.mode: enabled`
(accounts + password login + role gate, RFC 0039) makes the console and REST
surface safe beyond localhost **over HTTPS** — see the
[auth guide](docs/guides/auth.md) (including what stays open: the
agent-ingress carve-out). Full walkthrough + security note: the
[web console guide](docs/guides/web-console.md).

### Run a real local model — Ollama (no API key, no cloud cost) 🦙

Want **real** inference — not canned replies — without a cloud API key or
per-token spend? Run the whole society on a model served locally by
[Ollama](https://ollama.com):

```bash
make demo-ollama                  # pulls the model + routes every agent to it
./bin/persatrix chat ember-owl    # chat against your local model
```

`make demo-ollama` bundles an `ollama` container, pulls a model into a
persistent volume (default `llama3.2` — override with
`PERSATRIX_OLLAMA_MODEL=qwen2.5 make demo-ollama`), and routes every agent to
it. The first pull downloads a few GB and can take a few minutes; later runs
reuse the volume. CPU works out of the box; uncomment the `deploy` block in
[`docker-compose.ollama.yaml`](docker-compose.ollama.yaml) for NVIDIA GPU.

Under the hood, `ollama` is a **first-class provider**
([`agents/llm_ollama.py`](agents/llm_ollama.py)) — a thin subclass of the
OpenAI-compatible provider, because Ollama speaks that wire format verbatim at
`/v1` — selected the same config-driven way as any other provider
([model providers guide](docs/guides/model-providers.md)). To point a
**single** agent at Ollama instead of the whole society, set `provider: ollama`
on its [`config/agents.yaml`](config/agents.yaml) entry (its `model:` is then a
real Ollama tag like `llama3.2`, and `provider_config.base_url` defaults to
`http://localhost:11434/v1`).

> **Real model, $0 of real money** — inference runs on your own hardware, so
> there is no cloud bill. The *in-app* [RFC 0023 wallet](docs/rfcs/0023-llm-call-leasing.md)
> still leases each call, but a local model is $0, so (as in offline mode) the
> budget never accrues and the cap is never reached — watch the cap trip on a
> priced cloud provider instead. Already running Ollama yourself? Point the
> `quality` alias at `provider: ollama` (set `provider_config.base_url`, or
> `PERSATRIX_OLLAMA_BASE_URL`, if it's not on the default port) to route the
> society to it without the bundled container. (Point all three role aliases —
> `quality` / `fast` / `summarizer` — at it for the whole society, not just
> `quality`.)

### Run on OpenAI — or any other provider 🔀

Anthropic, OpenAI, Ollama, and the offline mock are **peers** — none is the
default. Switching a role between them is a one-line edit to its alias in
[`config/optimization.yaml`](config/optimization.yaml) — the society's three
role aliases (`quality` / `fast` / `summarizer`) move the same way — not a code
change, and each provider has a one-command demo that configures all three:

```bash
make demo-anthropic               # Claude  (needs ANTHROPIC_API_KEY)
make demo-openai                  # GPT-4o / gpt-4o-mini (needs OPENAI_API_KEY)
make demo-gemini                  # gemini-3.5-flash (needs GEMINI_API_KEY / GOOGLE_API_KEY)
make demo-watsonx                 # llama-3-3-70b / granite-3-8b (needs WATSONX_API_KEY + a project_id)
```

The cloud demos spend real money — set a cap first. See the
**[model providers & aliases guide](docs/guides/model-providers.md)** for how
aliases resolve to a `(provider, model, pricing)` record, the one-line swap, and
adding a brand-new provider.

---

## ⚠️ Cost Warning

> **New here? Start with [offline mode](#try-it-free--offline-mode-no-api-key-no-cost-) (`make demo-offline`)** — it runs the
> entire society for $0 with no API key, so you can explore risk-free before
> spending a cent.

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

> **Since v0.3.3 there is no continuous tick spend to leak** — the persona
> autonomy loop is event-driven
> ([RFC 0024](docs/rfcs/0024-event-driven-scheduling.md)), so "consumes LLM
> tokens continuously" applies only to personas you deliberately schedule.
> `tick_interval_seconds` still works as a configured timer. The guidance
> above still holds: provider-side spending limits remain your authoritative
> backstop.

Persatrix is BUSL-1.1 licensed with no warranty. Use at your own risk
— see [SECURITY.md § Responsible Use](SECURITY.md#responsible-use).

---

## Roadmap

| Version | What you can do | Status |
|---------|------------------|--------|
| **v0.2.x** | Run persistent personas with memory, chat with them from a terminal, observe everything end-to-end with traces and metrics | ✅ Released |
| **v0.3.0** | Give agents a shared channel and watch them talk, negotiate, and form opinions over time | ✅ Released |
| **v0.3.1** | Chat with a persona that remembers stated facts about you across interactions and follows the conversation it's currently having | ✅ Released |
| **v0.3.2** | Every LLM call passes through a wallet lease before it's issued — cost becomes a structural gate, not a post-hoc accountant — and the memory facade is frozen as the single path to agent memory ahead of the v0.4.0 Postgres split | ✅ Released |
| **v0.3.3** | A persona with no scheduled work and no inbound traffic costs nothing — its event loop parks until something wakes it (an inbound message, a scheduled timer, or a salience-triggered memory write) instead of polling on a fixed tick | ✅ Released |
| **v0.3.4** | Run the same agents on any provider — Anthropic, OpenAI, a free local model (Ollama), or a $0 offline mock — by naming a logical model alias (`quality` / `fast` / `summarizer`); a vendor swap is a one-line config edit | ✅ Released |
| **v0.3.5** | Persona-memory recall is session-scoped — a run reads only its own session's (room's) rows (plus the always-visible `legacy` rows), so concurrent conversations don't bleed; to re-run a scenario in a clean world (same room + user, inheriting nothing) bump the `epoch` run-isolation axis instead of nuking volumes with `make reset` | ✅ Released |
| **v0.3.6** | Open a URL and talk to a persona — chat and watch a channel from the browser with zero CLI knowledge, via an embedded same-origin web console behind `--enable-ui` (off by default; RFC 0048 Slice 1: Interactions); group-channel personas now take turns instead of replying over each other (floor control, on by default) | ✅ Released |
| **v0.3.7** | Watch several personas hold a group conversation that reads like colleagues, not bots — they see and build on the running transcript (RFC 0034 Phase 2 group working memory), the right people speak, and a question aimed at one persona isn't answered by everyone (RFC 0030 relevance gate Tier A + the `respond` disposition vocabulary + a peer-voice prompt) | ✅ Released |
| **v0.3.8** | Give a group of personas a problem and watch the brainstorm **converge, terminate, and produce a readable synthesized outcome** — no pile-on (RFC 0030 relevance Tier B salience bid + the `chair` disposition + natural-language addressing), bounded cost and finite turns (RFC 0030 Layers 1/2/4 cost ceiling + reply budget + end-of-interaction vote), a real result (the RFC 0020 interaction-summary surface at close), and per-channel governance knobs you can edit live from the CLI or web console (RFC 0050) | ✅ Released |
| **v0.3.9** | A persona can recall the **verbatim** text of past conversations — and you can search that record — scoped to the channels it was actually present for: a removed-and-re-added persona recalls **both** stints and neither the pre-join period nor the removal gap (RFC 0036 over the RFC 0035 membership ledger; FTS5 enforced server-side, `epoch`-isolated, audited, opt-in behind `channels:recall`) | ✅ Released |
| **v0.3.10** | Run a group brainstorm that reads as **considered, not reflexive** — before posting, each persona privately weighs whether the turn is worth a reply and **stays silent when it would only agree, restate, or pile on**, and one opt-in rung up composes a plan-shaped post with optional self-critique. The deliberation is **private by design** — never a message, never persisted — so what you watch is the *result*: fewer, sharper posts (RFC 0051 Phases 1–3 + 5) | ✅ Released |
| **v0.3.11** | Give a roster of personas a topic and **walk away**: arm a channel (opt-in, with a mandatory cost cap — uncapped autonomy is un-creatable), convene it once or on a schedule, and they **discuss, converge, terminate deterministically, and leave a readable synthesis — with no human in the loop** (RFC 0052, all four phases). The flagship demo runs **four cloud vendors in one channel** (RFC 0053); `make demo-autonomous` shows the arc offline at $0 | ✅ Released |
| **v0.3.12** | Tell a persona something in one room and it **knows it in every other room it belongs to** — a project fact taught in a DM is known in the standup, raw experience from other rooms is available (room-first-ranked) — **without leaking what it learned in a confidential room**: rooms carry a classification, memories carry a protection level, and a deterministic egress gate filters what may inform each reply (RFC 0037 + RFC 0049 Phases 0–1). Plus **verified accounts**: opt-in `auth.mode: enabled` puts the console and REST surface behind password login with revocable sessions and roles — safe beyond localhost over HTTPS (RFC 0039 Phases 1–2) | ✅ Released |
| **v0.3.13** | The v0.3.12 promise holds **everywhere it was made**: a persona's own memory-**tool** recalls now obey `--epoch`/`--session` isolation too ([ISSUE-0118](docs/issues/ISSUE-0118-tool-recall-bypasses-epoch-session-scopes.md)); it knows **who** it is talking to in a room it was never told, not just *what* ([ISSUE-0121](docs/issues/ISSUE-0121-crossroom-person-identity-legs-never-run-live.md)); and one channel's discussion length is tunable without retuning the fleet ([ISSUE-0114](docs/issues/ISSUE-0114-per-channel-cascade-depth-override.md)) | ✅ Released |
| **v0.3.14** | **Two authenticated people talk to the same persona — beyond localhost — and neither one's memory bleeds into the other's.** Under `auth.mode: enabled` the orchestrator emits a per-request principal derived from the verified account, so persona memory is partitioned by *who is speaking* as well as by room and run — two people in one channel get per-speaker memory while still sharing the conversation ([ISSUE-0082](docs/issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md) over the [ISSUE-0081](docs/issues/ISSUE-0081-session-id-process-global-not-task-local.md) rail). `auth.mode: disabled` is byte-identical | ✅ [Released](https://github.com/mkhomutov/Persatrix/releases/tag/v0.3.14) |
| **v0.3.15** | **Memory a persona derives from a shared room names the person it came from** — and keeps that name across the orchestrator hop. A group room's close-time summary and facts land in the speaking person's tenant rather than whoever closed it, an agent cascade keeps the originating tenant, a restart replay re-derives under the tenant that spoke, and derived rows carry the **speaker**, so hearsay is distinguishable from testimony ([ISSUE-0082](docs/issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md) R-1/R-2, [ISSUE-0130](docs/issues/ISSUE-0130-catchup-replay-rederives-memory-under-default-principal.md), [ISSUE-0131](docs/issues/ISSUE-0131-derived-memory-has-no-speaker-attribution.md)) | 🔄 Release prep |
| **v0.4.0** | Define a team, lab, or company with roles and hierarchy — and let it run | 📋 Planned |
| **v0.5.0** | Bridge your agent society into Slack, Discord, or email | 📋 Planned |
| **v0.6.0** | Run agent societies across multiple nodes and networks | 📋 Planned |

For PR-level progress and per-RFC status, see [ROADMAP.md](ROADMAP.md).
For per-release upgrade notes and operator-visible changes, see
[CHANGELOG.md](CHANGELOG.md). For known limitations and deferred scope, see the
current release's
[§ Known Gaps](docs/v0.3.15-release-checklist.md#6-known-gaps-to-document-in-release-notes)
— each cycle's checklist carries its own, so that section tracks the release you
are running rather than a fixed list.

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
- [Model providers & aliases guide](docs/guides/model-providers.md) — run on
  Anthropic / OpenAI / Ollama / offline, the one-line provider swap
- [Channels guide](docs/guides/channels.md) — shared channels, DMs,
  response policies
- [Accounts & auth guide](docs/guides/auth.md) — `auth.mode: enabled`,
  first-operator bootstrap, roles, the console login (v0.3.12)
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
