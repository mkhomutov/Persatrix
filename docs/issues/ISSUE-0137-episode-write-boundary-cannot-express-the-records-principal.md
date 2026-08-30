---
id: ISSUE-0137
summary: "The persona-memory write boundary takes speaker_id explicitly but resolves principal_id ambiently, so a record's tenant is carried by one `with` statement rather than by the call"
status: open
severity: medium
area: memory
created: 2026-08-30
refs:
  - docs/issues/ISSUE-0123-per-speaker-interaction-scope.md
  - docs/issues/ISSUE-0131-derived-memory-has-no-speaker-attribution.md
  - docs/issues/ISSUE-0082-residuals-pr-plan.md
  - docs/rfcs/0020-interaction-lifecycle.md
---

## Summary

`EpisodicMemory.store_episode` and `FactStore.store` accept `speaker_id` as a
parameter but have no `principal_id` parameter — they resolve the tenant from
the ambient `principal_scope`. The two halves of the same
`(principal, speaker, scope)` record key therefore reach the same row by two
different mechanisms, and only one of them is visible in the signature.

## Context

Found in the PR [#846](https://github.com/mkhomutov/Persatrix/pull/846) review
and re-confirmed against `main` after
[#849](https://github.com/mkhomutov/Persatrix/pull/849). It is **not a live
misattribution today** — that half was fixed in #846, which wrapped the whole
derivation in `with principal_scope(interaction.principal_id):`
(`close_path.py`). This issue is about what holds that fix up.

The record freezes both key halves at open (`Interaction.principal_id` /
`Interaction.speaker_id`). At the write boundary they diverge:

| Key half | How it reaches the row | Enforced by |
|---|---|---|
| `speaker_id` | `store_episode(speaker_id=…)` — explicit argument (ISSUE-0131, #849) | the signature |
| `principal_id` | `resolve_active_principal()` reading the ambient ContextVar | one `with` statement at one call site |

`grep -rn "principal_scope(" agents/` returns exactly **one** binding on the
memory write path — `close_path.py:258`. Every close-derived episode, fact,
projection and relationship row in the system is tagged correctly because of
that single line, and nothing fails if it is removed or if a new derived-write
path is added without it.

Demonstrated directly — `store_episode` called under a mismatched ambient
scope, as any caller lacking the wrapper would:

```python
with principal_scope("bob"):                    # the closing request's tenant
    await mem.store_episode(..., interaction_id="i-alice", speaker_id="alice")
# → row: principal_id='bob', speaker_id='alice'
```

The row is internally contradictory: it claims alice spoke inside bob's tenant.
The write boundary has no way to notice, because it was never told whose record
this is.

## Impact

Latent, silent, and cross-tenant — the combination the ISSUE-0123 re-key exists
to prevent.

- **Recall is strict-equality with no carve-out** (`_principal_filter`), so a
  row written under the wrong principal is invisible to the speaker who owns it
  and readable by whoever triggered the close. That inverts the boundary rather
  than merely blurring it.
- **The failure is silent.** No exception, no counter, no log — a mistagged row
  looks exactly like a correct one, and only shows up as memory that has gone
  missing for one tenant and appeared for another.
- **The regression surface is any new derived write.** Room-wide fans and
  `idle_check` close *other* tenants' records inside whichever tenant's request
  scope triggered them, so a new close-derived writer that forgets the wrapper
  is wrong on exactly the multi-tenant paths that are hardest to test and were
  the original defect.
- **No test pins the wrapper.** The suites that would catch its removal are the
  ones asserting row tenancy through the close path; removing the `with` line
  changes no signature and breaks no type.

## Proposed fix / investigation path

Make the write boundary able to say whose record it is, so the invariant is
carried by the call rather than by ambient state:

1. Add `principal_id: str | None = None` to `EpisodicMemory.store_episode` and
   `FactStore.store`, `None` meaning "resolve ambient" — preserving every
   current caller. `episodic_queries.insert_episode` already takes
   `principal_id` explicitly, so this closes a gap that exists only at the tier
   seam.
2. Pass `interaction.principal_id` from the close path alongside
   `speaker_id=interaction.speaker_id or None`, so both key halves travel the
   same way and a reader of the call site sees the whole key.
3. Keep the `principal_scope` binding: Phase 2's facts, projections and
   relationship writes inherit it through the `asyncio.create_task` context
   snapshot, and the relationship tier has no explicit parameter to pass. The
   goal is that the explicit argument is the contract and the scope is
   defence-in-depth, not the other way round.
4. Pin it: a test that a record whose frozen principal differs from the ambient
   one lands under the **record's** principal, exercised through the close path
   — the shape the room-fan and idle-flush paths actually produce.

Worth doing in the same change: the relationship tier is the one derived write
with no `record_admission` and no explicit tenant argument at all
([[ISSUE-0122]] territory), so it stays ambient-only either way — state that
rather than leave it looking like an oversight.

## Notes

> 2026-08-30 — captured after PR #849. Named as open finding #1 of the #846
> review with the instruction to bring it into that PR's scope; #849's scope
> then inverted (the principal binding had already landed early in #846, and
> the reserve re-size split out to PR 4b), so it was never picked up. Filing it
> so it stops riding on a PR description. Not a blocker for #849 — the live
> paths are correct — but it should land before the workstream closes, and
> before v0.4.0 organizations add derived-write paths on top of this boundary.
