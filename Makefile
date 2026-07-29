.PHONY: all build build-orchestrator build-orchestrator-ui ui ui-test ui-html-check build-cli build-agents proto proto-go proto-python proto-python-check proto-orphans-check proto-check clean reset test lint run run-ui validate dockerignore-check help demo-autonomous demo-offline demo-ollama generate-persona-nickname generate-sanitizer-patterns generate-sanitizer-patterns-check check-licenses check-licenses-go check-licenses-python check-licenses-rust notices notices-check bump-version issues issues-check rfcs rfcs-check imports-check eval-replay eval-record eval-record-offline eval-drift

# ─── Config ─────────────────────────────────────────────
GO_MODULE     := github.com/mkhomutov/persatrix
GO_BIN        := bin
PROTO_DIR     := proto
PROTO_GO_OUT  := internal/generated
PROTO_PY_OUT  := agents/generated
PYTHON        := python3
PIP           := pip3
CARGO         := cargo
NPM           := npm
WEB_DIR       := web
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
	@# `newline='\n'` pins LF on disk regardless of platform — Python text-mode
	@# write defaults to os.linesep, which produces CRLF on Windows and breaks
	@# byte-parity gates against the LF-only blobs. v0.3.0 release-prep PR 4.
	@$(PYTHON) -c "\
import re, pathlib; \
[f.write_text(re.sub(r'^import (\\w+_pb2\\b)', r'from . import \\1', \
  f.read_text(encoding='utf-8'), flags=re.MULTILINE), encoding='utf-8', newline='\\n') \
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
	  f.read_text(encoding='utf-8'), flags=re.MULTILINE), encoding='utf-8', newline='\\n') \
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

ui: ## Build the embedded web console (RFC 0048) into internal/ui/assets/
	@echo "→ Building web console (Svelte/Vite)..."
	cd $(WEB_DIR) && $(NPM) ci && $(NPM) run build
	@echo "✓ Web console built → internal/ui/assets/ (embed via WithUI, serve with --enable-ui)"

ui-test: ## Run the web console's unit tests (Vitest)
	cd $(WEB_DIR) && $(NPM) ci && $(NPM) test

ui-html-check: ## Fail if a {@html} directive appears under web/src (RFC 0039 amendment §A3 XSS gate, CI)
	$(PYTHON) scripts/checks/ui_html_directive.py

build-orchestrator-ui: ui build-orchestrator ## Build the orchestrator with the real console bundle embedded (release/asset lane)

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

run-ui: ui build-orchestrator ## Build the console bundle + orchestrator and run locally with the web console enabled (RFC 0048 — local UI iteration)
	@echo "→ Web console enabled at http://localhost:8080/ui"
	$(GO_BIN)/persatrix-server$(EXE) --config config/ --enable-ui

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

# ─── Eval (RFC 0044 golden-trace harness) ───────────────
# Phase 1 (v0.3.11) ships the runner; the seed recipes + `.golden.yaml`
# sidecars land in PR 4 (gated on RFC 0041 typed events), so an empty
# `evaluators/eval_sets/` makes these a clean no-op today. Pass TARGET=<id>
# to scope to one recipe, REPORT=<path> to write the structured JSON artifact.
# Replay pins the offline optimization overlay: Phase-1 goldens are recorded
# against the mock (`make eval-record-offline`), and a replay must resolve the
# same model aliases the record did. The action loop hashes the raw alias
# (`quality`, env-independent), but the RFC 0020 close-summary and RFC 0051
# critic paths hash the *resolved physical* model, so replaying under a different
# optimization config would shift those requests and miss the cassette. Replay
# does not need the offline responses file (it plays the golden, not the mock).
#
# LIMITATION: this pins the offline overlay for EVERY target, so it replays only
# goldens recorded under that overlay. The release-prep live re-record of
# EVAL-MEMORY-001 bakes in the real physical models and would miss the cassette
# here until this target resolves the overlay per recipe — a follow-up parked in
# docs/rfcs/0044-pr-plan.md (§Notes).
eval-replay: ## Replay golden-trace evals deterministically (RFC 0044). TARGET / REPORT optional.
	PERSATRIX_OPTIMIZATION_CONFIG=config/demo/offline/optimization.yaml \
	$(PYTHON) -m evaluators.runner --mode replay $(if $(TARGET),--target $(TARGET),) $(if $(REPORT),--report $(REPORT),)

eval-record: ## Record a golden from a live run (author-only; overwrites the sidecar). TARGET=<id>.
	$(PYTHON) -m evaluators.runner --mode record $(if $(TARGET),--target $(TARGET),)

