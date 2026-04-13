.PHONY: all build build-orchestrator build-cli build-agents proto proto-go proto-python clean test lint run validate help

# ─── Config ─────────────────────────────────────────────
GO_MODULE     := github.com/orchestr8/orchestr8
GO_BIN        := bin
PROTO_DIR     := proto
PROTO_GO_OUT  := internal/generated
PROTO_PY_OUT  := agents/generated
PYTHON        := python3
PIP           := pip3
CARGO         := cargo

# ─── Default ────────────────────────────────────────────
all: proto build

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ─── Protobuf ───────────────────────────────────────────
proto: proto-go proto-python ## Generate Go + Python code from protobuf definitions

proto-go: ## Generate Go gRPC stubs from protobuf definitions
	@echo "→ Generating Go protobuf stubs..."
	@mkdir -p $(PROTO_GO_OUT)
	protoc --go_out=. --go-grpc_out=. \
		--go_opt=module=$(GO_MODULE) --go-grpc_opt=module=$(GO_MODULE) \
		-I $(PROTO_DIR) $(PROTO_DIR)/*.proto
	@echo "✓ Go protobuf stubs generated"

proto-python: ## Generate Python gRPC stubs from protobuf definitions
	@echo "→ Generating Python protobuf stubs..."
	@mkdir -p $(PROTO_PY_OUT)
	$(PYTHON) -m grpc_tools.protoc --python_out=$(PROTO_PY_OUT) --grpc_python_out=$(PROTO_PY_OUT) \
		-I $(PROTO_DIR) $(PROTO_DIR)/*.proto
	@echo "✓ Python protobuf stubs generated"

# ─── Build ──────────────────────────────────────────────
build: build-orchestrator build-cli ## Build all components

build-orchestrator: ## Build Go orchestrator binary
	@echo "→ Building orchestrator..."
	@mkdir -p $(GO_BIN)
	go build -o $(GO_BIN)/orchestr8-server ./cmd/orchestrator
	@echo "✓ Orchestrator built → $(GO_BIN)/orchestr8-server"

build-cli: ## Build Rust CLI binary
	@echo "→ Building CLI..."
	cd cli && $(CARGO) build --release
	@cp cli/target/release/orch $(GO_BIN)/orch 2>/dev/null || true
	@echo "✓ CLI built → $(GO_BIN)/orch"

build-agents: ## Install Python agent dependencies
	@echo "→ Installing Python agent dependencies..."
	cd agents && $(PIP) install -e ".[dev]"
	@echo "✓ Agent dependencies installed"

# ─── Run ────────────────────────────────────────────────
run: build ## Run the orchestrator
	$(GO_BIN)/orchestr8-server --config config/

run-agent: ## Run a Python agent process (AGENT=coder)
	cd agents && $(PYTHON) -m orchestr8_agents.server --agent $(AGENT)

# ─── Test ───────────────────────────────────────────────
test: test-go test-python test-integration ## Run all tests

test-go: ## Run Go unit tests
	go test ./internal/... -v -race -cover

test-python: ## Run Python agent tests
	$(PYTHON) -m pytest tests/unit/python/ -v --tb=short

test-integration: ## Run integration tests
	$(PYTHON) -m pytest tests/integration/ -v --tb=short -c agents/pyproject.toml

test-persona: ## Run persona consistency tests (AGENT=sarah-chen)
	cd agents && $(PYTHON) -m pytest tests/ -v -k "persona" --agent $(AGENT)

# ─── Lint ───────────────────────────────────────────────
lint: lint-go lint-python lint-rust ## Lint all code

lint-go:
	golangci-lint run ./...

lint-python:
	cd agents && $(PYTHON) -m ruff check . && $(PYTHON) -m mypy .

lint-rust:
	cd cli && $(CARGO) clippy -- -D warnings

# ─── Validate ───────────────────────────────────────────
validate: ## Validate all YAML configs against JSON schemas
	@echo "→ Validating configuration..."
	$(PYTHON) agents/validate.py config/
	@echo "✓ All configs valid"

# ─── Docker ─────────────────────────────────────────────
docker-build: ## Build Docker images
	docker compose build

docker-up: ## Start all services
	docker compose up -d

docker-down: ## Stop all services
	docker compose down

docker-logs: ## Tail logs
	docker compose logs -f

# ─── Clean ──────────────────────────────────────────────
clean: ## Remove build artifacts
	rm -rf $(GO_BIN) $(PROTO_GO_OUT) $(PROTO_PY_OUT)
	cd cli && $(CARGO) clean
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
