# Changelog

All notable changes to this project will be documented in this file.

## [0.1.0] - 2026-04-11

### 🚀 Features

- Scaffold initial project structure (#1)
- Adopt blueprint tooling for project governance and quality gates (#2)
- *(state)* Implement InMemoryStateStore (RFC 0001, PR 1/5) (#6)
- *(registry)* Implement InMemoryRegistry (RFC 0001, PR 2/5) (#7)
- *(planner)* Implement YAMLPlanner Parse+DAG+Plan (RFC 0001, PR 3a/5) (#8)
- *(planner)* Implement ResolveInputs template resolution (RFC 0001, PR 4/5) (#9)
- *(orchestrator)* Wire state, registry, planner into main.go (RFC 0001, PR 5/5) (#10)
- *(server)* HTTP server scaffolding + workflow handlers (RFC 0002, Phase 1) (#14)
- *(server)* Implement agent registry endpoints (RFC 0002, PR 3/4) (#16)
- *(server)* Stub endpoints + main.go wiring + Docker fix (RFC 0002, PR 4/4) (#17)
- *(proto)* Generate Go gRPC stubs from protobuf definitions (#21)
- *(executor)* GRPCExecutor core with retry logic (#22)
- *(state)* Add RunRetrying, SetRunTimestamps, SetRunError (RFC 0003, PR 4/7) (#24)
- *(scheduler)* WorkflowScheduler core with polling, parallel stages, dedup (RFC 0003, PR 3a/7) (#25)
- *(orchestrator)* Wire scheduler + executor into main.go (RFC 0003, PR 5/7) (#27)
- *(agents)* PermissionGate + PathValidator (RFC 0004, PR 2/7) (#36)
- *(agents)* Built-in tools + PR 2 follow-up fixes (RFC 0004, PR 3/7) (#37)
- *(agents)* LLM client + TaskInputConfig + base handle loop (RFC 0004, PR 4a/7) (#38)
- *(agents)* CoderAgent, ReviewerAgent, PlannerAgent (RFC 0004, PR 4b/7) (#39)
- *(agents)* GRPC server + agent loading + proto stubs (RFC 0004, PR 5a) (#40)
- *(agents)* Self-registration + integration tests + follow-up fixes (RFC 0004, PR 5b/7) (#41)

### 🐛 Bug Fixes

- Address accumulated review findings (RFC 0001, PR 6/6) (#12)
- Address accumulated review findings (RFC 0002, PR 5/5) (#18)
- *(state)* Replace rune-based test IDs with fmt.Sprintf (RFC 0001, F-06) (#30)
- *(executor)* Additive dial options, mid-dispatch cancel & retry stress tests (RFC 0003, PR 6) (#31)
- *(orchestrator)* Graceful shutdown drain + absolute workflowsDir (RFC 0003, PR 8) (#33)
- *(agents)* Registration follow-ups + RFC 0004 close (PR 6/7) (#42)
- *(lint)* Resolve all golangci-lint, ruff, mypy, clippy warnings (#44)

### 📚 Documentation

- RFC 0001 Core Orchestration Pipeline (#3)
- PR implementation plan for RFC 0001 (#5)
- *(plan)* Update PR plan with PR #8 review follow-ups
- RFC 0002 REST API Server (#4)
- PR implementation plan for RFC 0002 (#11)
- RFC 0003 Scheduler & Executor (#13)
- RFC 0004 Python Agent gRPC Server (#15)
- RFC 0004 PR implementation plan (#19)
- Add ROADMAP.md, status hygiene rules, fix pre-commit checks (#20)
- Update PR plan with PR #22 review findings (N-06..N-11)
- Add follow-up PRs 6-9 to RFC 0003 PR plan (#28)
- Close RFC 0002 PR plan — mark PR 5 as superseded
- *(rfc0001)* Complete PR 6 follow-up scope with all carry-forward findings (#29)
- RFC 0003/0004 status updates, multi-provider LLM design, v0.2 deferrals (#35)
- Update progress tracking for PR #39 merge (RFC 0004, 5/7)
- *(roadmap)* Add missing merged PRs #28, #29, #30, #35 to history table
- Add v0.1 release checklist (#43)

### 🧪 Testing

- *(executor)* IsTransient table-driven tests, retry edge cases, concurrent dispatch (#23)
- *(scheduler)* Step execution, template resolution, error path coverage (RFC 0003, PR 3b/7) (#26)
- Observability improvements — concurrent race tests, log assertions, zaptest logger (#32)

### 🏗️ Build

- *(proto)* Split make proto into go/python targets + CI staleness check (RFC 0003, PR 9) (#34)

### 📦 Miscellaneous

- Update FILEMAP.md


