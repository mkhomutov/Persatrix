# vX.Y.Z — release baseline (record)

**Companion to**: `docs/vX.Y.Z-release-prep-plan.md` §Current state
**Captured**: YYYY-MM-DD, at release-prep PR 0, against `main` tip `<sha>`
**Status**: frozen once release-prep PR 4 merges — a record of the tree the release-prep PRs acted on

> Guidance: this is the "Current state" section of the release-prep plan,
> split out when the plan nears the 3 000-word cap (v0.3.15 precedent). Every
> fact below that differs from the previous release's checklist is named in
> the last section — PR 2 copies that checklist forward, and a copied row the
> baseline contradicts is *wrong*, not merely stale.

---

## Issue roll-up

| Issue | Slotted by | State on the RC tip | Closes at |
|-------|-----------|---------------------|-----------|
| ISSUE-NNNN | Amendment YYYY-MM-DD / plan lock | <merged PRs; what remains> | release-prep PR 1 / stays open (why) |

## Version state

All version strings at `X.Y.(Z-1)` (`cli/Cargo.toml`, `agents/pyproject.toml`, the two `_DEFAULT_SERVICE_VERSION`s, `ui_handlers.go`); PR 3 bumps.

## Schema / migration state

| Store | From → to | PR | Reader | Shape |
|-------|-----------|----|--------|-------|
| <channel store / persona memory> | vN → vN+1 | #… | lands in #… | ahead of its reader / repair shipping with its consumer / index only |

Drop-in, or forward-only with a downgrade caution: <state which>.

## Wire-compatibility state

<Additive proto fields? none? Lockstep guards touched?>

## Manual-test state

| MT | Version | New or changed legs | Owner of the edits |
|----|---------|---------------------|--------------------|

## Eval / golden-trace state

<n> recipes; <re-record needed? which?>

## Changelog state

`[Unreleased]` holds <n> entries; bullets per story, not per PR; <which PRs fold into which story>.

## Dependency-notices state

`make notices` <will / will not> show a delta since the last tag (<why>).

## Differs from the vX.Y.(Z-1) checklist

The rows PR 2 must **not** copy forward:

- <fact> — was <old>, is <new>.
