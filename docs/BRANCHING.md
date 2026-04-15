# Persatrix — Git Branching Strategy

## Overview

Persatrix uses a **trunk-based development** model with short-lived feature
branches and release branches per milestone. This strategy is chosen because:

- The project ships in clear phases (v0.1 → v0.2 → v0.3) with distinct feature sets
- It's a polyglot codebase (Go + Python + Rust) where cross-language changes are common
- Early-stage development favors fast iteration over ceremonial branching
- The small team (initially) doesn't benefit from the overhead of full Git Flow

---

## Branch Types

### `main` — The Trunk

The single source of truth. Always in a deployable state.

```
Rules:
  - All work merges here via pull request
  - Must pass CI (build + test + lint + validate) before merge
  - Squash merge preferred for feature branches (clean history)
  - Direct commits only for: typo fixes, CI config, README updates
  - Protected: requires at least 1 approval (when team > 1)
```

### `feature/*` — Short-Lived Feature Branches

For all new development. Branch from `main`, merge back to `main`.

```
Naming:    feature/<phase>-<component>-<description>
Lifetime:  1–5 days (ideally ≤ 3 days)
Merges to: main (via PR, squash merge)

Examples:
  feature/v01-registry-inmemory          # implement in-memory agent registry
  feature/v01-planner-yaml-parser        # workflow YAML parser
  feature/v01-executor-grpc-client       # gRPC executor
  feature/v01-security-permission-gate   # permission gate enforcement
  feature/v01-tools-builtin              # built-in tool implementations
  feature/v01-telemetry-otel-setup       # OTEL instrumentation
  feature/v01-cli-run-command            # CLI 'run' command
  feature/v02-persona-agent-interface    # PersonaAgent base class
  feature/v02-channels-core              # channel manager
  feature/v02-bridges-email              # email bridge
  feature/v03-mesh-node-runtime          # distributed node runtime
```

**Size guidance**: each feature branch should represent one logical unit of work
that can be reviewed in a single PR session (< 500 lines of meaningful change).
If a feature is larger, break it into sequential branches.

### `release/*` — Release Branches

Cut from `main` when a milestone is feature-complete and entering stabilization.

```
Naming:    release/<version>
Lifetime:  1–3 weeks (stabilization period)
Merges to: main (when released) + tag

Examples:
  release/0.1.0      # v0.1 core engine release
  release/0.2.0      # v0.2 agent societies release
  release/0.3.0      # v0.3 distributed mesh release
```

Only **bug fixes and documentation** go into release branches — no new features.
Bug fixes in a release branch are cherry-picked back to `main`.

### `hotfix/*` — Emergency Fixes

For critical bugs in a released version.

```
Naming:    hotfix/<version>-<description>
Lifetime:  Hours to 1 day
Merges to: release/* branch + main

Examples:
  hotfix/0.1.1-circuit-breaker-deadlock
  hotfix/0.1.2-secret-redaction-bypass
```

### `docs/*` — Documentation-Only Changes

For standalone documentation PRs that are not part of a feature branch
(e.g., new guides, process docs, cross-RFC documentation).

```
Naming:    docs/<description>
Lifetime:  1–3 days
Merges to: main (via PR, squash merge)

Examples:
  docs/development-workflow        # development lifecycle guide
  docs/branching-update            # branching strategy additions
  docs/spec-audit-followup         # spec audit documentation
```

Use `feature/*` when the docs change accompanies code changes.

---

## Branch Lifecycle

### Normal Development Flow

```
main ─────────────────────────────────────────────────────►
  │                                          │
  ├─ feature/v01-registry-inmemory ──────────┤ (squash merge)
  │                                          │
  ├─ feature/v01-planner-yaml-parser ────────┤
  │                                          │
  ├─ feature/v01-executor-grpc-client ───────┤
  │                                          │
  │   (v0.1 feature-complete)                │
  │                                          │
  ├─ release/0.1.0 ─── bugfix ─── bugfix ───► tag: v0.1.0
  │        │                                 │
  │        └── cherry-pick fixes back ───────┤
  │                                          │
  ├─ feature/v02-persona-agent-interface ────┤
  │                                          │
  ├─ feature/v02-channels-core ──────────────┤
  │                                          │
  ...
```

