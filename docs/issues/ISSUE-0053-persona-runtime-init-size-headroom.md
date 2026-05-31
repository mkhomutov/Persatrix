---
id: ISSUE-0053
summary: "agents/persona_runtime/__init__.py sits 1 line under the 500-line code cap — the next addition trips the strict file-size check"
status: resolved
severity: low
area: agents/persona_runtime
created: 2026-05-16
closed: 2026-05-31
refs:
  - docs/rfcs/0034-persona-conversational-working-memory.md
  - docs/rfcs/0034-pr-plan.md
---

## Summary

`agents/persona_runtime/__init__.py` is 499 lines — exactly 1 under the
500-line review-friendly cap enforced (strict) by
`scripts/checks/file_size.py`. Any one-line addition trips the check.

## Context

RFC 0034 Phase 1 PR 2 re-exported `ConversationWindowConfig` and
`build_conversation_messages` from the package root, adding one import
line and two `__all__` entries. To stay under the cap PR 2 condensed the
module docstring rather than land over-limit. The file is now at the
ceiling with no headroom.

The file is not grandfathered in `GRANDFATHERED_FILES` (it is under, not
over, the cap), so the next contributor to add a symbol — a re-export, a
helper, an `__all__` entry — gets a `--strict` CI failure with no in-file
hint that the file was deliberately parked at the limit.

## Impact

Review-friendliness / CI-ergonomics only. Nothing is broken today. The
hazard is a surprise CI failure on an unrelated change, plus pressure to
"fix" it by trimming docstring prose (lossy) instead of extracting code.

## Proposed fix / investigation path

When the cap is next tripped, extract a cohesive block into a submodule
rather than trimming prose — mirroring the existing mixin-split pattern
(`action_loop`, `memory_context`, `state_persistence`). Candidates: the
`Linkable` Protocol + `_PENDING_TICK_LINKS_CAP`, or the
`_coerce_event_timeout` helper, each of which is self-contained. That
restores headroom and keeps `__init__.py` focused on assembly +
re-exports.

## Resolution

> 2026-05-31 — resolved (v0.3.5 candidate fold-in, per the
> [v0.3.5 plan §Candidate fold-ins](../v0.3.5-plan.md#candidate-fold-ins-maintainer-decision)).
> Took the issue's own recommended path: extracted the self-contained
> `_coerce_event_timeout` helper into a dedicated `event_timeout` submodule
> (`agents/persona_runtime/event_timeout.py`) rather than trimming docstring
> prose, mirroring the existing `conversation_window` / `summarize_close`
> extraction precedent. `agents/persona_runtime/__init__.py` re-exports the
> helper (`# noqa: F401`) and keeps it in `__all__`, so every existing
> importer — `agents/persona.py`'s back-compat re-export,
> `test_persona_agent_factory`, `test_persona_state` — is unaffected.
> `__init__.py` dropped 500 → 465 lines (35 lines of headroom restored).
> Test-driven by
> `tests/unit/python/test_persona_runtime_event_timeout_extraction.py`
> (submodule home + same-object re-export from both the package root and
> `agents.persona`, `__all__` membership, coercion behaviour parity, and a
> regression pin that `__init__.py` stays well under the cap).

## Notes

> 2026-05-16 — captured during RFC 0034 PR 2 review. No fix scheduled;
> the issue exists so the next contributor who trips the cap has context
> instead of a bare CI failure.