# Record a golden deterministically against the offline mock ($0, no API key) —
# the pre-0041 seed path (RFC 0044 Phase 1). The offline optimization overlay
# points the `quality` alias at the mock (config/demo/offline/optimization.yaml)
# and the eval fixtures feed the curated replies; the resulting golden replays
# with `make eval-replay` (which needs neither, since replay plays the cassette).
eval-record-offline: ## Re-record a seed golden against the mock, deterministically ($0). TARGET=<id>.
	PERSATRIX_OPTIMIZATION_CONFIG=config/demo/offline/optimization.yaml \
	PERSATRIX_OFFLINE_RESPONSES=evaluators/eval_sets/offline_responses.eval.yaml \
	$(PYTHON) -m evaluators.runner --mode record $(if $(TARGET),--target $(TARGET),)

eval-drift: ## Live drift check against recorded goldens (reports, never gates). TARGET optional.
	$(PYTHON) -m evaluators.runner --mode drift $(if $(TARGET),--target $(TARGET),)

# ─── Lint ───────────────────────────────────────────────
lint: lint-go lint-python lint-rust ## Lint all code

lint-go:
	golangci-lint run ./...

lint-python: imports-check
	cd agents && $(PYTHON) -m ruff check . && $(PYTHON) -m mypy .
	@# ISSUE-0056 + ISSUE-0062: the line above runs `cd agents`, leaving the
	@# repo-root tests/ tree unchecked. Lint and type-check it from the repo
	@# root via the root ruff.toml / mypy.ini. RFC 0044 adds the sibling
	@# evaluators/ tree (the golden-trace eval harness) — a repo-root package,
	@# not under agents/ — so it rides the same root invocation.
	$(PYTHON) -m ruff check tests/ evaluators/
	$(PYTHON) -m mypy tests/ evaluators/

imports-check: ## Fail if an MIT-candidate primitive imports orchestrator-coupled (BUSL) code (RFC 0045 §B dependency-direction gate)
	@# RFC 0045 §B — the MIT↛BUSL dependency-direction gate. Runs the
	@# `[tool.importlinter]` forbidden contract in agents/pyproject.toml so a
	@# leaf MIT-candidate (wallet client, prompt-safety kit, mock provider)
	@# that grows an import into orchestrator-internal (BUSL) code fails the
	@# build before the next mirror/release ships BUSL source under MIT terms.
	@# `lint-imports` auto-discovers the contract from the cwd's pyproject and
	@# needs the editable install (`make build-agents`) so root_package
	@# `persatrix_agents` resolves. Regression suite:
	@# tests/unit/python/test_dependency_direction_imports.py. `--no-cache`
	@# keeps the one-shot CI gate deterministic and leaves no cache dir behind.
	cd agents && lint-imports --no-cache

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
	@# pytest-timeout 2.4.0 declares `License: MIT` and the OSI-MIT classifier,
	@# but its metadata also carries the legacy `License :: DFSG approved`
	@# Trove classifier; `pip-licenses --from=mixed` concatenates both into
	@# "DFSG approved; MIT License", which the strict-split allow-list checker
	@# rejects (the SPDX token is "MIT", not "MIT License"). Genuinely MIT,
	@# reviewed exception. Added in v0.3.0 release-prep PR 4. See ISSUE-0024
	@# for why the dep is required (pytest hangs without it).
	@$(PYTHON) scripts/checks/python_licenses.py --exception Persatrix-agents --exception pytest-timeout

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
# NOTE(RFC 0048): the orchestrator image builds the web console bundle in a
# Node stage inside Dockerfile.orchestrator, so docker-build / demo-* embed the
# real console with NO host JS toolchain and no prior `make ui` — even a bare
# `docker compose up --build` from a clean clone works. For local (non-Docker)
# UI iteration use `make run-ui` (or `make build-orchestrator-ui`) instead.
docker-build: ## Build Docker images
	docker compose build

dockerignore-check: ## Fail if .dockerignore lets nested web/node_modules leak into the build context (ISSUE-0104, CI)
	$(PYTHON) scripts/checks/dockerignore_context.py --strict

docker-up: ## Start all services
	docker compose up -d

docker-down: ## Stop all services
	docker compose down

docker-logs: ## Tail logs
	docker compose logs -f