### Parallel Work Within a Phase

Multiple feature branches can run in parallel if they touch different
components. The dependency graph (see spec E12) determines what can
parallelize:

```
Phase 1 parallel tracks:
  Track A (Go core):     registry → planner → scheduler → executor
  Track B (Python core): base_agent → tools → builtin_tools → mcp_bridge
  Track C (Infra):       proto → otel → docker → ci

  These tracks are independent and can merge to main in any order.
  Integration points (e.g., executor needs base_agent) are coordinated
  by merging dependencies first.
```

### Cross-Language Changes

Many features span Go + Python (e.g., adding a new gRPC method requires
proto changes, Go server code, and Python client code). These should be
in a **single feature branch** to keep the codebase consistent:

```
feature/v01-executor-grpc-client
  ├── proto/task.proto                    (protobuf change)
  ├── internal/executor/executor.go       (Go implementation)
  ├── agents/server.py                    (Python gRPC server)
  └── tests/integration/test_executor.py  (integration test)
```

Never split a cross-language change across multiple PRs — the intermediate
state would leave the codebase broken.

---

## Phase-Based Branching Plan

### Phase 1: v0.1 Core Engine (Weeks 1–8)

```
Week 1-2: Foundation
  main
    ├─ feature/v01-proto-schemas              # protobuf definitions
    ├─ feature/v01-registry-inmemory          # agent registry
    ├─ feature/v01-base-agent-interface       # Python BaseAgent + gRPC server
    ├─ feature/v01-executor-grpc              # Go gRPC executor
    ├─ feature/v01-otel-foundation            # OTEL tracer + exporter setup
    └─ feature/v01-config-validation          # JSON Schema + orch validate

Week 3-4: Tools & MCP
    ├─ feature/v01-tools-builtin              # file_read, file_write, shell_exec
    ├─ feature/v01-tools-decorator            # @tool decorator + registry
    ├─ feature/v01-mcp-stdio                  # MCP stdio transport client
    ├─ feature/v01-mcp-sse                    # MCP SSE transport client
    ├─ feature/v01-permission-gate            # deny-by-default permission system
    └─ feature/v01-rate-limiter               # action rate limiting

Week 5-6: Workflows & Security
    ├─ feature/v01-planner-yaml               # YAML workflow parser
    ├─ feature/v01-planner-dag                # DAG builder + topological sort
    ├─ feature/v01-scheduler-parallel         # parallel execution
    ├─ feature/v01-resilience-circuit-breaker # circuit breakers + retry
    ├─ feature/v01-secrets-management         # env-var resolution, log redaction
    ├─ feature/v01-audit-logging              # append-only audit log
    └─ feature/v01-cost-tracking              # token counting + budget alerts

Week 7-8: CLI & Polish
    ├─ feature/v01-cli-core                   # orch run, validate, test, status
    ├─ feature/v01-testing-framework          # mock LLM replay, sandbox mode
    ├─ feature/v01-health-checks              # gRPC health, liveness/readiness
    ├─ feature/v01-graceful-shutdown           # drain, task handoff
    ├─ feature/v01-docker-compose             # local dev stack
    ├─ feature/v01-integration-tests          # end-to-end test suite
    └─ feature/v01-docs-readme                # README, getting-started guide

  release/0.1.0                               # cut from main, stabilize, tag
```

### Phase 2: v0.2 Agent Societies (Weeks 9–14)

```
Week 9-10: Persona & Channels
    ├─ feature/v02-persona-agent              # PersonaAgent class + event system
    ├─ feature/v02-proto-agent-message        # AgentMessage protobuf
    ├─ feature/v02-channels-core              # channel manager, DM, broadcast
    ├─ feature/v02-channels-history           # history storage + summarization
    └─ feature/v02-org-topology               # hierarchy + flat topologies

Week 11-12: Communication & Sub-Agents
    ├─ feature/v02-protocols-engine           # protocol engine (standup, debate)
    ├─ feature/v02-sub-agent-spawner          # ephemeral sub-agent lifecycle
    ├─ feature/v02-sub-agent-permissions      # child ≤ parent enforcement
    ├─ feature/v02-bridge-email               # SMTP/IMAP email bridge
    ├─ feature/v02-bridge-security            # content filter, PII detection
    └─ feature/v02-input-sanitization         # external input wrapping

Week 13-14: Optimization & Polish
    ├─ feature/v02-model-tiering              # task-based model routing
    ├─ feature/v02-context-management         # priority-weighted context window
    ├─ feature/v02-llm-cache                  # exact-match response cache
    ├─ feature/v02-session-replay             # CLI replay + export
    ├─ feature/v02-inline-evaluators          # schema check + safety gate
    ├─ feature/v02-budget-enforcement         # per-agent/workflow spend limits
    ├─ feature/v02-blueprints                 # software-team + social-experiment
    └─ feature/v02-human-participant          # human-as-agent via bridge

  release/0.2.0
```

