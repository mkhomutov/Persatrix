---
id: ISSUE-0232
summary: "Values that Go, Python, Rust and the web console must agree on are kept as hand-written copies, checked by tests that read the other language's source as text. Pinned values stay aligned and unpinned ones drift, and the REST API has four hand-written clients with a machine-checked contract for only three payload shapes."
status: open
severity: medium
area: repo
created: 2026-11-10
refs:
  - cmd/genpatterns
  - schemas/channel.schema.json
  - agents/channel_payload_contract.py
  - cli/src/commands/channel_types.rs
  - cli/src/commands/channel_config_tests.rs
  - web/src/lib/channelKnobs.js
  - internal/server/channel_config_merge.go
  - agents/defaults.py
  - internal/defaults/defaults.go
  - docs/rfcs/0040-agent-orchestrator-transport-unification.md
  - docs/issues/ISSUE-0209-participant-id-rule-differs-between-go-and-python.md
---

# ISSUE-0232: Cross-language agreement rests on hand-kept copies

## Summary

The same value often has to exist in two to four languages: a
validation pattern, the list of channel settings, a default, a wire
constant. With one exception these are separate hand-written copies.
What keeps them aligned is a set of tests that open the other
language's source file and search its text.

## Context

- **The exception.** Sanitizer patterns have one source and a generator,
  `cmd/genpatterns`, with a CI check that the generated copies are
  current.
- **The text-reading tests.** 22 Python unit-test files read `.go` or
  `.rs` source, the Rust tests read Go files
  (`channel_config_tests.rs`), and one web test does the same. They
  work, and they fail loudly when the text they look for is missing.
- **Where they fall short.** A value nobody pinned drifts unnoticed:
  [ISSUE-0209](ISSUE-0209-participant-id-rule-differs-between-go-and-python.md)
  is one, and `agents/defaults.py` against
  `internal/defaults/defaults.go` is covered only by a comment that the
  two are reviewed together. A pin can also go quiet when the file it
  reads is split; the review process document records a guard that read
  zero settings after such a split.
- **The REST surface.** About 40 routes are called by four hand-written
  clients: the agents, the CLI, the web console, and the evaluators and
  scripts. The JSON-schema contract covers three payload shapes
  (`schemas/channel.schema.json`), and the Go side does not validate
  against it. A comment in `channel_types.rs` says a new server field
  "requires a coordinated bump".
- [RFC 0040](../rfcs/0040-agent-orchestrator-transport-unification.md)
  is the plan for a typed contract. `agents/channel_payload_contract.py`
  is labelled as that RFC's Phase 1, yet the RFC still reads proposed,
  its PR plan shows PR 1 as not started, and its later phases have no
  release.

## Impact

Each new setting or wire value has to be added by hand in up to five
places, and the tests can only confirm the places someone remembered to
pin. The agent-to-orchestrator direction is REST and is the one that
grows in v0.4.0.

## Proposed fix / investigation path

- Bring RFC 0040's status up to date.
- Extend the generator approach of `cmd/genpatterns` to wire constants
  and validation patterns.
- Serve the channel-settings registry from the orchestrator, so the CLI
  and console stop carrying their own lists.
- Extend the schema contract to the agent-facing routes before v0.4.0
  adds more.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; the file counts were measured, the rest confirmed by
> reading.
