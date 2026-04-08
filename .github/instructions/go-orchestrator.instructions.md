---
applyTo: "internal/**/*.go,cmd/**/*.go"
description: "Go orchestrator conventions: zap logging, testify tests, deny-by-default security, TODO stub phases"
---

# Go Orchestrator

- Use `go.uber.org/zap` structured logging with fields: `logger.Info("msg", zap.String("key", val))`. Never `fmt.Sprintf` in log calls.
- Test with `github.com/stretchr/testify` (`assert`, `require`). CI runs with `-race -cover`.
- Orchestrator owns scheduling, registry, security, cost, telemetry. No LLM call logic here—that belongs in Python agents.
- Many `internal/` packages are TODO stubs for v0.2/v0.3. Implement the stub when its phase is active; don't remove the placeholder.
- gRPC services defined in `proto/task.proto` and `proto/agent_message.proto`. Run `make proto` after changing `.proto` files.
- Entry point flags: `--config` (dir), `--port` (gRPC 9090), `--http-port` (REST 8080), `--env` (development|staging|production).
