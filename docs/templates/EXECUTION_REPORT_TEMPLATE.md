# vX.Y.Z Manual Test Execution Report

**Version**: vX.Y.Z (<Codename> — <what shipped, by issue / RFC phase>)
**Report status**: ⬜ Pending — <flips to `✅ Complete — <verdict in one sentence>` when every row is Pass / Accepted-with-known-gap; zero Fail, zero Pending>
**Tester**: <name / agent + model> — <live Docker stack (provider + model) | host structural gates>
**Execution date**: YYYY-MM-DD
**Target commit**: `main` tip `<sha>` (<PR>) <plus any in-PR fix re-verified live>. This is PR 1 of `docs/vX.Y.Z-release-prep-plan.md`.

> **Environment note.** Structural gates ran on host against the RC tip working
> tree; the arc ran on a freshly `make reset` Docker stack rebuilt at the RC tip
> against a real provider. All results are from real runs — none inferred.

> Guidance: every cell starts as Pending and none may be inferred. Evidence is
> pasted verbatim. Run knobs are listed and reverted before commit. Cost is
> read from the reconciled-charge log line. This file is frozen after the tag
> and exempt from the word cap.

---

## Scope

Per `docs/vX.Y.Z-release-prep-plan.md` §PR 1: <the arc, the substrate re-runs,
the eval replay, the offline smoke>.

## Environment

| Component | Version |
|-----------|---------|
| OS | |
| Go / Python / Rust / Node | |
| Docker | `make reset` before the arc; images rebuilt at the RC tip |
| Provider | <vendor + model> (arc); mock (offline smoke) |

**Run knobs** (temporary, reverted before commit): <e.g. `memory.interaction_idle_timeout_sec: 45`; why each is needed and what shape the legs take because of it>.

**Preflight** (`scripts/manual_tests/<preflight>.py`): <one row per gate — PASS / FAIL / SKIP with what it protects>.

---

## Structural / Automated Gates (host)

| Gate | Command | Result |
|------|---------|--------|
| Golden replay | `make eval-replay PYTHON=.venv/bin/python` | ⬜ |
| Rust suite | `cd cli && cargo test` | ⬜ |
| Go suites | `go test ./internal/... -race` | ⬜ |
| <this release's named suites> | | ⬜ |
| Config validation | `make validate` | ⬜ |

---

## <MT ID> — <title> (live)

**Result**: ⬜

### Evidence obligation 1 — <name>

<The artifact, verbatim, in a fenced block: table / triples / counts. State
what a green leg *without* this artifact would have proven instead.>

### Legs

| Leg | What it proves | Result | Evidence |
|-----|----------------|--------|----------|
| 0 | <bootstrap / preconditions> | ⬜ | |
| 1 | | ⬜ | |
| … | | | |

**Vacuity check**: <for each absence bar, the positive control that shows the
surface was exercised>.

**Cost**: <USD, leases> read from `provisional charge reconciled`.

---

## Offline smoke — `make demo-autonomous` (mock provider, $0)

⬜ <convene → converge → terminate → synthesize; both close artifacts present>

## Carried-forward surface — regression

| MT | Result | Notes |
|----|--------|-------|

---

## Findings & follow-ups

| # | Severity | Finding | Disposition |
|---|----------|---------|-------------|
| F-1 | | | Fixed in-PR / in-release fix PR #n / ISSUE-NNNN / accepted-with-known-gap |
| P-1 | | <a prep finding — wrong recipe, stale checklist row> | <corrected where> |

## Issue dispositions

| Issue | Evidence | Status after this PR |
|-------|----------|----------------------|
| ISSUE-NNNN | Leg n | resolved / stays open (why) |

## Sign-off

<n>/<n> legs run live · <k> Pass · <a> Accepted-with-known-gap · 0 Fail · 0 Pending.

---

## Final Pre-Tag Verification (release-prep PR 4)

> Filled by PR 4 on the post-bump tip; left Pending until then.

### Automated gates
| Gate | Result |
|------|--------|
### Migration gate — populated pre-vX.Y.Z store pair
### Offline Docker smoke