demo-anthropic: ## Run the demo society on Anthropic (Claude) — needs ANTHROPIC_API_KEY; spends real money
	@echo "→ Starting Persatrix on Anthropic (Claude) — REAL cloud calls, REAL spend."
	@echo "  Needs ANTHROPIC_API_KEY in your environment or .env. Set a hard cap at https://console.anthropic.com/ first."
	@# Provider selection is config-driven (RFC 0033 — no force-knob, no default
	@# provider): the base config ships UNCONFIGURED, so this overlay mounts an
	@# alias config pointing every agent at `provider: anthropic`. Anthropic is a
	@# peer of openai / ollama / offline. --build matches the other demos.
	docker compose -f docker-compose.yaml -f docker-compose.anthropic.yaml up -d --build
	@echo "✓ Anthropic society up. Try:  ./bin/persatrix chat ember-owl"
	@echo "  Stop with: make docker-down"

demo-autonomous: build-cli ## Run the RFC 0052 offline autonomous brainstorm — the `roundtable` roster convenes ITSELF on the mock provider (zero keys, zero spend, no human turn)
	@echo "→ Starting the offline autonomous brainstorm (RFC 0052) — mock provider, no API key, no spend."
	@# Reuses the offline overlay: config/demo/offline/optimization.yaml points
	@# every alias at `provider: mock`, and PERSATRIX_OFFLINE_RESPONSES feeds the
	@# curated replies (config/offline_responses.yaml — incl. the monorepo
	@# roundtable scenario). The `roundtable` channel ships DISARMED in
	@# config/channels.yaml (a default-deploy safety posture — an armed channel is
	@# convenable, and would spend, on any default boot); this target arms it at
	@# RUNTIME and convenes it, so the bundled config stays safe at rest. This is
	@# the exact MT-AUTONOMOUS-001 operator flow (arm via CLI → convene via CLI),
	@# just on the mock provider. --build matches the other demos.
	docker compose -f docker-compose.yaml -f docker-compose.offline.yaml up -d --build
	@echo "→ Waiting for the orchestrator to become healthy..."
	@t=120; while ! curl -sf http://localhost:8080/healthz >/dev/null 2>&1; do \
		t=$$((t-2)); \
		if [ $$t -le 0 ]; then echo "✗ orchestrator did not become healthy in time — is Docker running?"; exit 1; fi; \
		sleep 2; \
	done
	@echo "→ Arming group:roundtable (nova-sparrow convenes; ember-owl chairs the synthesis)..."
	@# /healthz can go green a beat before the config-edit REST surface is ready,
	@# so retry the arm a few times rather than aborting the whole demo on a
	@# transient miss (the convene step below retries for the same reason).
	@for i in $$(seq 1 10); do \
		if $(GO_BIN)/persatrix channel config set group:roundtable autonomous.enabled=true; then break; fi; \
		if [ $$i -eq 10 ]; then echo "✗ could not arm group:roundtable — is config editing enabled on the orchestrator?"; exit 1; fi; \
		sleep 2; \
	done
	@echo "→ Convening — retrying while the persona agents finish registering..."
	@for i in $$(seq 1 20); do \
		if $(GO_BIN)/persatrix channel convene group:roundtable --json 2>/dev/null; then \
			echo ""; \
			echo "✓ Convened with ZERO human turns. The roundtable is brainstorming 'Should we adopt a monorepo?'"; \
			echo "  Watch it:  open http://localhost:8080/ui  (Channels → roundtable), or"; \
			echo "             $(GO_BIN)/persatrix agent interactions ember-owl  (once it closes — the synthesis + summaries)"; \
			echo "  Stop:      make docker-down"; \
			exit 0; \
		fi; \
		sleep 3; \
	done; \
	echo ""; \
	echo "⚠ Auto-convene did not land yet — the agents may still be registering, or it is already convening."; \
	echo "  Convene it yourself once the agents are up:  $(GO_BIN)/persatrix channel convene group:roundtable"

demo-offline: ## Run the demo society with ZERO cost — no API key, no network (mock provider)
	@echo "→ Starting Persatrix in offline mode (mock provider — no API calls, no spend)..."
	@# Provider selection is config-driven (RFC 0033 — no force-knob): the
	@# offline overlay mounts an alias config pointing every agent at
	@# `provider: mock`, so no API key is needed. --build so the agent image
	@# always bakes the current source (the Dockerfile COPYs + pip-installs
	@# agents/, so `up` alone would reuse a stale image). Layer caching keeps
	@# it fast when nothing changed.
	docker compose -f docker-compose.yaml -f docker-compose.offline.yaml up -d --build
	@echo "✓ Offline society up. Try:  ./bin/persatrix chat ember-owl"
	@echo "  Stop with: make docker-down"

