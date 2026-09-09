# Strategy Review — Evidence (September 2026)

> Companion to [Project Strategy Review](project-strategy-review-2026-09.md).
> This file holds the measurements; the argument that rests on them lives
> there. Everything below was measured against `main` at `f52e7ff6`
> (2026-09-09), the v0.3.16 Phase 1 tip.

---

## Method

Counts come from `git ls-files` over tracked files only, `git log` over the
full history, and `gh repo view` for the public signals. Word counts follow
[the documentation guide](documentation-guide.md#size-limits) — the checker,
not `wc -w`. The Go tree was confirmed to build, vet, and pass its fast test
packages before the review was written, so none of the findings below are
about a broken tree.

---

## 1. Scale

| Measure | Value |
|---------|-------|
| Tracked files | 2 152 |
| Production code | ~128 000 lines (Go 48 614 · Python 68 163 · Rust 10 909) |
| Test code | ~202 000 lines (Go 67 191 · Python 134 426) |
| Markdown | 134 322 lines across 564 files |
| RFC files | 129 |
| Issue files | 151 (111 resolved · 30 open · 1 in progress) |
| Commits | 891 |
| Releases tagged | 21 (v0.1.0 → v0.3.15) |
| Elapsed | 2026-04-08 → 2026-09-09 (5 months) |
| Contributors | 1 human, plus Dependabot |

Test code exceeds production code by roughly 1.6 : 1. Markdown alone is
larger than the Go production tree.

---

## 2. Distribution

Public repository, five months old, 21 tagged releases:

| Signal | Value |
|--------|-------|
| Stars | 4 |
| Forks | 0 |
| Watchers | 0 |
| External issues opened | 0 |
| External pull requests | 0 |
| External contributors | 0 |

Every non-bot issue in the tracker was filed by the author. Discussions are
enabled and empty.

---

## 3. Throughput over time

| Month | Commits | Lines added |
|-------|---------|-------------|
| 2026-04 | 247 | 176 598 |
| 2026-05 | 244 | 161 450 |
| 2026-06 | 228 | 120 251 |
| 2026-07 | 78 | 66 195 |
| 2026-08 | 52 | 28 640 |
| 2026-09 (partial) | 42 | 27 759 |

Mean lines per commit stays in the 540–720 band across the whole window, so
the decline is fewer changes landing, not larger batches.

Release cadence over the same period:

| Release | Commits | Days |
|---------|---------|------|
| v0.3.0 | 136 | 18 |
| v0.3.8 | 98 | 10 |
| v0.3.11 | 55 | 24 |
| v0.3.13 | 13 | 3 |
| v0.3.14 | 20 | 14 |
| v0.3.15 | 31 | 18 |

---

## 4. Process-to-feature ratio

Conventional-commit types across v0.3.12 → v0.3.15 — three complete release
cycles, 64 commits:

| Type | Commits |
|------|---------|
| `docs(release)` | 18 |
| all other `docs` / `chore` / `ci` | ~17 |
| `feat(channels)` / `feat(memory)` / `feat(server)` | 10 |
| `fix(*)` | 8 |

Roughly 3.5 process or documentation commits for every feature commit.

The v0.3.13 cycle is the clearest single instance: nine pull requests, of
which three carried code. The other six were the master plan, release-prep
PRs 0–4, and the post-release follow-up.

Cycle shape is defined in
[the release cycle](methodology/release-cycle.md): a sequencing amendment
and planning-readiness audit before Phase 0, a master plan PR, the
implementation PRs, a release-prep plan PR, release-prep PRs 1–4, the tag,
and a post-release follow-up carrying a debt sweep — about seven
non-implementation pull requests per release.

Commit-type mix over the last three months, for comparison: 110 `feat`,
107 `docs`, 38 `fix`, 27 `chore`, 7 `test`, 3 `refactor`, 1 `perf`, 1 `ci`.

---

## 5. Stub inventory

Packages that appear in the architecture diagram and the roadmap component
tables but contain only comments:

```
internal/mcp/mcp.go              7 lines   all TODO
internal/a2a/a2a.go              6 lines   all TODO
internal/protocols/protocols.go  7 lines   all TODO
internal/mesh/mesh.go            8 lines   all TODO
internal/bridges/bridges.go     10 lines   all TODO
internal/resilience/            24 lines   all TODO (package doc + TODOs)
agents/tools/mcp_bridge.py      12 lines   all TODO
```

MCP is unimplemented on both sides of the gRPC boundary. The four builtin
tools in `agents/tools/builtin.py` are the entire tool surface. Two workflow
definitions ship in `workflows/`; four blueprints ship as YAML only.

`TODO(v0.2)` markers remain live in `internal/server/`, `internal/executor/`,
`internal/cost/`, `internal/scheduler/` and `internal/state/` — carried
through fourteen minor releases.

---

## 6. Size-cap pressure

Distribution of `.go` and `.py` file lengths against the 500-line cap:

| Band | Files |
|------|-------|
| under 100 | 275 |
| 100–249 | 552 |
| 250–399 | 290 |
| 400–479 (79-line window) | 159 |
| **480–500 (21-line window)** | **88** |
| over 500 (allowlisted) | 2 |

A 21-line window holds 55 % as many files as the 79-line window beneath it.
That is clustering against the ceiling, not a natural tail.
[ISSUE-0143](issues/ISSUE-0143-debt-sweep-26-files-at-size-cap.md) records
26 files at exactly the cap, one of which blocks a test that
[ISSUE-0137](issues/ISSUE-0137-episode-write-boundary-cannot-express-the-records-principal.md)
needs.

---

## 7. Polyglot and toolchain cost

Costs attributable to the three-language split, each independently
observable:

- Proto staleness gates for Go and Python stubs, against a CI-pinned
  toolchain (protoc 34.1, protoc-gen-go 1.36.11, protoc-gen-go-grpc 1.6.1).
- Hand-maintained sanitizer enum mirrors kept in sync Go↔Python by a
  dedicated CI step.
- Three lint and type stacks, plus `deny.toml` and a 37 000-line
  `THIRD_PARTY_NOTICES.md`.
- [ISSUE-0142](issues/ISSUE-0142-ci-never-runs-golangci-lint.md) — CI runs
  no Go linter at all, and no `golangci-lint` version or config is pinned.
- [ISSUE-0144](issues/ISSUE-0144-anthropic-sdk-pinned-below-1x.md) — the
  Anthropic SDK is held below 1.x with nothing scheduled to move it.
- [ISSUE-0145](issues/ISSUE-0145-proto-toolchain-pinned-to-protobuf-5x.md) —
  the Python protobuf toolchain is frozen at 5.x.

The Rust CLI is 10 909 lines. The specification describes it as a thin REST
client with all business logic server-side.

---

## 8. The deferred extraction

[RFC 0046](rfcs/0046-budget-lease-extraction.md) was written 2026-05-25. Its
own motivation headings read *"The flagship funnel asset is locked in
BUSL"* and *"It is a library, not a framework — and a single-language one"*.
It proposes an MIT Python package with adapters for external agent
frameworks.

Status at the time of this review: `proposed`, target `v0.4.0+`, gated on
[RFC 0045](rfcs/0045-open-core-extraction-policy.md) acceptance. Three and a
half months without movement.
