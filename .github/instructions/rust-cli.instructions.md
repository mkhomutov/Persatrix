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