demo-ollama: ## Run the demo society on a REAL local model via Ollama — no API key, no cloud spend (set PERSATRIX_OLLAMA_MODEL to change the model)
	@echo "→ Starting Persatrix on a local Ollama model ($(or $(PERSATRIX_OLLAMA_MODEL),llama3.2)) — no cloud calls, no per-token spend..."
	@echo "  First run pulls the model into the ollama-models volume — a few GB; this can take minutes."
	@# Provider selection is config-driven (RFC 0033 — no force-knob): the
	@# ollama overlay mounts an alias config pointing every agent at
	@# `provider: ollama` (base_url on the compose bridge). PERSATRIX_OLLAMA_MODEL
	@# overrides the pulled model AND the agents in lock-step. No API key needed.
	@# --build so the agent image bakes the current source (matches demo-offline).
	docker compose -f docker-compose.yaml -f docker-compose.ollama.yaml up -d --build
	@echo "✓ Local-model society up. Try:  ./bin/persatrix chat ember-owl"
	@echo "  Stop with: make docker-down  (the pulled model persists in the ollama-models volume)"

demo-openai: ## Run the demo society on OpenAI (cloud peer) — needs OPENAI_API_KEY; spends real money
	@echo "→ Starting Persatrix on OpenAI (gpt-4o / gpt-4o-mini) — REAL cloud calls, REAL spend."
	@echo "  Needs OPENAI_API_KEY in your environment or .env. Set a hard cap at https://platform.openai.com/ first."
	@# Provider selection is config-driven (RFC 0033 — no force-knob): the
	@# openai overlay mounts an alias config pointing every agent at
	@# `provider: openai` — the release's one-line provider swap as a demo. The
	@# base compose plumbs OPENAI_API_KEY into every agent (closes MT F-5).
	@# --build matches the other demos.
	docker compose -f docker-compose.yaml -f docker-compose.openai.yaml up -d --build
	@echo "✓ OpenAI society up. Try:  ./bin/persatrix chat ember-owl"
	@echo "  Stop with: make docker-down"

demo-gemini: ## Run the demo society on Google Gemini (cloud peer) — needs GEMINI_API_KEY (or GOOGLE_API_KEY); spends real money
	@echo "→ Starting Persatrix on Gemini (gemini-3.5-flash) — REAL cloud calls, REAL spend."
	@echo "  Needs GEMINI_API_KEY (or GOOGLE_API_KEY) in your environment or .env. Set a hard cap in Google AI Studio first."
	@# Provider selection is config-driven (RFC 0033 — no force-knob): the
	@# gemini overlay mounts an alias config pointing every agent at
	@# `provider: gemini` (native google-genai SDK — RFC 0053 OQ #1). Unlike the
	@# openai overlay it (1) installs the google-genai EXTRA via the
	@# AGENT_EXTRAS build arg — so `--build` is REQUIRED, not just conventional —
	@# and (2) plumbs GEMINI_API_KEY / GOOGLE_API_KEY (the base compose plumbs
	@# only ANTHROPIC/OPENAI).
	docker compose -f docker-compose.yaml -f docker-compose.gemini.yaml up -d --build
	@echo "✓ Gemini society up. Try:  ./bin/persatrix chat ember-owl"
	@echo "  Stop with: make docker-down"

