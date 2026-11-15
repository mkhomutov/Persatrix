---
id: ISSUE-0267
summary: "Twelve seats in three blueprints, and the header of `templates/personas.yaml`, name a persona archetype with an `extends:` key that nothing applies: no command loads a blueprint, and in `config/agents.yaml` the key fails `make validate`. The YAML instructions told authors to use it; that line is corrected. One unit test checks that the anchors of one blueprint exist; the other two are checked by nothing."
status: open
severity: low
area: config
created: 2026-11-15
refs:
  - templates/personas.yaml
  - templates/sub_agents.yaml
  - blueprints/autonomous-multivendor/blueprint.yaml
  - blueprints/autonomous-roundtable/blueprint.yaml
  - blueprints/software-team/blueprint.yaml
  - tests/unit/python/test_autonomous_multivendor_blueprint.py
  - cli/src/main.rs
  - .github/instructions/yaml-config.instructions.md
  - .github/copilot-instructions.md
  - schemas/agent.schema.json
  - agents/sub_agents/templates.py
  - https://github.com/mkhomutov/Persatrix/pull/1133
---

# ISSUE-0267: `extends:` template references are documented and not built

## Summary

`templates/personas.yaml` holds persona archetypes under
`persona_templates` and opens with a comment that shows how to refer to
one: an `extends` key whose value is the file's path, a hash sign and
the archetype's name. The YAML instructions for assistants said the
same: use that key "to inherit persona/sub-agent definitions".

Three blueprints do. `blueprints/autonomous-multivendor` gives four
seats an `extends`, `blueprints/autonomous-roundtable` three and
`blueprints/software-team` five: twelve seats in all.

Nothing applies the key:

- No command loads a blueprint. `persatrix init --blueprint` prints
  `Command 'init' not yet implemented` (`cli/src/main.rs`), and no Go or
  Python code opens a file under `blueprints/`.
- No code looks for an `extends` key: neither the Go config loader nor
  the Python agents nor the validator.
- In `config/agents.yaml` the key does not validate. The agent schema
  allows no unknown key, so a persona that carries it fails
  `make validate`:

  ```
  agents.yaml at $.agents.3.persona: Additional properties are not allowed ('extends' was unexpected)
  ```

  `make validate` does not look at blueprints, so the twelve seats pass
  it unread.

## Context

- One thing does read `templates/personas.yaml`: the unit test
  `tests/unit/python/test_autonomous_multivendor_blueprint.py`, which
  runs in the required Python job. It loads the template file and fails
  if a seat of the multivendor blueprint names an archetype that is not
  there. Its comment gives the reason: a dangling name "would break a
  future `init --blueprint`". The seats of the other two blueprints are
  checked by nothing.
- The failure in `config/agents.yaml` is loud, not silent: an author
  who follows the old instruction finds out at `make validate`.
- `templates/sub_agents.yaml` exists too. Its own header calls it "a
  partial draft that nothing loads yet"; the loader it names,
  `agents/sub_agents/templates.py`, is a placeholder.

Checked on `main` at `111b041d`: a search of `agents/`, `internal/`,
`cmd/`, `cli/src/`, `scripts/`, `schemas/`, `config/`, `blueprints/`
and `tests/` for `extends` and for the template file's name finds the
file's own comment, the twelve seats and the one test. The validator
was run on a copy of the shipped config with the key added.

## Impact

An author, or an assistant following the old instruction, writes a
config that does not validate. A blueprint reads as if its seats take
their persona from the template file, and they would take nothing: the
day a loader is written, the seven seats no test covers may name
archetypes that are gone. Proposed low: nothing runs wrongly today.
The severity is proposed here, not ruled.

## Proposed fix / investigation path

Decide whether persona templates are wanted.

- If so, design the reference (the schema key, where it is resolved,
  what a seat may override), build it where a blueprint is loaded, and
  extend the anchor test to all three blueprints.
- If not, the template file cannot simply be deleted: the test would
  fail and twelve seats would name a file that is gone. Write each
  seat's persona out in the three blueprints first, then remove the
  file, the test's anchor case and the row in the instructions' project
  layout.

Until then, extend the anchor test to the other two blueprints: it is
one loop.

## Notes

> 2026-11-15 — found in [#1133](https://github.com/mkhomutov/Persatrix/pull/1133)
> while correcting the instruction files for
> [ISSUE-0202](ISSUE-0202-workflow-condition-and-approval-validate-then-do-nothing.md)
> and
> [ISSUE-0214](ISSUE-0214-declared-tool-permissions-are-never-enforced.md).
> The instruction line now says that nothing applies `extends` and to
> write each persona out in full in `config/agents.yaml`.

> 2026-11-16 — corrected after the review of [#1133](https://github.com/mkhomutov/Persatrix/pull/1133).
> The first text of this issue said that nothing reads the template
> file, that `templates/sub_agents.yaml` does not exist, and proposed
> deleting the template file. The search behind it had left out
> `blueprints/` and `tests/`: three blueprints carry the key, one unit
> test reads the file, `templates/sub_agents.yaml` is there, and the
> deletion would have failed that test. The instruction line, the row
> for the file in the shared instructions' project layout and this
> text now say what is so. This summary also no longer spells the key's
> value out: the index's reader cut the row at its hash sign
> ([ISSUE-0271](ISSUE-0271-issue-index-cuts-a-summary-at-a-hash-after-an-escaped-quote.md)).
