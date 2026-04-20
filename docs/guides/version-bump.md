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

The Go orchestrator version is set by the **git tag** — no file edit needed.

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
