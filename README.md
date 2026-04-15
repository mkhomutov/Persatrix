# Persatrix

A general-purpose **agent society engine** — a runtime for creating, connecting, and observing groups of AI agents that behave as individuals within organizational or social structures.

## Quick Start

### Prerequisites

- Go 1.24+
- Python 3.11+
- Rust 1.80+ (for CLI)
- Protobuf compiler (`protoc`)
- Docker & Docker Compose (optional, for local stack)

### Setup

```bash
# Clone
git clone https://github.com/mkhomutov/Persatrix.git
cd Persatrix

# Configure
cp .env.example .env
# Edit .env with your ANTHROPIC_API_KEY

# Build everything
make all

# Install Python agent dependencies
make build-agents

# Validate config
make validate
```

### Run with Docker Compose

```bash
docker compose up -d

# View Jaeger UI (traces): http://localhost:16686
# Orchestrator API:        http://localhost:8080
```

### Run a Workflow

```bash
# Via CLI
orch run workflows/feature-builder.yaml --input "Build a REST API for user management"

# Via API
curl -X POST http://localhost:8080/api/v1/workflows/run \
  -H "Content-Type: application/json" \
  -d '{"workflow": "feature-builder", "input": "Build a REST API for user management"}'
```

### Run Tests

```bash
make test               # all tests
make test-go            # Go unit tests
make test-python        # Python agent tests
make test-integration   # end-to-end tests
```

## Architecture

```
CLI (Rust) → Orchestrator (Go) → Agents (Python)
                  ↓                    ↓
            gRPC / REST           LLM APIs
            OTEL Traces           Tools / MCP
```

**Go** handles orchestration: workflow planning, scheduling, state management, security, and telemetry.

**Python** handles agent logic: LLM interaction, tool execution, persona behavior, and sub-agent spawning.

**Rust** handles the CLI: fast, single-binary distribution.

**gRPC** connects them: type-safe, cross-language, bidirectional streaming.

## Project Structure

```
Persatrix/
├── cmd/orchestrator/     Go server entry point
├── internal/             Go packages (planner, scheduler, registry, security, ...)
├── proto/                Protobuf definitions
├── agents/               Python agent runtime
│   ├── base.py           BaseAgent interface
│   ├── persona.py        PersonaAgent (v0.2+)
│   └── tools/            Tool system (@tool decorator, MCP bridge)
├── cli/                  Rust CLI
├── config/               YAML configuration
├── workflows/            Workflow definitions
├── schemas/              JSON Schema for config validation
├── tests/                Test suites
└── docker-compose.yaml   Local development stack
```

## Roadmap

| Version | Scope | Status |
|---------|-------|--------|
| v0.1    | Core engine: workflows, tools, MCP, security, OTEL, testing | ✅ Complete |
| v0.2    | Agent societies: personas, channels, protocols, bridges, sub-agents | 📋 Planned |
| v0.3    | Distributed mesh: multi-node, A2A protocol, platform integrations | 📋 Planned |
| v0.4+   | Autonomous agents, memory, simulation controls, web dashboard | 📋 Future |

See [ROADMAP.md](ROADMAP.md) for detailed progress tracking, RFC status, and component completion.

## Documentation

- [Roadmap & Progress](ROADMAP.md)
- [MVP Specification](docs/ai-agents-orchestration-spec.md)
- [Extension Specification](docs/persatrix-extension-spec.md)
- [Audit Report](docs/persatrix-spec-audit.md)

## License

Persatrix is distributed under the Business Source License 1.1 (`BUSL-1.1`).
Production use is not granted under the default terms in this repository.
Each version transitions to Apache License, Version 2.0 four years after its first public release.
See [LICENSE](LICENSE) for the full terms.
