# RFC 0028 — PR Implementation Plan: notes on merged PRs

**Plan**: [RFC 0028 PR plan](0028-pr-plan.md)
**RFC**: [0028-agent-decision-policy-engine.md](0028-agent-decision-policy-engine.md)
**Split out**: 2026-10-30, unchanged, from the plan, when PR 2's review took
the plan toward its 8 000-word cap. Each merged PR's review findings move
here; its scope, tests and estimate stay in the plan.

---

## PR 1 — `feature/v040-rfc0028-types`

Merged as [#1054](https://github.com/mkhomutov/Persatrix/pull/1054) on
2026-10-24.

**Review findings** (PR 1):

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| F-1 | Medium | A number field took an `int` or a `bool`: `confidence=1` was written `1` and re-written `1.0`, so the line changed on a second pass; `confidence=True` was written and then refused on read; an infinite time built a record that could not be written | Fixed in-PR: both number fields are stored as finite floats, and a `bool` is refused |
| F-2 | Medium | A directive record's `action_class` meant the class the directive governs, but RFC 0012 gives a directive one only where the instruction allows it, a tie had to write `defer` in its place, and a tie could carry a verdict | Fixed in-PR: a verdict record has no action class (the directive's own row holds one, when it has one); a tie defers with no verdict; an action record names no subject id |
| F-3 | Low | `from_json` filled a missing `record_id` or `recorded_at` from their defaults, so one line read twice gave two ids and the reader's clock | Fixed in-PR: a line without either is refused |
| F-4 | Low | The decoder's comment said any field added later needs no change, but it reads only str, float, the enums and dataclasses, optionals and `tuple[X, ...]`; no test read an older line | Fixed in-PR: the comment says which types it reads, and a test reads a line without two defaulted fields; other types → deferred to PR 7 |
| F-5 | Low | `hash_content` raised on a lone surrogate, which a reply cut mid-emoji can hold | Fixed in-PR: `surrogatepass` |
| F-6 | Low | An end-of-interaction vote is `publish_channel`, so under E-11 a held reply and a vote to the same channel fold onto one hold, and the second is dropped | → deferred to PR 14 |
| F-7 | Low | The key-order test's `shuffled != line` held because of the separators, whatever the key order | Fixed in-PR: compact separators |
| F-8 | Low | The import walk started only from the package's top-level modules, so a subpackage reaching a loop went unseen | Fixed in-PR: it walks every module, and its negative control reaches the loop from a subpackage |
| F-9 | Low | The HITL glossary entry said all four §H classes are held; under OQ 7 two of them follow whether HITL is enabled | Fixed in-PR |
| F-10 | Info | `GRANT_APPROVAL` and `DENY_APPROVAL` are `respond`, the one class OQ 1 lets an operator automate; harmless while both return `not_implemented` | → deferred to PR 11 |
| F-11 | Info | ~1 100 changed lines against the ~300 estimate and the 500-line target, about half of them tests; the code is the JSON codec and the record's checks | Noted: the types stay with their tests |
| F-12 | Info | The RFC 0021 Phase 2 plan's PR 1 row, outside this row, moves to ✅ | Noted: #1053 merged, and `main`'s plan-status check stays red until the row flips |
| F-13 | Info | The PR 1 row did not link its PR | Fixed in-PR once the PR was open |
| F-14 | Low | `content_hash` is an unsalted SHA-256, so a short or guessable text — a yes or no, a small number, a known channel name — can be recovered by hashing guesses | → deferred to PR 3 |
| F-15 | Info | Construction checks no text or tuple field's type: `agent_id=7` is written and refused on read, and a list of candidates makes `to_json` raise | Noted: mypy types every caller, and PR 3's sink counts a write error without failing the turn |
| F-16 | Medium | A verdict record whose `selected_id` named no candidate passed: the lookup gave `None`, the verdict's own class | Fixed in-PR: a selection must name a candidate of the outcome's class |
| F-17 | Low | A number too large for a float raised `OverflowError`, and a deeply nested line `RecursionError`, not `ValueError` | Fixed in-PR: both become `ValueError` |
| F-18 | Low | The import walk dropped imports from the package root (`from .. import X`, `import agents`), whose `__init__` loads both runtimes | Fixed in-PR: such an import walks the root's `__init__` |
| F-19 | Low | The walk skipped the package `__init__` an import runs first, such as `agents/tools/__init__.py` for `agents.tools.permissions` | Fixed in-PR: every package above an imported module is walked |
| F-20 | Low | An outcome's or a rejection's `reason` took any text, so E-10 rested on each caller | Fixed in-PR: a reason is a short code (`[a-z][a-z0-9_]*`); `rationale` stays the policy's own words |
| F-21 | Low | The decoder read a fixed-length tuple as `tuple[X, ...]` without a word | Fixed in-PR: it refuses any other tuple |
| F-22 | Low | A reader refused every version but its own, so a version bump would orphan every log line | Fixed in-PR: a reader takes its version and older ones; the version goes up only for a change older lines cannot be read under |
| F-23 | Low | Only `hash_content(text)` existed, so each runtime would write argument values its own way before hashing | Fixed in-PR: `hash_arguments`, over canonical JSON |
| F-24 | Low | `recorded_at` was agent time, though `agents.clock` keeps telemetry and what reaches the orchestrator on real time, as the span and an approval's expiry are | Fixed in-PR: real time |
| F-25 | Low | The decoder resolved each dataclass's type hints on every line, about six times slower than once | Fixed in-PR: once per class |
| F-26 | Info | The hand-rolled import walk duplicates the import-linter gate | Noted: import-linter's graph misses F-19's case, so the walk stays |
| F-27 | Low | A record did not name its policy mode, so semi-automated and automated records looked alike | Fixed in-PR: `DecisionContext.mode`, a `PolicyMode` |
| F-28 | Low | The glossary gave every directive record a verdict; a tie has none | Fixed in-PR |
| F-29 | Info | The glossary aliased Decision Checkpoint as a bare "checkpoint", which also names a saved state snapshot | Fixed in-PR: the four named checkpoints are the aliases |
| F-30 | Low | `types.py` shadows the standard `types` module when its directory is first on `sys.path`; the repo names these `<area>_types.py` | Fixed in-PR: `decision_types.py`; the action-class tests move to their own file, keeping the types tests under 500 lines |

