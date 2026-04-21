.PHONY: all build build-orchestrator build-cli build-agents proto proto-go proto-python clean test lint run validate help generate-persona-nickname check-licenses check-licenses-go check-licenses-python check-licenses-rust notices notices-check bump-version

# ─── Config ─────────────────────────────────────────────
GO_MODULE     := github.com/mkhomutov/persatrix
GO_BIN        := bin
PROTO_DIR     := proto
PROTO_GO_OUT  := internal/generated
PROTO_PY_OUT  := agents/generated
PYTHON        := python3
PIP           := pip3
CARGO         := cargo
# On Windows, executables require the .exe extension; EXE is empty on Unix.
EXE           := $(if $(filter Windows_NT,$(OS)),.exe,)

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
	go build -o $(GO_BIN)/persatrix-server$(EXE) ./cmd/orchestrator
	@echo "✓ Orchestrator built → $(GO_BIN)/persatrix-server$(EXE)"

build-cli: ## Build Rust CLI binary
	@echo "→ Building CLI..."
	cd cli && $(CARGO) build --release
	@cp cli/target/release/persatrix$(EXE) $(GO_BIN)/persatrix$(EXE) 2>/dev/null || true
	@echo "✓ CLI built → $(GO_BIN)/persatrix$(EXE)"

build-agents: ## Install Python agent dependencies
	@echo "→ Installing Python agent dependencies..."
	cd agents && $(PIP) install -e ".[dev]"
	@echo "✓ Agent dependencies installed"

# ─── Run ────────────────────────────────────────────────
run: build ## Run the orchestrator
	$(GO_BIN)/persatrix-server$(EXE) --config config/

run-agent: ## Run a Python agent process (AGENT=coder PORT=50051)
	PYTHONPATH="agents/generated" $(PYTHON) -m persatrix_agents.server --agent $(AGENT) --port $(or $(PORT),50051)

generate-persona-nickname: ## Generate nickname-style persona id/name pairs (COUNT=1 SEED=)
	$(PYTHON) scripts/persona_nickname_generator.py --count $(or $(COUNT),1) $(if $(SEED),--seed $(SEED),)

# ─── Test ───────────────────────────────────────────────
test: test-go test-python test-integration ## Run all tests

test-go: ## Run Go unit tests
	go test ./internal/... -v -race -cover

test-python: ## Run Python agent tests
	$(PYTHON) -m pytest tests/unit/python/ -v --tb=short

test-integration: ## Run integration tests
	PYTHONPATH="agents/generated" $(PYTHON) -m pytest tests/integration/ -v --tb=short -c agents/pyproject.toml

test-persona: ## Run persona consistency tests (AGENT=ember-owl)
	cd agents && $(PYTHON) -m pytest tests/ -v -k "persona" --agent $(AGENT)

# ─── Lint ───────────────────────────────────────────────
lint: lint-go lint-python lint-rust ## Lint all code

lint-go:
	golangci-lint run ./...

lint-python:
	cd agents && $(PYTHON) -m ruff check . && $(PYTHON) -m mypy .

lint-rust:
	cd cli && $(CARGO) clippy -- -D warnings

# ─── License checks ─────────────────────────────────────
# Canonical allow-list: scripts/checks/allowed_licenses.txt
# Rust mirror: deny.toml [licenses].allow — keep in sync.
check-licenses: check-licenses-go check-licenses-python check-licenses-rust ## Run third-party license checks for Go, Python, and Rust

check-licenses-go: ## Check Go module licenses against the allow-list
	@echo "→ Checking Go dependency licenses..."
	@command -v go-licenses >/dev/null 2>&1 || go install github.com/google/go-licenses@latest
	@ALLOWED=$$(grep -v '^\s*#' scripts/checks/allowed_licenses.txt | grep -v '^\s*$$' | paste -sd, -); \
		go-licenses check ./cmd/... ./internal/... \
			--allowed_licenses="$$ALLOWED" \
			--ignore=$(GO_MODULE)
	@echo "✓ Go licenses OK"

check-licenses-python: ## Check Python dependency licenses against the allow-list
	@echo "→ Checking Python dependency licenses..."
	@$(PYTHON) scripts/checks/python_licenses.py --exception Persatrix-agents

check-licenses-rust: ## Check Rust crate licenses via cargo-deny
	@echo "→ Checking Rust dependency licenses..."
	@command -v cargo-deny >/dev/null 2>&1 || $(CARGO) install cargo-deny --locked --version 0.19.0
	cd cli && $(CARGO) deny check licenses
	@echo "✓ Rust licenses OK"

# ─── Third-party notices ────────────────────────────────
notices: ## Regenerate THIRD_PARTY_NOTICES.md from Go, Python, and Rust dependency graphs
	@echo "→ Regenerating THIRD_PARTY_NOTICES.md..."
	@command -v go-licenses >/dev/null 2>&1 || go install github.com/google/go-licenses@latest
	@command -v cargo-license >/dev/null 2>&1 || $(CARGO) install cargo-license
	@$(PYTHON) scripts/generate_third_party_notices.py
	@echo "✓ THIRD_PARTY_NOTICES.md updated"

notices-check: ## Fail if THIRD_PARTY_NOTICES.md is stale relative to current deps
	@$(PYTHON) scripts/generate_third_party_notices.py --check

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

# ─── Version ────────────────────────────────────────────
bump-version: ## Bump version across all components (VERSION=X.Y.Z [DRY_RUN=--dry-run])
	@test -n "$(VERSION)" || (echo "error: VERSION is required (e.g. make bump-version VERSION=0.3.0)" && exit 1)
	$(PYTHON) scripts/bump_version.py $(VERSION) $(DRY_RUN)
