# RFC 0009 — PR Implementation Plan (Phases 3–4): notes on merged PRs

**Plan**: [RFC 0009 Phases 3–4 PR plan](0009-phases-3-4-pr-plan.md)
**RFC**: [0009-security-sandboxing.md](0009-security-sandboxing.md)
**Split out**: 2026-10-30, unchanged, from the plan, when PR 5's second
review took the plan toward its 8 000-word cap. Each merged PR's
implementation note and review findings move here; its scope, tests and
estimate stay in the plan.

---

## PR 3 — `feature/v040-rfc0009-arg-schemas`

Merged as [#1069](https://github.com/mkhomutov/Persatrix/pull/1069) on
2026-10-29.

**Implementation note (2026-10-29)**: how G-2 is met, each point a new
default this PR's review confirms or overturns.

- **Where.** The check is `agents/tools/arg_check.py`, not `registry.py`,
  which keeps room for PR 4's output cap and PR 15's tag; `registry.py`
  reads each tool's signature once into an `ArgumentSpec`.
  `tool_list.admit_call` replaces the lookup at both call sites, and
  RFC 0028 PR 10's seat goes between the admission and the run, on the
  checked arguments. Since the PR edits `base.py` at 505 lines, it first
  moves the task agent's tool round (`_build_tool_definitions`,
  `_execute_tools`) unchanged into `agents/task_tool_round.py`, the seam
  RFC 0028 PR 6 named, as personas have `tool_round.py`. A check that
  itself fails refuses the call: running it unchecked would give the seat a
  value nobody checked.
- **Against what.** Missing and unknown arguments are read off the
  signature, not the generated schema, which admits unknown keys; a tool
  with `**kwargs` takes any name. The schema no longer offers `*args` or
  `**kwargs` as a required string: a call binds by name alone, so the model
  was told to send what the check refuses. No built-in has either, so no
  golden changes. `jsonschema` checks each parameter's narrowing, and only
  on a tool that narrows one.
- **Type.** A declared type is enforced only on a parameter the tool
  narrows (`@tool(narrow=...)`), since an off-type value runs today on five
  of the nine built-ins: a `shell_exec` timeout of 5.5, a falsy `tags`, a
  `recall_notes` limit of `true`. A narrowing is added to the offered
  schema, and a narrowed parameter is checked against that whole schema, its
  declared type included, so the model is told what is checked; a narrowed
  number must also be finite. A narrowed parameter's null is checked rather
  than replaced by its default, so a null that failed inside the tool is
  refused, unless the annotation takes `None`, when it runs as before. The
  keywords are the ones every provider takes, Gemini's subset being the
  narrowest; a narrowing that cannot work fails when the tool is defined: a
  keyword on a type it does not limit, an empty enum, a bound that is not
  finite, a minimum over its maximum. The built-ins narrow eleven string
  parameters where every off-type value fails today, which a test pins by
  running each tool without the check; these restate the type, so no offered
  schema or golden changes.
- **One form.** An integer parameter given canonical integer text (`"30"`,
  never `" 30"` or `"030"`) or a whole float gets the int; a bool, a
  fraction, NaN and infinity pass unchanged, so RFC 0028 PR 10's hash must
  handle those itself. A number parameter given an int or integer text the
  float holds exactly, or decimal text that reads back the same (`"0.5"`,
  never `"0.50"` or `"1e5"`), gets the float, so Anthropic's `1` and
  Gemini's `1.0` are one call; a bool passes unchanged. A null for an
  optional parameter means its default, unless the annotation takes `None`
  or the tool narrows the parameter. Defaults are not filled in, and text
  passes exactly as sent (`http_request` upper-cases its own method), so
  PR 10's hash binds defaults itself, and PR 15's rule for a writing method
  compares as the tool does.
- **One exception to "fails today".** On Python 3.11 a null `shell_exec`
  command read the agent's standard input; its narrowing refuses it.
- **Refusal and audit.** The model gets JSON with
  `tool_argument_validation_error` and lists of the missing, unknown and
  invalid arguments; the audit line is INFO on `agents.tools.audit`,
  written through the helper the fact tier's audit uses
  (`agents/observability/audit_record.py`). Neither carries a value, and the
  audit line counts the unknown names instead of listing them, since the
  model chooses a name as freely as a value; the reply passes each through
  `bounded`, the one log-injection defence. A refused call is recorded as a
  failed tool call, an `agent.tool.execute` span with an error status and
  `agent.tool.invocations` with `tool.success=false`, as the wrapper
  recorded the `TypeError` it replaces.

**Review findings** (PR 3):

