# Document Templates

One template per document kind the [release cycle](../methodology/release-cycle.md)
produces. Copy the file, replace every `<placeholder>`, delete the guidance
blockquotes (`> Guidance: …`), and keep the section order — later readers and
the checks rely on it.

| Template | Produces | When |
|----------|----------|------|
| `VERSION_PLAN_TEMPLATE.md` | `docs/vX.Y.Z-plan.md` — the release's one document: the previous release's follow-up, scope locks, acceptance, PRs, release checklist | Phase 0 |
| `PLAN_AMENDMENT_TEMPLATE.md` | A dated section in a sequencing doc, an RFC or a version plan | Whenever a ratified decision changes |
| `PR_PLAN_TEMPLATE.md` | `docs/rfcs/NNNN-pr-plan.md` or `docs/issues/ISSUE-NNNN-…-pr-plan.md` | RFC Phase 3, or an issue-owned workstream |
| `EXECUTION_REPORT_TEMPLATE.md` | `docs/manual-tests/vX.Y.Z-execution-report.md` | The last implementation PR; the tag PR fills its final verification |
| `MANUAL_TEST_TEMPLATE.md` | `docs/manual-tests/MT-<AREA>-<NNN>.md` | Any time |

RFCs have their own template at [`docs/rfcs/RFC_TEMPLATE.md`](../rfcs/RFC_TEMPLATE.md)
and issues at [`docs/issues/ISSUE-TEMPLATE.md`](../issues/ISSUE-TEMPLATE.md).

`make release-doc KIND=plan|execution-report VERSION=X.Y.Z CODENAME="…"` copies
a template to its versioned path with `vX.Y.Z`, `<Codename>`, the previous
version and the date filled in and the guidance blockquotes removed
(`scripts/release/open_doc.py`).

Until v0.3.16 a release also had a scope-locks record, a release-prep plan, a
release baseline, a release checklist and a post-release follow-up PR body,
each from its own template. Ruling (e) of the sequencing Amendment 2026-09-12
folded them into the version plan, and the five templates were removed; the
released documents built from them stay in place as evidence.

## Conventions the templates assume

- **Links in the produced document are relative** and are checked by
  `doc_links.py`; the templates write paths in backticks so the templates
  themselves stay link-clean.
- **Status markers** come from the [documentation guide](../documentation-guide.md#status-markers);
  progress legends inside a table use ⬜ · 🔄 · 🔀 · ✅ · ✂️ Cut.
- **Word cap** is 3 000 words. A version plan holds its release under it: a
  release that does not fit is cut or re-scoped by amendment, never split into
  a second file, and `make plan-status-check` fails one.
- **Every claim carries a citation**: a PR number, a commit, a path, or a
  section anchor. "Green" without an artifact is not a result.
