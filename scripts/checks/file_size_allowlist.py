#!/usr/bin/env python3
"""Grandfather allowlist for the file-size audit (data, not logic).

This module holds *only* the ``GRANDFATHERED_FILES`` frozenset consumed by
``scripts/checks/file_size.py``. It is split out for one structural reason:
the allowlist is **reference data whose length scales with release history**,
not authored logic, so the list grows and would eventually push the *checker
itself* over its own 500-line code cap.

**Scope (narrowed 2026-07-25).** The two write-once release-evidence
categories — manual-test execution reports and release checklists — are no
longer listed here; they are excluded by pattern in ``file_size.py``
(``_EXTRA_EXCLUDES``). Those files are frozen against a tag and can never
shrink back under the cap, so each release was adding an entry whose stated
exit condition ("archive once the tag ships") was unachievable — 19 had
accumulated across v0.3.0–v0.3.10 and none was ever retired. Do not add new
ones back; add the *pattern* if a genuinely new write-once category appears.

**Scope (narrowed again 2026-09-06, ISSUE-0139).** Version-cycle documents
of *released* versions — master plans, scope locks, plan amendments,
release-prep plans, release baselines — are also gone from here. They are
frozen at the post-release follow-up, so ``file_size.py`` now excludes them
once ``CHANGELOG.md`` carries the version's dated release heading
(``_is_released_version_doc``; read from the tree, not ``git tag``, so a
depth-1 CI checkout answers the same as a full clone).
Sixteen entries whose exit condition ("remove once archived") nothing could
execute were retired in one move; ``#838`` had recorded the first failed
attempt.

What remains here is the honest case for an allowlist: files that are still
edited, where the cap continues to do useful work and the entry really is
expected to go away — the OPEN cycle's plan (its entry now expires by itself
at the tag), living specs/guides (removable once a topic split lands), and
long-form RFC PR plans (removable at RFC seal).

Separating the data from the logic keeps ``file_size.py`` honestly under the
code-line cap (it measures complexity, which should not grow just because the
allowlist does), and lets this file grow without bound. For that reason this
module is itself **excluded** from the size scan in ``file_size.py``
(``_EXTRA_EXCLUDES``) — the same "size scales with data, not prose" exemption
already granted to ``THIRD_PARTY_NOTICES.md`` and ``docs/issues/INDEX.md``.

Add or remove entries here, with the same inline justification convention used
throughout: each entry carries a comment saying why it is grandfathered and
when it should be removed (it should *shrink* back under its threshold, not
grow).
"""

from __future__ import annotations