## PR 2 — `feature/v040-rfc0028-pipeline`

Merged as [#1072](https://github.com/mkhomutov/Persatrix/pull/1072) on
2026-10-31.

**Review findings** (PR 2):

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| F-1 | Low | A step that raised was logged with its traceback, so the error's message, which may quote the turn, reached the agent log; the raising fakes carried no probe word, so the tests' log assertion could not fail on them | Fixed in-PR: the log line names the error's type only, and every raising fake carries the probe |
| F-2 | Low | When broken constraint inputs removed every candidate, the outcome said `no_permitted_candidate`, as for a real refusal, though the module docstring listed `constraint_error` as an outcome reason | Fixed in-PR: with nothing left, a constraint input that broke on a candidate makes the outcome `constraint_error` |
| F-3 | Low | A policy could return a scored outcome under manual mode, the mislabelled record PR 1's F-27 guards against | Fixed in-PR: a confidence under manual mode is `policy_error` |
| F-4 | Low | The baseline's own outcome for no candidates carried a rationale the pipeline's did not, so one case gave two records | Fixed in-PR: the baseline returns the pipeline's outcome |
| F-5 | Low | After the rebase took `main`'s ROADMAP header, the feature commit still said the header names the pipeline | Fixed in-PR: the commit message is reworded |
| F-6 | Info | Constraint inputs and approval gates are sync callables, while the persona's roster and audience reads are async | Noted for PRs 5 and 7: fetch before calling `decide`; mypy refuses an `async def` at a typed call site |
| F-7 | Info | The baseline under another mode fails at every checkpoint, one ERROR each, rather than once at startup | Noted for PRs 4 and 5: choose the policy from the configured mode when wiring it |
| F-8 | Info | `ConstraintInput` and `ApprovalGate` are one type, so a gate passed as a constraint type-checks | Noted: both parameters are keyword-only |
| F-9 | Info | ~760 changed lines against the ~350 estimate and the 500-line target, over half of them tests | Noted: the pipeline stays with its tests |
| F-10 | Info | The head branch is not the planned `feature/v040-rfc0028-pipeline` | Noted: the session that opened the PR assigned it, as for #1069; the row records the planned name |
| F-11 | Low | When a constraint input removed the runtime's proposal, the baseline's pick of the next candidate still said `runtime_proposal`, so OQ 3's calibration would count a forced pick as the runtime's own | Fixed in-PR: the pipeline, which alone knows the proposal, makes it `proposal_removed` |
| F-12 | Low | The constraint inputs were read again for each candidate, so an iterator ran out after the first, and later candidates went unchecked: fail open | Fixed in-PR: both kinds of input are read once, up front |
| F-13 | Low | Whether a constraint input broke was read from the rejection's code, and the first error stopped the checks, so the outcome depended on the inputs' order, and a refusal with the code `constraint_error` read as a breakage | Fixed in-PR: a broken input is passed over and flagged; a later refusal still names the rejection |
| F-14 | Low | A held pick kept the policy's rationale and confidence on a deferral that selected nothing, which PR 7's `low_confidence` deferrals would be confused with | Fixed in-PR: the held outcome carries neither |
| F-15 | Low | A context whose checkpoint was text made the fail-closed handler's log line raise, with the step's error, and its message, chained to it | Fixed in-PR: such a context raises `TypeError` before any step runs |
| F-16 | Low | Only the pipeline refused a score in manual mode, so a record built directly or read from a line could carry one | Fixed in-PR: the record's own check refuses it |
| F-17 | Info | The broken-check log line read "could not run a approval gate" | Fixed in-PR: it names the check's slot, such as `approval_gates[0]` |
| F-18 | Low | The log line named neither which check broke nor where it is defined, so with several inputs an operator could not find it | Fixed in-PR: its slot and `module.qualname`, and the policy's class |
| F-19 | Info | The policy's outcome was checked by building a throwaway record, and candidate-id uniqueness was checked three times, once in a copied check | Fixed in-PR: `check_candidates` and `check_outcome`, shared with the record's own check |
| F-20 | Info | The `no_candidate` outcome was written out in both modules, as before F-4 | Fixed in-PR: one `NO_CANDIDATE` |
| F-21 | Low | An `async def` check or policy returned an unawaited coroutine: every candidate broke, and Python warned it was never awaited | Fixed in-PR: the coroutine is closed unrun, and the log line says the step is async |
| F-22 | Info | The baseline's tests sat in the pipeline's test file, against the one-test-file-per-module rule | Fixed in-PR: `test_decision_baseline.py` |
| F-23 | Info | Several tests checked unrelated cases in one block of asserts | Fixed in-PR: split, or one assert on the whole outcome or log line |
| F-24 | Info | F-9 and F-10 break the plan header's 500-line rule and the branch-naming rule, recorded only as noted | Noted: renaming the head branch would close the PR, and the size rule is guidance the checks do not enforce; the fixes above add to the overrun |