demo-watsonx: ## Run the demo society on IBM watsonx.ai (cloud peer) — needs WATSONX_API_KEY + WATSONX_PROJECT_ID (non-secret); spends real money
	@echo "→ Starting Persatrix on watsonx.ai (llama-3-3-70b / granite-3-8b) — REAL cloud calls, REAL spend."
	@echo "  Needs, in your environment or .env: WATSONX_API_KEY (secret) AND WATSONX_PROJECT_ID"
	@echo "  (non-secret; or WATSONX_SPACE_ID). WATSONX_URL is optional (defaults to us-south)."
	@echo "  Set a cap in IBM Cloud first."
	@# Provider selection is config-driven (RFC 0033 — no force-knob): the
	@# watsonx overlay mounts an alias config pointing every agent at
	@# `provider: watsonx` (native ibm-watsonx-ai SDK — RFC 0053 §C). Unlike the
	@# openai overlay it (1) installs the ibm-watsonx-ai EXTRA via the
	@# AGENT_EXTRAS build arg — so `--build` is REQUIRED, not just conventional —
	@# and (2) plumbs the SECRET WATSONX_API_KEY plus the non-secret
	@# WATSONX_PROJECT_ID/URL env fallbacks (provider_config wins, RFC 0053 §C).
	@# Preflight: the factory fails CLOSED at agent startup on an absent
	@# project_id/space_id (llm_factory.py — RFC 0053 §C). That is correct, but
	@# it surfaces only as crash-looping agents + an EMPTY web-console persona
	@# picker (the orchestrator still boots and serves the UI), which reads as a
	@# broken build rather than an unfinished config. Catch it HERE — before
	@# compose builds/boots anything — with an actionable message. The id is
	@# NON-secret config resolvable from EITHER channel (resolve_watsonx_config),
	@# so this passes when a non-empty project_id OR space_id is set in ANY of:
	@# the mounted config, the live env, or .env (docker compose reads .env; this
	@# recipe's shell does not, so grep it directly). space_id counts too. All
	@# three empty → this blocks. The value pattern rejects "" (empty quotes).
	@if ! { \
		grep -Eq '^[[:space:]]*(project_id|space_id):[[:space:]]*("[^"]+"|[^[:space:]"])' config/demo/watsonx/optimization.yaml \
		|| [ -n "$$WATSONX_PROJECT_ID$$WATSONX_SPACE_ID" ] \
		|| { [ -f .env ] && grep -Eq '^[[:space:]]*(export[[:space:]]+)?WATSONX_(PROJECT_ID|SPACE_ID)=[[:space:]]*("[^"]+"|[^[:space:]"#])' .env; }; \
	}; then \
		echo "✗ No watsonx project_id found (config, env, or .env all empty → fail-closed at startup)."; \
		echo "  watsonx needs a project_id (or space_id) — NON-secret config the client cannot be"; \
		echo "  built without (RFC 0053 §C). Without it every agent fails closed at startup and the"; \
		echo "  web-console persona picker comes up empty. Set it in EITHER channel:"; \
		echo "    • .env (recommended — keeps your id out of VCS):  WATSONX_PROJECT_ID=<your-project-id>"; \
		echo "      (and, for a non-us-south region, WATSONX_URL=https://<region>.ml.cloud.ibm.com); or"; \
		echo "    • config/demo/watsonx/optimization.yaml: set project_id in all three alias blocks."; \
		echo "  Then re-run:  make demo-watsonx"; \
		exit 1; \
	fi
	docker compose -f docker-compose.yaml -f docker-compose.watsonx.yaml up -d --build
	@echo "✓ watsonx society up. Try:  ./bin/persatrix chat ember-owl"
	@echo "  Stop with: make docker-down"

reset: ## Stop the stack and purge ALL named volumes (channels DB / orchestrator-data, persona memory / ember-owl-data + iron-fox-data + nova-sparrow-data, agent scratch / workspace) — operator workaround for F-3 cross-run state bleed; see docs/issues/ISSUE-0051
	@echo "→ Stopping stack and removing named volumes..."
	@# `docker compose down -v` is idempotent: it tears down whatever is up
	@# (no-op if already down) and removes every volume declared in this
	@# compose project — currently orchestrator-data, ember-owl-data,
	@# iron-fox-data, nova-sparrow-data, and workspace. Re-running after a
	@# successful reset succeeds cleanly.
	docker compose down -v
	@echo "✓ Stack stopped; wiped channels DB (orchestrator-data),"
	@echo "  persona memory (ember-owl-data, iron-fox-data, nova-sparrow-data),"
	@echo "  and agent scratch (workspace)."
	@echo "  Restart with: make docker-up"
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

issues-check: ## Fail if docs/issues/INDEX.md is stale or front-matter is invalid (CI)
	$(PYTHON) scripts/issues.py --check

# ─── RFCs ───────────────────────────────────────────────
rfcs: ## Regenerate docs/rfcs/INDEX.md from per-RFC YAML front-matter
	$(PYTHON) scripts/rfcs.py --print

rfcs-check: ## Fail if docs/rfcs/INDEX.md is stale or front-matter is invalid (CI)
	$(PYTHON) scripts/rfcs.py --check

# ─── Version ────────────────────────────────────────────
bump-version: ## Bump version across all components (VERSION=X.Y.Z [DRY_RUN=--dry-run])
	@test -n "$(VERSION)" || (echo "error: VERSION is required (e.g. make bump-version VERSION=0.3.0)" && exit 1)
	$(PYTHON) scripts/bump_version.py $(VERSION) $(DRY_RUN)