# Files that already exceeded the size limit when the CI guard was introduced
# (v0.2 release prep, PR 13). New files must stay under the limit. These
# entries are tracked for targeted follow-up splits/trims and should shrink
# rather than grow; remove each once it falls back under its threshold.
GRANDFATHERED_FILES: frozenset[str] = frozenset({
    # Long-form reference docs. The 3000/8000-word limit targets typical
    # prose; these are enumerated planning and specification documents whose
    # length is inherent to their purpose.
    "ROADMAP.md",
    "docs/ai-agents-orchestration-spec.md",
    "docs/persatrix-extension-spec.md",
    # Version-cycle documents of RELEASED versions (master plans, release-prep
    # plans, scope locks, amendments, baselines) are no longer listed here:
    # file_size.py treats them as frozen release evidence once CHANGELOG.md
    # carries the version's dated heading (ISSUE-0139 — the archival mechanism
    # #838 found missing). Only the OPEN cycle's documents may appear below,
    # and each such entry expires on its own at the release.
    # docs/v0.3.x-sequencing.md is the LIVING sequencing record for the whole
    # v0.3.x line: the original 2026-05-10 decision is preserved verbatim and
    # every later scope decision lands as a dated amendment appended below it
    # (eight so far, through 2026-08-19). That "preserve original + append
    # amendment" shape is the load-bearing property — trimming an earlier body
    # to fit the cap would defeat the comparison each amendment depends on —
    # and, unlike a single version's plan, no single release ever freezes it,
    # so the released-version exclusion does not apply. Exit condition: when the
    # v0.3.x line closes (the v0.4.0 train opens a new sequencing doc) this
    # file freezes and the entry goes; or earlier if the amendments are split
    # into their own file.
    "docs/v0.3.x-sequencing.md",
    "docs/rfcs/0005-persona-agent-memory.md",
    "docs/rfcs/0005-pr-plan.md",
    "docs/rfcs/0006-pr-plan.md",
    # PR plan accumulates per-PR review residuals throughout the multi-PR
    # lifecycle (one review-findings subsection per merged PR). The plan
    # exited PR 3's review window with ~7 984 words; the PR 4 (RFC 0008
    # PR 4) review captures pushed it over the 8 000-word threshold.
    # Same rationale as `docs/rfcs/0019-pr-plan.md` below — trim/split is
    # more disruptive than informative on a plan that is still actively
    # accumulating per-PR follow-ups. Remove this entry once the
    # remaining 6-PR sequence completes and the plan is closed out.
    "docs/rfcs/0008-pr-plan.md",
    # PR plan accumulates per-PR review residuals throughout the multi-PR
    # lifecycle (one review-findings subsection per merged PR). The plan
    # exited PR 4's review window with ~7 980 words; the closeout PR 5
    # (RFC 0019 PR 5) tipped the file over the 8 000-word threshold while
    # appending the standard Disposition / Applied / Deferred sections.
    # Trim/split is more disruptive than informative on an already-merged
    # plan; remove this entry if a future maintenance PR splits the
    # per-PR review captures into a separate document.
    "docs/rfcs/0019-pr-plan.md",
    # Same per-PR review residual accumulator pattern as 0008-pr-plan.md
    # and 0019-pr-plan.md above. The plan exited PR 3's review window
    # with ~7 470 words; the PR 4 (RFC 0026 PR 4) deep-review captures
    # — phantom-reinforcement / TICK-cost / soft-slice overage findings,
    # each justified inline so the next reader sees the rationale
    # alongside the contract — pushed the doc over the 8 000-word
    # threshold. Trimming the per-PR review captures would defeat the
    # purpose of co-locating residuals with the plan; remove this
    # entry when the remaining 2-PR sequence (PR 5 / PR 6) closes out
    # and the plan is sealed at v0.3.1 release tag.
    "docs/rfcs/0026-pr-plan.md",
    # Same per-PR review residual accumulator pattern as 0008-pr-plan.md,
    # 0019-pr-plan.md, and 0026-pr-plan.md above. The plan exited PR 1's
    # first review window with ~7 983 words (1 word under the cap); the
    # PR 1 follow-up review captured a fifth deferred item — stop()
    # orphans pending ``InboundEventWake`` handles via the supervisor's
    # ``_stopped`` guard short-circuiting before the queue drains, plus
    # the same TOCTOU shape on ``enqueue`` racing ``stop()``. The item
    # text co-locates the symptom, fix sketch, and the pinning xfail
    # test name so the next reader sees the residual alongside the
    # remaining four. Trimming would defeat the co-location; same
    # disposition as the prior PR plans. Remove this entry once the
    # remaining 4-PR sequence (PR 2 / PR 3a / PR 3b / PR 4) plus the
    # review-follow-ups PR closes out and the plan is sealed at the
    # v0.3.3 release tag.
    "docs/rfcs/0024-pr-plan.md",
    # Same per-PR review residual accumulator pattern as 0008-pr-plan.md,
    # 0019-pr-plan.md, 0026-pr-plan.md, and 0024-pr-plan.md above. The plan
    # was at ~7 854 words after PR 3 merged; the PR #433 deep-review findings
    # table (cost-regression/$0 caps, ISSUE-0070 SystemExit footgun, two
    # in-PR doc fixes, templates info) pushed it to ~8 070 words. The finding
    # rows co-locate rationale with the plan so the next reader sees why each
    # item is deferred or fixed; trimming would erase that context. Remove
    # this entry once the remaining PR 4–7 sequence closes out and the plan
    # is sealed at the v0.3.4 release tag.
    "docs/rfcs/0033-pr-plan.md",
    # Same per-PR review residual accumulator pattern as 0008-pr-plan.md,
    # 0019-pr-plan.md, 0024-pr-plan.md, 0026-pr-plan.md, and 0033-pr-plan.md
    # above. The plan sat at ~7 989 words through PR 7c-ii-a; the PR 7c-ii-b
    # deep-review residuals pushed it to ~8 314 — the unwired
    # ``WalletService.EvictInteraction`` call site plus its missing
    # cross-process settle barrier (the standing leg's wallet footprint is a
    # tracked bounded leak, NOT the flat footprint the plan previously
    # asserted), and the convene-client init->wire window that silently drops a
    # first convene fire. Each residual co-locates the symptom, the
    # ground-truth code citation, and the fix sketch with the PR row it blocks,
    # so the next reader sees why the standing leg's bound is a leak rather
    # than a proof; trimming would erase exactly that context. Remove this
    # entry once the remaining PR 8–9 demo sequence closes out and the plan is
    # sealed at the v0.3.11 release tag.
    "docs/rfcs/0052-pr-plan.md",
    # Long-form architecture RFC (cf. docs/rfcs/0005-persona-agent-memory.md
    # above) that accumulates implementation amendments inline so each spec
    # section carries its as-built reconciliation: the ISSUE-0081 PR 2/3/4
    # session-model amendments, the scope-axes reframing (§A), and the Phase 3
    # operator-CLI closeout. The RFC exited the Phase 3 PR 4 window at ~7 995
    # words (just under the cap); the Phase 3 PR 5 closeout amendment (§E — all
    # three resolution mechanisms wired, the OQ #6 override-above-auto-binding
    # reconciliation, and the ISSUE-0086 `--all-sessions` carve-out) tipped it
    # to ~8 090. Trimming the closeout would split the operator-surface
    # contract from the spec it amends; reaching into unrelated amendments to
    # offset it trades fidelity for an arbitrary line. Remove this entry at RFC
    # seal (Phase 4 closeout) or if a maintenance PR moves the amendment
    # history into a separate changelog.
    "docs/rfcs/0031-per-session-namespacing-channels.md",
    # docs/observability.md tipped over the 3 000-word prose limit when
    # RFC 0009 PR 1c added the audit-logger metric inventory + SLO alert
    # templates to §13. The new content is already trimmed (a one-line
    # instrument list and three Prometheus alert blocks — code-fenced
    # YAML does not count toward the prose limit). The §13 expansion is
    # required by the PR #234 review Medium-1 finding (capability-fsync
    # amplification monitoring) and PR #233 review Nice-to-have #5;
    # splitting observability.md by topic is a separate maintenance
    # refactor. Grandfather here until that lands.
    "docs/observability.md",
    # CHANGELOG.md grows over a release cycle and is trimmed/archived at
    # each release tag by the git-cliff pipeline (see `cliff.toml` and
    # the release process in `docs/development-workflow.md`), so size
    # temporarily exceeds the prose limit during the active Unreleased
    # window.
    "CHANGELOG.md",
    # NOTE: per-release manual-test execution reports
    # (docs/manual-tests/v[0-9]*-execution-report.md) and release checklists
    # (docs/v[0-9]*-release-checklist.md) are no longer enumerated here. Both are
    # write-once release evidence that can never shrink back under the cap, so
    # each release added an entry whose "archive once the tag ships" exit
    # condition was never achievable; 19 such entries (v0.3.0–v0.3.10) had
    # accumulated and none was ever retired. They are now excluded by pattern
    # in scripts/checks/file_size.py (_EXTRA_EXCLUDES), which is also why new
    # ones must NOT be added back here. Master plans and release-prep plans of
    # RELEASED versions are likewise excluded, by their dated CHANGELOG heading
    # (_is_released_version_doc, ISSUE-0139); only the open cycle's plan may
    # still need an entry.
    # docs/manual-tests/MT-MEMORY-005-dementia-test.md — the qualitative
    # memory acceptance gate; gains a Test Results row every memory-touching
    # release (v0.3.1 ×2, v0.3.5 ×2), so it sits at the 3 000-word prose cap
    # as a permanent release-cycle accumulator. Trim only if a row is dropped.
    "docs/manual-tests/MT-MEMORY-005-dementia-test.md",
    # docs/manual-tests/MT-CHANNEL-GOV-004.md — the live chair-stall-escalation
    # acceptance gate (RFC 0030 §C). Like MT-MEMORY-005 above it is a permanent
    # Test Results accumulator: each governance build (ISSUE-0096/0097/0098/0099
    # verification) folds in a detailed evidence row, so it sits at the 3 000-word
    # prose cap. The ISSUE-0099 PR-2 row tipped it over. Trim only if a row is dropped.
    "docs/manual-tests/MT-CHANNEL-GOV-004.md",
    # docs/manual-tests/MT-MEMORY-CROSSROOM-001.md — the live cross-room memory
    # acceptance gate (RFC 0049 scenario 2), same class as the two above. It
    # accumulates by *tier* rather than by results row: v1.1 added the person-
    # identity legs (1b/2b) after ISSUE-0119 reached a release candidate on a
    # green v1.0 run, because a facts-tier leg names a topic and structurally
    # cannot observe an identity-tier break. Each further memory tier that
    # ships lands the same way. The v1.1 edit sat at exactly 3 000/3 000 after
    # three prose trims — the point at which shaving load-bearing procedure to
    # fit the cap costs more than the cap saves, in a doc whose job is to be
    # followed step-by-step under live timing pressure. Trim only if a leg is
    # dropped.
    "docs/manual-tests/MT-MEMORY-CROSSROOM-001.md",
    # docs/guides/persona-agents.md was at 2 867 words on the v0.3.0
    # release-candidate tip; release-prep PR 2 added three §2 callouts
    # (interactions-not-messages per RFC 0020, now-anchor per RFC 0021,
    # and a new §6 listing the externally inspectable persona prompt
    # sections per RFC 0022). The new content is already trimmed (each
    # callout is one paragraph; §6 is a one-row-per-section table). A
    # future maintenance PR can split the chat (§4) and observability
    # (§5) subsections into the chat-specific guide once it exists, but
    # that is a separate refactor. Grandfather here until that lands.
    "docs/guides/persona-agents.md",
    # docs/guides/channels.md was at 2 999 words (1 word under the cap — the
    # same knife-edge as docs/ai-glossary.md below) when the v0.3.7 RFC 0030
    # relevance Tier A closeout (PR 3) had to document the new per-membership
    # `respond` disposition vocabulary (participant/addressed/observer) + its
    # legacy back-compat mapping + the Tier A-vs-Tier B scope note in the
    # operator-facing "§Per-membership respond dispositions" section. The new
    # content is already trimmed (terse bullets + a single combined blockquote).
    # A future maintenance PR can split the floor-control (§7) / memory (§5)
    # subsections into their own pages; grandfather here until that lands.
    "docs/guides/channels.md",
    # docs/ai-glossary.md was at 2 999 words (1 word under the cap) when
    # RFC 0020 PR 4 (PR #229) landed. The PR #229 review Should-Fix #5
    # required adding the canonical PR-4 terminology (closing-state
    # interaction, summary-pending sentinel, summary-unavailable
    # sentinel, interaction janitor) to the glossary per the project's
    # own term-policy in `.github/copilot-instructions.md`. The new
    # section is already trimmed to ~150 words (one definition per
    # term, no aliases/examples sections). Splitting the glossary by
    # topic is a separate maintenance refactor; grandfather here until
    # that lands.
    "docs/ai-glossary.md",
    # docs/storage-architecture-roadmap.md is the long-form planning doc the
    # SA-1..SA-10 storage items live in. It sat right at the 3000-word prose
    # cap and crossed it when RFC 0029 Phase 1 PR 5 (the Phase 1 closeout)
    # flipped the SA-1 row — vehicle "new RFC" → RFC 0029, target + status
    # updated to record the v0.3.2 `MemoryStore` facade landing. Same
    # status-flip-tips-a-tracking-doc pattern the version plans followed
    # (a status change, not new prose, pushed it over the cap);
    # trimming the SA-1..SA-10 narrative would erase planning context and a
    # topic split is a separate docs refactor. Remove this entry once that
    # split lands.
    "docs/storage-architecture-roadmap.md",
    # agents/memory/facade.py, agents/memory/episodic.py, and
    # agents/persona_runtime/memory_context.py were grandfathered above
    # the 500-line cap; their splits landed in this PR.  facade.py was
    # already under-cap once the procedural mixin (``facade_procedural``)
    # absorbed RFC 0008 PR 5 follow-ups; episodic.py dropped below the
    # cap when the notes-tier delegates moved into
    # ``episodic_notes_api._EpisodicNotesAPIMixin``; memory_context.py
    # dropped below the cap when the relationship-tier admission block
    # moved into ``relationship_section`` (parallel to the
    # ``channel_history`` extraction precedent).
})