| Finding | Severity | Description | Disposition |
|---------|----------|-------------|-------------|
| F-1 | Low | A narrowing was checked on its own, while the model was offered it merged with the declared type, so `{"minimum": 1}` admitted `false`, which a tool reads as 0 | Fixed in-PR: a narrowed parameter is checked against its whole offered schema |
| F-2 | Low | A numeric bound let NaN through, since NaN compares false with every bound, and Python's `json` reads NaN in a model's arguments | Fixed in-PR: a narrowed number must be finite |
| F-3 | Low | Four allowed keywords (`const`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`) and an enum of numbers make Gemini refuse every request that offers the tool; a fractional `multipleOf` made the check raise on NaN, infinity or a huge integer; `pattern` runs as Python's `re.search`, where `$` matches before a final newline | Fixed in-PR: the keywords are `type`, `enum` (text only), `minimum`, `maximum`, `minLength` and `maxLength`; a test pins the set, and another checks them against Gemini's SDK where it is installed |
| F-4 | Low | A null was replaced by the default for an annotation that takes `None` but is a `Literal` or a type alias | Fixed in-PR |
| F-5 | Low | No test covered `Optional`, `Union`, a `Literal`, an alias or annotations that cannot be resolved | Fixed in-PR: one case each |
| F-6 | Low | An unknown name was escaped only for lone surrogates, so control and right-to-left characters reached the audit line raw, and the one test of it could not fail | Fixed in-PR: every character that does not print is escaped, and the test runs the audit fields through the log shipper's encoder |
| F-7 | Low | The audit line's leak check read only some of its fields | Fixed in-PR: every field the record carries |
| F-8 | Low | No test covered the audit line's redactor or its fallback, though a redactor that raised past it would end the agent's turn | Fixed in-PR: two tests |
| F-9 | Low | The binding test checked only refused or admitted, so a `**kwargs` tool could lose its extra arguments unseen | Fixed in-PR: an admitted call keeps every argument |
| F-10 | Info | A 5 000-digit case passed only under the interpreter's default digit limit | Fixed in-PR: its own test sets the limit |
| F-11 | Info | The fail-closed test did not check the operator's log line, the only record of why the call did not run | Fixed in-PR |
| F-12 | Info | A refusal read "at least 1 characters" | Fixed in-PR |
| F-13 | Info | The CHANGELOG said arguments that are not an object failed inside the tool (they failed before it, as an internal error), that every argument is named (16 unknown names are), stated the null rule without its exception, and read as a full list of the narrowed arguments | Fixed in-PR |
| F-14 | Low | New comments said "RFC 0009 PR 3", which already names the Phases 1–2 plan's input sanitizer | Fixed in-PR: "RFC 0009 Phase 3" |
| F-15 | Low | The comments stating the rule "narrow only what fails today" left out its one exception, the null `shell_exec` command | Fixed in-PR |
| F-16 | Info | The `argument_spec` comment left out the positional-only case | Fixed in-PR |
| F-17 | Info | `tool_list.py` described the pre-tool seat as already there | Fixed in-PR |
| F-18 | Info | The glossary's *Narrowing* did not set itself apart from the recall tool's narrowing parameters | Fixed in-PR |
| F-19 | Info | The Go audit registry gave no pointer that the Python runtime now writes `tool.arg_invalid` | Fixed in-PR: a comment, as `agent.deliberated` has |
| F-20 | Low | RFC 0009 had no dated note on how PR 3 meets its amendment, and G-2 no pointer to the exception | Fixed in-PR |
| F-21 | Info | The note to RFC 0028 did not say defaults are not filled in, nor that text passes as sent | Fixed in-PR |
| F-22 | Info | The pin test sends no falsy value of another type | Noted: verification found each fails through the same branch as one the test sends |
| F-23 | Medium | A null for an optional narrowed parameter was replaced by its default before the narrowing ran, so `http_request` with a null method, which failed inside the tool, sent a live GET; the pin test ran the tools without the check only | Fixed in-PR: a narrowed parameter's null is checked, and a test runs the check on every off-type value of every narrowed built-in |
| F-24 | Low | A narrowed parameter annotated to take `None` refused a null the tool ran on before | Fixed in-PR: such a null passes the narrowing unchecked |
| F-25 | Low | The generated schema still offered `*args` and `**kwargs` as required strings, so the model was told to send what the check refuses | Fixed in-PR: neither is offered; no built-in has either, so no golden changes |
| F-26 | Low | A narrowing that cannot work passed the definition-time check: a NaN or infinite bound (which also breaks the request's JSON), an enum that is not a list (a `TypeError`) or is empty, a length on a number, a bound on text | Fixed in-PR: each raises `ValueError`, and so does a minimum over its maximum |
| F-27 | Low | Only integer parameters had one form: a number parameter kept `1` and `1.0`, and `"0.5"` and `0.5`, apart, so RFC 0028's hash would see two calls | Fixed in-PR: a number parameter gets the float where the conversion is lossless |
| F-28 | Low | A refused call recorded no tool span or metric, though the `TypeError` it replaces did, so a tool failure rate stopped counting it | Fixed in-PR: a failed `agent.tool.execute` span and `agent.tool.invocations{tool.success=false}`, also for a call refused because the check itself failed |
| F-29 | Low | Unknown argument names, which the model chooses, went verbatim into the audit line, so a name could carry a secret into the log stream | Fixed in-PR: the audit line counts them; the model's reply, which repeats what the model sent, still names them |
| F-30 | Low | The tool audit helper copied the fact tier's redact-then-log body without its re-entry guard | Fixed in-PR: one helper, `agents/observability/audit_record.py`, guard included, writes both |
| F-31 | Info | Unknown names had an escaper of their own beside `agents/sub_agents/_log_safety.bounded`, the single log-injection defence | Fixed in-PR: `bounded` replaces every character that does not print, and the check calls it |
| F-32 | Info | The narrowing pass ran on every call, even for a tool with no narrowing, and `ArgumentSpec.narrowed` was read only by tests | Fixed in-PR: the pass runs only over narrowed parameters |
| F-33 | Info | The wording for a broken keyword was split between a table and inline branches, with a fallback that could not run | Fixed in-PR: one table for all six keywords, their types included |
| F-34 | Low | The PR edited `agents/base.py` at 505 lines without splitting it, against the size rule | Fixed in-PR: the task agent's tool round moved unchanged to `agents/task_tool_round.py`, RFC 0028 PR 6's seam; `base.py` is 412 lines |
| F-35 | Info | The PR description ended with an assistant attribution line, which the repository's rules forbid in PRs and commits | Fixed in-PR: removed from the description |
| F-36 | Info | The head branch is not the planned `feature/v040-rfc0009-arg-schemas` | Noted: the session that opened the PR assigned its head branch, which a PR cannot change; the row records the planned name, as for #941 and #1063, and the squash commit carries no branch name |
