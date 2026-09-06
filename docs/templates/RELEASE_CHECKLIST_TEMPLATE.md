# vX.Y.Z Release Checklist

> **Status**: 🔄 Release prep — <flips to `✅ Released YYYY-MM-DD — tag vX.Y.Z at <sha>, GitHub Release published` at the post-release follow-up>. Every §1–§4 gate must pass on the post-bump tip (release-prep PR 4). §3.1 is the canonical Upgrade Notes source PR 3 reconciles into `CHANGELOG.md`. §4 cites the MT execution report landed by PR 1.

> vX.Y.Z — "<Codename>" — <one paragraph: the story, what it closes, what rides along, what was taken-not-cut>.

**Based on test report**: `docs/manual-tests/vX.Y.Z-execution-report.md`

> Guidance: copy the previous release's checklist and the release baseline's
> "differs from last release" list side by side. A row copied forward that the
> baseline contradicts is wrong, not stale. Enumerate every test target; never
> let `make test` stand for the whole sweep.

---

## 1. Pre-release Verification

Every gate as a command, run on a clean checkout of the post-bump tip
(`make release-sweep RUN=1 REPORT=/tmp/sweep.md` runs the list below and
prints the results table; `OPTIONAL=1` adds the Docker smoke):

- [ ] `make test` — all four legs (`test-go`, `test-python`, `test-agents`, `test-integration`)
- [ ] `cd cli && cargo test` — incl. the CLI↔server lockstep guards
- [ ] `make lint` — golangci-lint, ruff + mypy (agents/, tests/, evaluators/), `imports-check`, clippy
- [ ] `mypy tests/` (the separate leg)
- [ ] `make validate`
- [ ] `make proto && git diff --exit-code` · `make proto-check`
- [ ] `make generate-sanitizer-patterns-check`
- [ ] `make ui` + `make ui-test` + `make ui-html-check` (restore `internal/ui/assets/index.html` after)
- [ ] `make eval-replay` on the post-bump tip — <n>/<n> recipes
- [ ] `make check-licenses` · `make notices` (<delta expected: yes/no>)
- [ ] `python scripts/checks/file_size.py --strict` · doc gates (`doc_links`, `doc_status_markers`, `doc_leaked_markup`, FILEMAP `--check`)
- [ ] This release's named suites: <list them>
- [ ] Offline Docker smoke: `make demo-autonomous` at $0

### 1.1 Live-run guidance (for any re-run of the arc)

<Run knobs, pacing, preflight, what the operator must not do mid-arc.>

## 2. Version Alignment

| File | Field | Value |
|------|-------|-------|
| `cli/Cargo.toml` | `version` | X.Y.Z |
| `agents/pyproject.toml` | `version` | X.Y.Z |
| `agents/observability/metrics.py` | `_DEFAULT_SERVICE_VERSION` | X.Y.Z |
| `agents/observability/tracing.py` | `_DEFAULT_SERVICE_VERSION` | X.Y.Z |
| `internal/server/ui_handlers.go` | `defaultServiceVersion` | X.Y.Z |
| `cli/Cargo.lock` | regenerated | `cargo update --workspace` |

## 3. Changelog

- [ ] `[Unreleased]` curated into a dated `[X.Y.Z] - YYYY-MM-DD` — one bullet per story, not per PR
- [ ] Prior sections untouched
- [ ] §3.1 reconciled into the Upgrade Notes subsection

### 3.1 Required Upgrade Notes

1. **Migrations** — <N>, by store: `<store> vA → vB` (<forward-only? repair shipping with its consumer?>). <Drop-in or downgrade caution.>
2. **<Coherence trade>** — <the behaviour change and its cost, stated>.
3. **<Metric / wire shape change>** — <what dashboards must re-check>.
4. **<What stays byte-identical>**.

## 4. Manual Test Sign-off

| MT | Legs | Result | Report anchor |
|----|------|--------|---------------|
| `<MT ID>` | 0–n | ⬜ | `docs/manual-tests/vX.Y.Z-execution-report.md#…` |

Zero Fail, zero Pending; every Accepted-with-known-gap row cites its issue.

## 5. Tag and GitHub Release Procedure

After release-prep PR 4 merges:

```bash
git checkout main && git pull
git tag -a vX.Y.Z -m "vX.Y.Z — <Codename>"
git push origin main --tags
```

- [ ] GitHub Release body = curated changelog + §3.1 + §6 + closing evidence from the PR 1 report; relative links re-rooted to absolute GitHub URLs
- [ ] Post-release follow-up PR (`docs/templates/POST_RELEASE_FOLLOWUP_TEMPLATE.md`)

## 6. Known Gaps to Document in Release Notes

| Gap | Owner | Bound |
|-----|-------|-------|
| <one line> | ISSUE-NNNN | <what keeps it bounded> |

## 7. Summary Checklist

- [ ] §1 all green on the post-bump tip (PR 4)
- [ ] §2 aligned (PR 3)
- [ ] §3 dated and curated (PR 3)
- [ ] §4 zero Fail / zero Pending (PR 1)
- [ ] §5 tag pushed, Release published (Phase 4)
- [ ] §6 stated in the Release body
- [ ] Statuses → Released everywhere (Phase 4)
