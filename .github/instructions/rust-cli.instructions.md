---
applyTo: "cli/**/*.rs"
description: "Rust CLI conventions: clap v4 derive, exhaustive match, tokio async, thin client pattern"
---

# Rust CLI

- **Argument parsing:** `clap` v4 derive macros. See existing `Command` enum in `cli/src/main.rs`.
- **Exhaustive match:** No catch-all `_` on command enums. Adding a command must cause a compile error until all match arms are implemented.
- **Async runtime:** `tokio` with `#[tokio::main]`.
- **Thin client:** CLI is a REST client to the orchestrator at `--server` (default `http://localhost:8080`). All business logic lives server-side.
- **Output:** `tabled` for tables, `indicatif` for progress bars, `colored` for terminal colors.
- **YAML:** `serde_yml` (maintained successor to `serde_yaml`).

## TDD (from v0.3.0 onward)

- **Red-Green-Refactor:** Write a failing `#[test]` (or `#[tokio::test]`) before implementing the function. Confirm the red state with `cargo test -p persatrix-cli`.
- **Unit test placement:** Inline `#[cfg(test)] mod tests { ... }` at the bottom of the source file being tested.
- **Integration tests:** Place in `cli/tests/` as separate `.rs` files. These test CLI argument parsing and output formatting end-to-end without a live server (use a mock HTTP server or `mockito`).
- **HTTP calls:** Mock the orchestrator REST API in unit tests — do not make real network calls. Inject the base URL via the `--server` flag or a test helper that binds `mockito`.
- **New commands:** Before adding a `Command` variant, write at least one test that asserts the expected output format; implement until it passes.
