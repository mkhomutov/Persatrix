.PHONY: all build build-orchestrator build-cli build-agents proto proto-go proto-python proto-python-check proto-orphans-check proto-check clean reset test lint run validate help generate-persona-nickname generate-sanitizer-patterns generate-sanitizer-patterns-check check-licenses check-licenses-go check-licenses-python check-licenses-rust notices notices-check bump-version issues issues-check

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

proto-python: ## Generate Python gRPC stubs from protobuf definitions (incl. mypy stubs)
	@echo "→ Generating Python protobuf stubs..."
	@mkdir -p $(PROTO_PY_OUT)
	@# --mypy_out emits *_pb2.pyi alongside *_pb2.py via the `protoc-gen-mypy`
	@# plugin from the `mypy-protobuf` dev dependency (see agents/pyproject.toml).
	@# Replaces the hand-maintained stub previously committed under
	@# agents/generated/task_pb2.pyi; ISSUE-0017.
	$(PYTHON) -m grpc_tools.protoc --python_out=$(PROTO_PY_OUT) --grpc_python_out=$(PROTO_PY_OUT) \
		--mypy_out=$(PROTO_PY_OUT) \
		-I $(PROTO_DIR) $(PROTO_DIR)/*.proto
	@# grpc_tools.protoc emits top-level 'import X_pb2' which fails at runtime
	@# when the package is installed as persatrix_agents.generated.* (agents/generated/
	@# is not on sys.path). Rewrite to 'from . import X_pb2' so the stubs work in
	@# the installed layout. ISSUE-0016 / PR #246 finding M1.
	@$(PYTHON) -c "\
import re, pathlib; \
[f.write_text(re.sub(r'^import (\\w+_pb2\\b)', r'from . import \\1', \
  f.read_text(encoding='utf-8'), flags=re.MULTILINE), encoding='utf-8') \
 for f in pathlib.Path('$(PROTO_PY_OUT)').glob('*_pb2_grpc.py')]"
	@echo "✓ Python protobuf stubs generated"

proto-python-check: ## Fail if agents/generated/*.pyi / *_pb2.py / *_pb2_grpc.py is stale relative to proto/*.proto (ISSUE-0017 + ISSUE-0023)
	@echo "→ Checking generated Python protobuf stubs are in sync with proto/..."
	@# Regenerate *.py + *.pyi + *_grpc.py into a tmp dir, apply the
	@# same relative-import rewrite the proto-python target applies,
	@# then byte-compare against the committed copies. Catches the
	@# three drift classes called out in ISSUE-0023:
	@#   1. .pyi stale (was the only class covered before this change)
	@#   2. _pb2.py hand-edited or .proto-changed-without-regen
	@#   3. _pb2_grpc.py hand-edited or .proto-changed-without-regen
	@tmpdir=$$(mktemp -d) || exit 1; \
	$(PYTHON) -m grpc_tools.protoc --python_out=$$tmpdir --grpc_python_out=$$tmpdir \
		--mypy_out=$$tmpdir \
		-I $(PROTO_DIR) $(PROTO_DIR)/*.proto || { rm -rf $$tmpdir; exit 1; }; \
	$(PYTHON) -c "\
	import re, pathlib; \
	[f.write_text(re.sub(r'^import (\\w+_pb2\\b)', r'from . import \\1', \
	  f.read_text(encoding='utf-8'), flags=re.MULTILINE), encoding='utf-8') \
	 for f in pathlib.Path('$$tmpdir').glob('*_pb2_grpc.py')]; \
	" || { rm -rf $$tmpdir; exit 1; }; \
	stale=0; \
	for f in $$tmpdir/*.py $$tmpdir/*.pyi; do \
		base=$$(basename $$f); \
		if ! diff -q "$(PROTO_PY_OUT)/$$base" "$$f" >/dev/null 2>&1; then \
			echo "✗ $(PROTO_PY_OUT)/$$base is stale; run: make proto-python"; \
			stale=1; \
		fi; \
	done; \
	rm -rf $$tmpdir; \
	if [ $$stale -ne 0 ]; then exit 1; fi
	@echo "✓ agents/generated/*.py + *.pyi are in sync with proto/"

proto-orphans-check: ## Fail if generated stubs survive after their proto source was deleted (ISSUE-0023)
	@$(PYTHON) scripts/checks/proto_drift.py

proto-check: proto-python-check proto-orphans-check ## Run every proto-source-of-truth gate (Python freshness + orphan detection)
	@echo "✓ proto/ source-of-truth gates passed"

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

generate-sanitizer-patterns: ## Regenerate agents/security_patterns.py + agents/security_enums.py from the Go canonical sources (RFC 0009 PR 3 + #254)
	@echo "→ Regenerating agents/security_patterns.py + agents/security_enums.py from Go canonical sources..."
	@go run ./cmd/genpatterns \
		-out agents/security_patterns.py \
		-enums-out agents/security_enums.py
	@echo "✓ agents/security_patterns.py + agents/security_enums.py regenerated"

generate-sanitizer-patterns-check: ## Fail if agents/security_patterns.py or agents/security_enums.py is stale relative to the Go source
	@echo "→ Checking generated security mirrors are in sync with the Go source..."
	@go run ./cmd/genpatterns \
		-out agents/security_patterns.py.check \
		-enums-out agents/security_enums.py.check
	@stale=0; \
	if ! diff -q agents/security_patterns.py agents/security_patterns.py.check >/dev/null 2>&1; then \
		echo "✗ agents/security_patterns.py is stale; run: make generate-sanitizer-patterns"; \
		stale=1; \
	fi; \
	if ! diff -q agents/security_enums.py agents/security_enums.py.check >/dev/null 2>&1; then \
		echo "✗ agents/security_enums.py is stale; run: make generate-sanitizer-patterns"; \
		stale=1; \
	fi; \
	rm -f agents/security_patterns.py.check agents/security_enums.py.check; \
	if [ $$stale -ne 0 ]; then exit 1; fi
	@echo "✓ agents/security_patterns.py + agents/security_enums.py are in sync"

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
	@# modernc.org/mathutil ships its license under a non-standard filename
	@# (LICENSE-MATHUTIL) that go-licenses cannot auto-detect. Upstream license
	@# is BSD-3-Clause; verified manually and recorded in THIRD_PARTY_NOTICES.md.
	@ALLOWED=$$(grep -v '^\s*#' scripts/checks/allowed_licenses.txt | grep -v '^\s*$$' | paste -sd, -); \
		go-licenses check ./cmd/... ./internal/... \
			--allowed_licenses="$$ALLOWED" \
			--ignore=$(GO_MODULE) \
			--ignore=modernc.org/mathutil
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
	@echo "→ Checking instructions_file references resolve..."
	$(PYTHON) scripts/checks/prompt_refs.py
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

reset: ## Stop the stack and purge named volumes (channels DB + persona memory) — operator workaround for F-3 cross-run state bleed; see docs/issues/ISSUE-0051
	@echo "→ Stopping stack and removing named volumes..."
	@# `docker compose down -v` is idempotent: it tears down whatever is up
	@# (no-op if already down) and removes only the volumes declared in this
	@# compose project. Re-running it after a successful reset succeeds
	@# cleanly — there is nothing left to remove.
	docker compose down -v
	@echo "✓ Stack stopped; channels DB + persona memory wiped"
	@echo "  Note: this is an operator workaround for F-3 cross-run state bleed."
	@echo "  Root-cause fix (per-session memory namespacing) tracked in"
	@echo "  docs/issues/ISSUE-0051-per-session-memory-namespacing-channels.md"

# ─── Clean ──────────────────────────────────────────────
clean: ## Remove build artifacts
	rm -rf $(GO_BIN) $(PROTO_GO_OUT) $(PROTO_PY_OUT)
	cd cli && $(CARGO) clean
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true

# ─── Issues ─────────────────────────────────────────────
issues: ## Regenerate docs/issues/INDEX.md from per-issue front-matter
	$(PYTHON) scripts/issues.py --print

issues-check: ## Fail if INDEX.md is stale or front-matter is invalid (CI)
	$(PYTHON) scripts/issues.py --check

# ─── Version ────────────────────────────────────────────
bump-version: ## Bump version across all components (VERSION=X.Y.Z [DRY_RUN=--dry-run])
	@test -n "$(VERSION)" || (echo "error: VERSION is required (e.g. make bump-version VERSION=0.3.0)" && exit 1)
	$(PYTHON) scripts/bump_version.py $(VERSION) $(DRY_RUN)