### Phase 3: v0.3 Distributed Mesh (Weeks 15–22)

```
    ├─ feature/v03-node-runtime               # agent runtime as service
    ├─ feature/v03-agent-addressing           # agent@node resolution
    ├─ feature/v03-hub-spoke-routing          # central orchestrator routing
    ├─ feature/v03-mtls                       # mutual TLS between nodes
    ├─ feature/v03-node-admission             # allowlist + fingerprint
    ├─ feature/v03-trust-domains              # per-domain permission boundaries
    ├─ feature/v03-offline-handling           # message queuing, status broadcasts
    ├─ feature/v03-agent-migration            # state transfer between nodes
    ├─ feature/v03-a2a-server                 # expose agents as A2A endpoints
    ├─ feature/v03-a2a-client                 # discover + delegate to external
    ├─ feature/v03-wire-compression           # gRPC zstd, delta sync
    ├─ feature/v03-platform-exporters         # AgentOps, Langfuse, Datadog
    ├─ feature/v03-mesh-cli                   # orch node, mesh status, trace
    └─ feature/v03-data-residency             # region pinning, transit rules

  release/0.3.0
```

---

## Commit Convention

Use [Conventional Commits](https://www.conventionalcommits.org/) for a
parseable, automatable commit history:

```
<type>(<scope>): <description>

Types:
  feat     — new feature
  fix      — bug fix
  refactor — code change that neither fixes a bug nor adds a feature
  test     — adding or fixing tests
  docs     — documentation only
  chore    — build process, CI, dependencies
  perf     — performance improvement
  security — security fix or hardening

Scopes (match internal packages):
  registry, planner, scheduler, executor, state, mcp, security,
  resilience, telemetry, cost, channels, protocols, bridges, mesh,
  a2a, tools, persona, cli, proto, config, docker, ci

Examples:
  feat(registry): implement in-memory agent registry
  feat(tools): add @tool decorator with auto parameter schema
  fix(resilience): prevent circuit breaker deadlock on concurrent resets
  test(executor): add integration test for gRPC task execution
  docs(readme): add quick start guide
  chore(proto): regenerate Go + Python stubs
  security(secrets): add log redaction for Anthropic API key pattern
  perf(cache): add exact-match LLM response cache
```

### Commit Message Body

For non-trivial changes, include a body explaining **why** (not what — the
diff shows what):

```
fix(resilience): prevent circuit breaker deadlock on concurrent resets

The circuit breaker's half-open state transition was holding a write lock
while making a test request, which could itself trigger a state change
needing the same lock. Switch to a lock-free atomic state machine.

Spec ref: §6.7 (Error Handling & Resilience)
```

---

## Pull Request Guidelines

### PR Title

Same format as commit convention (since we squash merge, the PR title
becomes the commit message):

```
feat(planner): implement YAML workflow parser with template variables
```

### PR Description Template

```markdown
## What

Brief description of the change.

## Why

Link to spec section and/or issue.

## How

High-level approach (especially for cross-language changes).

## Spec Reference

- Main spec: §X.Y
- Extension spec: EX.Y

## Checklist

- [ ] Tests pass (`make test`)
- [ ] Lint passes (`make lint`)
- [ ] Config validates (`make validate`)
- [ ] Cross-language consistency (proto + Go + Python all updated)
- [ ] OTEL spans added for new operations
- [ ] TODOs removed or converted to issues
```

### PR Size

Target: **< 500 lines** of meaningful change (excluding generated code,
config files, and test fixtures). If larger, split into stacked PRs.

### Review

- Author should self-review before requesting review
- Reviewer focuses on: correctness, spec compliance, cross-language
  consistency, security implications, and test coverage
- Use "Request Changes" for blockers, "Comment" for suggestions

---

## Tags & Releases

### Version Tags

```
v0.1.0        # first stable release of core engine
v0.1.1        # hotfix release
v0.2.0        # agent societies release
v0.2.1        # hotfix
v0.3.0        # distributed mesh release
```

Semantic versioning: `MAJOR.MINOR.PATCH`
- MAJOR: breaking changes to agent interface, config format, or API
- MINOR: new features (backward compatible)
- PATCH: bug fixes only

### Release Process

```
1. All phase features merged to main
2. Cut release/X.Y.Z branch from main
3. Stabilization period (1–3 weeks):
   - Bug fixes only (no new features)
   - Fix → commit to release branch → cherry-pick to main
4. Final testing on release branch
5. Tag: git tag -a vX.Y.Z -m "Release X.Y.Z"
6. Merge release branch to main (fast-forward)
7. Build and publish artifacts:
   - Go binary (persatrix-server)
   - Python package (Persatrix-agents on PyPI)
   - Rust binary (orch CLI)
   - Docker images (ghcr.io/Persatrix/*)
8. GitHub Release with changelog (auto-generated from conventional commits)
```

---

## CI/CD Integration

### Branch Protection Rules (GitHub)

```yaml
# .github/branch-protection.yml (conceptual, configure in GitHub UI)
main:
  required_reviews: 1               # when team > 1
  require_status_checks:
    - build-go
    - build-python
    - build-rust
    - test-go
    - test-python
    - test-integration
    - lint
    - validate-config
  require_linear_history: true      # squash merge enforced
  restrict_pushes: true             # no direct pushes

release/*:
  required_reviews: 1
  require_status_checks: [build-go, build-python, test-go, test-python]
```

### CI Pipeline Per Branch Type

```
feature/* branches:
  On push:
    1. Build all (Go + Python + Rust)
    2. Run unit tests (Go + Python)
    3. Run lint (Go + Python + Rust)
    4. Validate configs
  On PR to main:
    5. Run integration tests
    6. Check commit message format
    7. Report test coverage delta

main branch:
  On push:
    1–7 above, plus:
    8. Build Docker images
    9. Push to staging registry (ghcr.io/Persatrix/*:main)
    10. Deploy to staging environment (if configured)

release/* branches:
  On push:
    1–7 above, plus:
    8. Build release Docker images
    9. Run extended test suite (longer timeouts, more scenarios)

Tags (vX.Y.Z):
    1. Build release artifacts (binaries + Docker images)
    2. Push to production registry (ghcr.io/Persatrix/*:vX.Y.Z)
    3. Publish Python package to PyPI
    4. Create GitHub Release with auto-changelog
```

---

## FAQ

**Q: Can I work directly on `main` for small changes?**
A: Only for trivial fixes (typos, comment updates, CI config). Everything
else goes through a feature branch + PR, even if you're the only contributor.
This creates a reviewable history and ensures CI runs before merge.

**Q: What if my feature depends on another feature branch not yet merged?**
A: Merge the dependency branch to `main` first, then branch your feature
from the updated `main`. If that's not possible (both in progress), you can
stack branches: branch B from branch A, but be aware this complicates rebasing.
Prefer smaller, faster-merging branches to avoid this.

**Q: Should I rebase or merge to keep my feature branch up to date?**
A: **Rebase** your feature branch onto `main` to keep history linear. Since
we squash-merge PRs, the feature branch history doesn't matter — only the
final squashed commit on `main` matters.

**Q: How do I handle a breaking change to the agent YAML schema?**
A: Breaking changes should only happen at MINOR version boundaries (v0.2.0,
v0.3.0). Bump the `schema_version` field, add a migration path in
`orch migrate`, and document the change in the changelog. Never ship a
breaking schema change in a PATCH release.

**Q: What if a v0.2 feature is ready but v0.1 hasn't shipped yet?**
A: Don't merge it to `main` until the release/0.1.0 branch is cut. Otherwise
it complicates the v0.1 stabilization. Work on it in a feature branch and
hold the PR until after the release branch is created.
