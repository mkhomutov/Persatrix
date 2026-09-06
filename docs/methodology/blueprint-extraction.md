# Blueprint Extraction — the plan

> **Last updated**: 2026-09-06
> How the methodology leaves this repository and comes back as a pinned
> dependency. This is a plan, not a record: the blueprint repository does
> not exist yet, and creating it is the owner's call.

## What history says about the direction

This repository's second commit
([#2](https://github.com/mkhomutov/Persatrix/pull/2), 2026-04-08) *imported*
a blueprint: the `scripts/checks` framework, the pre-commit hook and its
installer, the RFC process and template, the documentation guide, the
manual-test template, the commit-lint and audit workflows. The two have
diverged silently since — the blueprint never saw the file-size allowlist,
the generated indexes, the release cycle, or any of the 2026-09 methodology
series — because nothing connected them. A copy is a fork; the second
extraction must be an **upstream** with a pinned version and a conformance
check on this side, so drift is a red check rather than a re-audit.

## Generic and specific

The [conformance manifest](conformance.json) is the split, kept as data:

| Section | Travels to the blueprint | Notes |
|---|---|---|
| `documents` | yes, with project names replaced by placeholders | The nine methodology docs, the documentation guide, BRANCHING, CONTRIBUTING skeleton, the RFC / issue / manual-test conventions and templates, the ten document templates, the PR template, `dependabot.yml`, and the generated files' *shapes* (FILEMAP, merged-prs, the two indexes) |
| `tooling` | yes, unchanged | Everything under `scripts/checks/`, the generators, the hook, the release tooling. All stdlib-only and path-relative by design |
| `make_targets` | yes, as a `Makefile.methodology` include | A consumer's Makefile includes it and keeps its own build targets |
| `ci_jobs`, `ci_steps_in_docs_hygiene` | yes, as a reusable workflow | `Docs hygiene` and `File size check` become `uses: <blueprint>/.github/workflows/methodology.yml@vN` |
| `persatrix_specific` | no | Product glossary, the sequencing record, ROADMAP, the assistant instruction files. The blueprint ships *templates* for the first and last |

The rule for deciding a new file's side: if the text would be wrong in a
different product, it is specific; if only a name in it would change, it is
generic with a placeholder.

## Placeholders

The generic documents cite this repository by name, by PR number, and by
version. The extraction replaces:

| In Persatrix | In the blueprint |
|---|---|
| `Persatrix`, `mkhomutov/Persatrix` | `{{project}}`, `{{repo}}` |
| PR and issue citations (`#858`, `ISSUE-0139`) | kept as *examples* in a clearly marked "worked example" section, or dropped |
| Version names (`v0.3.15`, codenames) | `vX.Y.Z`, `<Codename>` — the templates already do this |
| The four languages and their gates (`cargo test`, `make ui-test`) | a `gates:` list in the consumer's `conformance.json`, rendered into the checklist template |
| The 500-line / 3 000-word caps | defaults in `file_size.py`, overridable by flag (already true) |

## The vendoring model (one-way)

1. The blueprint repository is the upstream. It carries `VERSION`, the
   manifest, the tooling, the generic documents, and a `CHANGELOG.md` of
   its own following the same release cycle in miniature.
2. A consumer pins a version in `docs/methodology/BLUEPRINT_VERSION` and
   vendors the tooling with `git subtree` (or a release tarball) into
   `scripts/` and `docs/templates/`. Local edits to vendored files are
   allowed and are the signal that the change belongs upstream.
3. `methodology_conformance.py` runs in the consumer's CI against the
   *blueprint's* manifest for the pinned version, plus the consumer's own
   additions. Missing artifacts fail the PR that removed them.
4. Upgrading is a PR that bumps the pin, re-vendors, and runs the check.
   The blueprint's changelog says what a version added; the diff says what
   the consumer had locally patched.

What does **not** flow: consumer documents never go upstream by copy. A
process improvement found in a consumer is proposed to the blueprint as an
RFC there, then arrives back through a version bump — the same discipline
this repository uses for its own product.

## Steps, in order

1. **Owner creates the repository** (empty, BUSL or MIT per RFC 0045's
   licence tiers — the tooling is a good MIT candidate and RFC 0045 §E's
   DCO rule applies there).
2. First PR there: `scripts/checks/`, `scripts/_doc_index.py`,
   `scripts/_git.py`, the generators, the hook and installer, the release
   tooling, the tests that pin them — unchanged — plus the manifest with
   `persatrix_specific` emptied.
3. Second PR: the generic documents with placeholders applied, the ten
   templates, `Makefile.methodology`, the reusable workflow.
4. Third PR, here: replace the vendored files with the pinned copy, add
   `BLUEPRINT_VERSION`, point the conformance check at both manifests. Tag
   the blueprint `v1.0.0` when this repository passes against it.
5. From then on, methodology changes start upstream.

## What the conformance check already gives us

Today the manifest and checker run here against this repository alone,
which is the half of the model that catches drift *inside* a consumer: a PR
that deletes a template, drops a make target, or leaves a check out of the
Docs hygiene job fails `make conformance-check`. The manifest names the
checker itself, so it cannot be dropped silently.

## Related documentation

- [README.md](README.md) — the methodology set
- [enforcement-matrix.md](enforcement-matrix.md) — which of its rules are checked
- [conformance.json](conformance.json) — the contract
- [RFC 0045](../rfcs/0045-open-core-extraction-policy.md) — licence tiers and the DCO rule for extracted repositories
