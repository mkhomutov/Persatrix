---
applyTo: "internal/**/*.go,cmd/**/*.go"
description: "Go orchestrator conventions: zap logging, testify tests, deny-by-default security, TODO stub phases"
---

# Go Orchestrator

- Use `go.uber.org/zap` structured logging with fields: `logger.Info("msg", zap.String("key", val))`. Never `fmt.Sprintf` in log calls.
- Test with `github.com/stretchr/testify` (`assert`, `require`). CI runs with `-race -cover`.
- Orchestrator owns scheduling, registry, security, cost, telemetry. No LLM call logic here—that belongs in Python agents.
- Many `internal/` packages are TODO stubs for v0.2/v0.3. Implement the stub when its phase is active; don't remove the placeholder.
- gRPC services defined in `proto/task.proto`. Run `make proto` after changing `.proto` files.
- Entry point flags: `--config` (dir), `--port` (gRPC 9090), `--http-port` (REST 8080), `--env` (development|staging|production).
- **Comments in plain English.** Write comments a non-programmer could follow — say what the code does and why it matters, briefly. Full rules: [Documentation Guide § Writing Style](../../docs/documentation-guide.md#writing-style).

## TDD (from v0.3.0 onward)

- **Red-Green-Refactor:** Write a failing `_test.go` file before writing the implementation. Commit the failing test separately if it clarifies intent.
- **Test file placement:** `internal/foo/bar_test.go` alongside `bar.go`. Use `package foo_test` (black-box) unless you must access unexported symbols.
- **Stub implementation:** When activating a previously-stubbed package, write the test against the expected interface first, confirm it fails (`go test ./internal/... -run TestFoo`), then implement. Replace the stub body in-place — do not delete and recreate the package (preserves the placeholder convention from the bullet above).
- **Table-driven tests:** Prefer `[]struct{ name, input, want }` table tests for functions with multiple input cases.
- **Mocks:** Use interface mocks in `internal/testutil/` (create the package on first use) rather than concrete types. Do not add real network/gRPC calls in unit tests.
- **Integration tests** (`tests/integration/`) are exempt from strict TDD — write them to validate assembled pieces after unit tests pass.
