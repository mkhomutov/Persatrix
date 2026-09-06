# Document Templates

One template per document kind the [release cycle](../methodology/release-cycle.md)
produces. Copy the file, replace every `<placeholder>`, delete the guidance
blockquotes (`> Guidance: …`), and keep the section order — later readers and
the checks rely on it.

| Template | Produces | When |
|----------|----------|------|
| `VERSION_PLAN_TEMPLATE.md` | `docs/vX.Y.Z-plan.md` | Phase 0 |
| `SCOPE_LOCKS_TEMPLATE.md` | `docs/vX.Y.Z-scope-locks.md` | Phase 0, or when the plan nears the word cap |
| `PLAN_AMENDMENT_TEMPLATE.md` | `docs/vX.Y.Z-plan-amendment-YYYY-MM-DD.md`, or a dated section in a sequencing doc / RFC | Whenever a ratified decision changes |
| `PR_PLAN_TEMPLATE.md` | `docs/rfcs/NNNN-pr-plan.md` or `docs/issues/ISSUE-NNNN-…-pr-plan.md` | RFC Phase 3, or an issue-owned workstream |
| `RELEASE_PREP_PLAN_TEMPLATE.md` | `docs/vX.Y.Z-release-prep-plan.md` | Phase 2 (release-prep PR 0) |
| `EXECUTION_REPORT_TEMPLATE.md` | `docs/manual-tests/vX.Y.Z-execution-report.md` | Release-prep PR 1 |
| `RELEASE_CHECKLIST_TEMPLATE.md` | `docs/vX.Y.Z-release-checklist.md` | Release-prep PR 2 |
| `POST_RELEASE_FOLLOWUP_TEMPLATE.md` | The Phase 4 PR body | After the tag |
| `MANUAL_TEST_TEMPLATE.md` | `docs/manual-tests/MT-<AREA>-<NNN>.md` | Any time |

RFCs have their own template at [`docs/rfcs/RFC_TEMPLATE.md`](../rfcs/RFC_TEMPLATE.md)
and issues at [`docs/issues/ISSUE-TEMPLATE.md`](../issues/ISSUE-TEMPLATE.md).

## Conventions the templates assume

- **Links in the produced document are relative** and are checked by
  `doc_links.py`; the templates write paths in backticks so the templates
  themselves stay link-clean.
- **Status markers** come from the [documentation guide](../documentation-guide.md#status-markers);
  progress legends inside a table use ⬜ · 🔄 · 🔀 · ✅ · ✂️ Cut.
- **Word cap** is 3 000 words. A plan that nears it splits its stable half
  (locks, baseline) into the sibling document the table names — it does not
  trim.
- **Every claim carries a citation**: a PR number, a commit, a path, or a
  section anchor. "Green" without an artifact is not a result.
