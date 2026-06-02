# Version Bump Checklist

Use this checklist when preparing a new Persatrix release. All steps are required unless marked optional.

## Automated Step

Run the version bump script:

```bash
# Preview changes
make bump-version VERSION=X.Y.Z DRY_RUN=--dry-run

# Apply changes
make bump-version VERSION=X.Y.Z
```

This updates:

| File | Field |
|------|-------|
| `cli/Cargo.toml` | `version = "X.Y.Z"` |
| `agents/pyproject.toml` | `version = "X.Y.Z"` |
| `agents/observability/metrics.py` | `_DEFAULT_SERVICE_VERSION = "X.Y.Z"` |
| `agents/observability/tracing.py` | `_DEFAULT_SERVICE_VERSION = "X.Y.Z"` (+ docstring default) |
| `internal/server/ui_handlers.go` | `const defaultServiceVersion = "X.Y.Z"` |

A proper release build stamps the Go orchestrator's binary version from the
**git tag** (`go install`/`ldflags`). The `ui_handlers.go` constant above is only
the **fallback** the web console reports when that stamp and the
`PERSATRIX_SERVICE_VERSION` env var are both absent (a plain `go build`/Docker
image), so it must be bumped in lockstep — same posture as the Python
`_DEFAULT_SERVICE_VERSION` defaults.

## Manual Steps

After running the script, complete these steps in order:

- [ ] **Regenerate Cargo.lock**: `cd cli && cargo update --workspace`
- [ ] **Verify builds**: `make all` (proto + build all components)
- [ ] **Run tests**: `make test`
- [ ] **Run linters**: `make lint`
- [ ] **Update CHANGELOG.md**: `git-cliff --tag vX.Y.Z --unreleased --prepend CHANGELOG.md`
- [ ] **Review changelog** — ensure curated content is preserved, not overwritten
- [ ] **Check release checklist** — execute the relevant `docs/vX.Y-release-checklist.md`
- [ ] **Commit version bump**: `git add -A && git commit -m "chore: bump version to X.Y.Z"`
- [ ] **Tag release**: `git tag -a vX.Y.Z -m "vX.Y.Z — <release name>"`
- [ ] **Push**: `git push origin main --tags`

## Adding a New Versioned File

If a new component is added that carries its own version string:

1. Add an entry to `VERSION_FILES` in `scripts/bump_version.py`
2. Add a row to the table in this document
3. Add a row to the Version Alignment section of the release checklist template

## Notes

- The version script validates semver format (`X.Y.Z` or `X.Y.Z-prerelease`).
- Use `--dry-run` to preview changes before writing.
- Historical release checklists (`docs/v0.1-release-checklist.md`, etc.) reference old versions — those are records and should **not** be updated retroactively.
