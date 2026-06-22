#!/usr/bin/env python3
"""Grandfather allowlist for the file-size audit (data, not logic).

This module holds *only* the ``GRANDFATHERED_FILES`` frozenset consumed by
``scripts/checks/file_size.py``. It is split out for one structural reason:
the allowlist is **reference data whose length scales with release history**,
not authored logic. Every release cycle adds an entry (a plan, a checklist, an
execution report) with an inline rationale, so the list grows monotonically
and would eventually push the *checker itself* over its own 500-line code cap.

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
    "docs/v0.2-release-prep-plan.md",
    # v0.3.0-plan.md is the active release plan; it accumulates MQ rows
    # and Memory Quality follow-ups during the v0.3.x release cycle (same
    # pattern as the PR plans below and as v0.2-release-prep-plan above).
    # PR 238 ratified the Memory Quality Roadmap and added MQ-1..MQ-9; the
    # PR 238 review pass added MQ-10..MQ-13 (SubjectErasure traversal,
    # per-turn provenance, cross-scope identity, within-interaction
    # pressure). These tracking rows are load-bearing for the v0.3.x
    # work and trimming the surrounding narrative would erase release-cycle
    # context. Remove this entry once v0.3.0 ships and the plan is archived.
    "docs/v0.3.0-plan.md",
    # docs/v0.3.1-plan.md is the v0.3.1 master plan — same release-cycle
    # accumulator pattern as docs/v0.3.0-plan.md above. It crossed the
    # 3000-word prose cap when the v0.3.1 post-release follow-up flipped the
    # Status / Completed header and rolled the release-prep and post-release
    # PRs into the Master Progress Overview. The RFC 0034 amendment was
    # already split out into docs/v0.3.1-plan-amendment-2026-05-15.md to
    # hold the line earlier in the cycle; trimming the remaining
    # release-cycle narrative would erase context. Remove this entry once
    # v0.3.1 is archived.
    "docs/v0.3.1-plan.md",
    # docs/v0.3.5-plan.md and docs/v0.3.6-plan.md are the active v0.3.5 /
    # v0.3.6 master plans — same release-cycle accumulator pattern as
    # docs/v0.3.0-plan.md / docs/v0.3.1-plan.md above. Each crossed the
    # 3000-word prose cap when a release-scope decision folded a new row in
    # (v0.3.5: the epoch axis / Phase 3b, per docs/rfcs/0031-epoch-pr-plan.md;
    # v0.3.6: the RFC 0030 floor-control channel blocker, per
    # docs/rfcs/0030-amendment-floor-control-pr-plan.md). The umbrellas hold
    # only release-level framing; per-PR detail lives in those dedicated
    # plans, and trimming the narrative would erase release-cycle context.
    # Remove each entry once its release ships and the plan is archived.
    "docs/v0.3.5-plan.md",
    "docs/v0.3.6-plan.md",
    "docs/v0.3.7-plan.md",  # v0.3.7 master plan — same accumulator pattern as v0.3.5/v0.3.6 above (3-workstream realism cut); remove once shipped/archived.
    "docs/v0.3.8-plan.md",  # v0.3.8 master plan — same accumulator pattern as v0.3.5/v0.3.6/v0.3.7 above (3-workstream convergence cut); remove once shipped/archived.
    "docs/v0.3.9-plan.md",  # v0.3.9 master plan — same accumulator pattern as v0.3.5–v0.3.8 above (2-RFC verbatim-recall cut: ledger substrate + recall consumer, with the §OQ-6 scope lock folded in); remove once shipped/archived.
    # docs/v0.3.x-sequencing.md orchestrates the v0.3.1 / v0.3.2 / v0.3.3
    # patch sequence and accumulates amendments as new v0.3.x-targeted
    # RFCs file (the 2026-05-12 amendment captured the RFC 0030 + RFC
    # 0031 landings and re-shuffled v0.3.1 / v0.3.2 scope). The original
    # 2026-05-10 ratified decision is preserved verbatim above the
    # amendment for context — that "preserve original + dated amendment"
    # framing is the load-bearing shape of the doc and trimming the
    # original body to fit the cap would defeat the comparison the
    # amendment depends on. Same release-cycle-accumulator pattern as
    # docs/v0.3.0-plan.md above. Remove this entry once v0.3.3 ships
    # and the doc is archived.
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
    # Per-release manual-test execution reports accumulate evidence across
    # the PR 1 (initial sweep) and PR 4 (final pre-tag verification)
    # release-prep passes — every test row carries inline command + output
    # snippets, per-leg notes, and §"Release-prep regressions fixed"
    # tables that grow the file past the 3 000-word prose cap. Same
    # release-cycle-accumulator pattern as `CHANGELOG.md` above: written
    # against a fixed release, archived once the tag ships. The v0.2.3
    # report (3 395 words at tag time) set the precedent; the v0.3.0 PR 4
    # rerun (this addition) brings the report to ~4 600 words.
    "docs/manual-tests/v0.3.0-execution-report.md",
    # docs/manual-tests/v0.3.1-execution-report.md — v0.3.1 sibling of the
    # v0.3.0 report above; same per-release accumulator pattern (~4 250 words
    # after the PR 1 live pass). Archive once the tag ships.
    "docs/manual-tests/v0.3.1-execution-report.md",
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
    # docs/manual-tests/v0.3.2-execution-report.md is the v0.3.2 sibling of
    # the v0.3.0 / v0.3.1 reports above — identical per-release accumulator
    # pattern. The release-prep PR 1 sweep (32 tests + wallet acquire+settle
    # p99 measurement) brought the report past the 3 000-word prose cap:
    # every test row carries inline outcome + evidence + the F-1 chat-REST
    # surface failure root-cause trace cross-linked to ISSUE-0065 (the only
    # ❌ Fail on this pass). Written against the v0.3.2 release; archive
    # once the tag ships.
    "docs/manual-tests/v0.3.2-execution-report.md",
    # docs/manual-tests/v0.3.3-execution-report.md is the v0.3.3 sibling of
    # the v0.3.0 / v0.3.1 / v0.3.2 reports above — identical per-release
    # accumulator pattern. The PR 1 sweep (34 rows + automated suites) plus
    # the release-prep PR 4 § Re-Execution section (full-suite rerun + the
    # four-behaviour live Docker smoke on the post-version-bump tip) bring
    # the report past the 3 000-word prose cap. Written against the v0.3.3
    # release; archive once the tag ships.
    "docs/manual-tests/v0.3.3-execution-report.md",
    # docs/manual-tests/v0.3.4-execution-report.md is the v0.3.4 sibling of
    # the v0.3.0–v0.3.3 reports above — identical per-release accumulator
    # pattern. The release-prep PR 1 sweep (38 rows = 4 new RFC 0033 MTs +
    # 34 carried-forward, plus the automated suites) carries inline per-step
    # evidence tables for the four new MTs (alias routing, the live one-line
    # provider swap with exact gpt-4o cost math, offline $0, Ollama real
    # tokens) and the §Follow-ups findings (F-5 OPENAI_API_KEY plumbing,
    # F-6 CPU-Ollama latency, F-7 1% tail-sampling), pushing it past the
    # 3 000-word prose cap. Written against the v0.3.4 release; archive once
    # the tag ships.
    "docs/manual-tests/v0.3.4-execution-report.md",
    # docs/manual-tests/v0.3.5-execution-report.md is the v0.3.5 sibling of
    # the v0.3.0–v0.3.4 reports above — identical per-release accumulator
    # pattern. The release-prep PR 1 sweep (3 new session/epoch MTs +
    # the carried-forward v0.3.4 surface) carries the automated-gate results
    # (20+5 integration / 893 unit / 153 Rust), inline per-step evidence
    # tables for MT-SESSION-002 / -003 / MT-EPOCH-001, the § Environment &
    # constraints rationale, and the §Follow-ups findings (F-1/F-2 scope/epoch
    # tagging, F-3/F-4 environment), past the cap. Archive once the tag ships.
    "docs/manual-tests/v0.3.5-execution-report.md",
    # v0.3.6 sibling — same per-release accumulator pattern; archive once tagged.
    "docs/manual-tests/v0.3.6-execution-report.md",
    # v0.3.7 sibling — same per-release accumulator pattern (the realism surface:
    # MT-CHANNEL-RELEVANCE-001 + MT-PERSONA-CONVERSATION-002 + the combined
    # walkthrough + the carried-forward v0.3.6 surface + the structural-gate
    # tables), and it carries the live root-cause analysis of the @everyone
    # broadcast defect (ISSUE-0094) past the cap. Archive once the tag ships.
    "docs/manual-tests/v0.3.7-execution-report.md",
    # v0.3.8 sibling — same per-release accumulator pattern (the convergence
    # surface: MT-CHANNEL-RELEVANCE-002 + MT-CHANNEL-GOV-003/-004 +
    # MT-INTERACTION-SUMMARY-001 + MT-CHANNEL-CONFIG-001…004 + the combined
    # convergence walkthrough + the structural-gate tables). The PR 4 final
    # pre-tag verification appended its live gate table + the two-store
    # migration upgrade-on-open verification + the Docker-smoke carry-forward,
    # tipping the report past the 3 000-word cap (~4 280 words). Archive once
    # the tag ships.
    "docs/manual-tests/v0.3.8-execution-report.md",
    # docs/v0.3.3-release-checklist.md crossed the 3 000-word prose cap as a
    # release-cycle record: the §3.1 Upgrade Notes table (8 rows — event-driven
    # loop, fire-and-forget channel dispatch, autonomy.timers, scheduled_wakes
    # cache, salience knobs, wake counters, the vestigial §F guard, and the
    # breaking MemoryFacade alias removal) and the §6 Known Gaps inventory are
    # inherently longer for this feature-rich release than the v0.3.2 sibling
    # (which fit at ~2 900 words); the PR 4 gate evidence is already condensed,
    # with full detail deferred to the grandfathered execution report. Written
    # against the v0.3.3 release; archive once the tag ships.
    "docs/v0.3.3-release-checklist.md",
    # docs/v0.3.4-release-checklist.md — v0.3.4 sibling of the v0.3.3 checklist
    # above; identical release-cycle-record pattern. Crossed the cap in
    # release-prep PR 4 when §1 gate boxes were filled with post-bump re-cert
    # evidence over the §3.1 Upgrade Notes (7 rows) + §6 Known Gaps. Archive at tag.
    "docs/v0.3.4-release-checklist.md",
    # docs/v0.3.5-release-checklist.md — v0.3.5 sibling of the v0.3.3 / v0.3.4
    # checklists above; identical release-cycle-record pattern. Crossed the cap
    # in release-prep PR 4 when §1/§2/§7 were filled with post-bump re-cert
    # evidence (+ the two §4 live hard-block legs) over the §3.1 Upgrade Notes +
    # §6 Known Gaps. Archive once the tag ships.
    "docs/v0.3.5-release-checklist.md",
    # docs/v0.3.6-release-checklist.md — v0.3.6 sibling of the v0.3.3–v0.3.5
    # checklists above; same release-cycle-record pattern. Crossed the cap in
    # release-prep PR 4 when §1/§2/§7 were filled with post-bump re-cert evidence
    # over the §3.1 Upgrade Notes + §6 Known Gaps. Archive once the tag ships.
    "docs/v0.3.6-release-checklist.md",
    # docs/v0.3.7-release-checklist.md — v0.3.7 sibling of the v0.3.3–v0.3.6
    # checklists above; same release-cycle-record pattern. Crossed the cap on
    # creation (release-prep PR 2) carrying the three §3.1 Upgrade Notes (the
    # respond_policy→disposition reframe, the addressing-aware directedness fix,
    # and the v12→v14 person-identity migration) + §6 Known Gaps. Archive once
    # the tag ships.
    "docs/v0.3.7-release-checklist.md",
    # docs/v0.3.8-release-checklist.md — v0.3.8 sibling of the v0.3.3–v0.3.7
    # checklists above; same release-cycle-record pattern. Crossed the cap on
    # creation (release-prep PR 2) carrying the convergence-cluster surface
    # (Tier B + governance Layers 1/2/4 + interaction-summary + RFC 0050), the
    # §3.1 Upgrade Notes for *both* schema migrations (channel store v6→v8,
    # persona-memory v14→v15) + the behaviour-active-by-default caveat, and §6
    # Known Gaps. Archive once the tag ships.
    "docs/v0.3.8-release-checklist.md",
    # docs/v0.3.9-release-checklist.md — v0.3.9 sibling of the v0.3.3–v0.3.8
    # checklists above; same release-cycle-record pattern. Crossed the cap on
    # creation (release-prep PR 2) carrying the verbatim-recall surface (RFC 0036
    # recall over the RFC 0035 ledger + the §G conversation-window filter), the
    # §3.1 Upgrade Notes (the opt-in channels:recall permission, the two
    # channel-store migrations v8→v9→v10, the conversation-window membership
    # filter, the two accepted recall limitations) + the behaviour-active-by-default
    # caveat + the version-skew caution, and §6 Known Gaps. Archive once the tag ships.
    "docs/v0.3.9-release-checklist.md",
    # docs/v0.3.4-release-prep-plan.md is the v0.3.4 release-prep sequencer —
    # same release-cycle-accumulator pattern as the v0.3.0 / v0.3.1 plans and
    # the v0.3.3 checklist above. It crossed the 3 000-word prose cap when PR 1
    # made the provider-neutral onboarding scope explicit (the F-5 per-agent
    # OPENAI_API_KEY plumbing requirement) on top of the four PR scope +
    # acceptance blocks and the §Current state / §Known follow-up inventories;
    # it will keep accumulating as PRs 2–4 land their status + acceptance
    # residuals. Trimming the per-PR scope/acceptance detail would erase the
    # contract the sequence depends on. Remove this entry once v0.3.4 ships and
    # the plan is archived.
    "docs/v0.3.4-release-prep-plan.md",
    # docs/v0.3.6-release-prep-plan.md — v0.3.6 release-prep sequencer; same
    # accumulator pattern as the v0.3.4 plan above. Crossed the cap when PR 4
    # flipped §Status + Progress Overview to ✅ Complete. Archive once tagged.
    "docs/v0.3.6-release-prep-plan.md",
    # docs/v0.3.7-release-prep-plan.md — v0.3.7 release-prep sequencer; same
    # accumulator pattern as the v0.3.4/v0.3.6 plans above, and carries the
    # extra §Schema/migration-state contract for the F-7 Option D persona-memory
    # migration (v12→v14) this release adds. Archive once v0.3.7 ships.
    "docs/v0.3.7-release-prep-plan.md",
    # docs/v0.3.8-release-prep-plan.md — v0.3.8 release-prep sequencer; same
    # accumulator pattern as the v0.3.4/v0.3.6/v0.3.7 plans above, and carries an
    # extra workstream (the RFC 0050 channel-config fold-in) plus the §Schema/
    # migration-state contract for the two migrations this release adds (channel
    # store v6→v8, persona-memory v14→v15). Archive once v0.3.8 ships.
    "docs/v0.3.8-release-prep-plan.md",
    # docs/v0.3.9-release-prep-plan.md — v0.3.9 release-prep sequencer; same
    # accumulator pattern as the v0.3.4/v0.3.6/v0.3.7/v0.3.8 plans above, and
    # carries the §Schema/migration-state contract for the two channel-store
    # migrations this release adds (v8→v9 ledger, v9→v10 FTS; persona-memory
    # unchanged) plus the structural-gate + Known-follow-up inventories for the
    # verbatim-recall surface. Archive once v0.3.9 ships.
    "docs/v0.3.9-release-prep-plan.md",
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
    # status-flip-tips-a-tracking-doc pattern as docs/v0.3.1-plan.md above;
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
