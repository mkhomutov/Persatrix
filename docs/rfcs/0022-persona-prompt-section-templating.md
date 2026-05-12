---
id: RFC-0022
title: Persona Prompt Section Templating
summary: Section-by-section templating layer for persona prompts — moves prompt assembly out of code into per-persona YAML.
type: architecture
status: implemented
author: Maksim Khomutov
created: 2026-04-26
target: v0.3.0
depends_on:
  - RFC-0005
---

# RFC 0022 — Persona Prompt Section Templating

**Type**: architecture  
**Status**: ✅ Implemented  
**Author**: Maksim Khomutov  
**Date**: 2026-04-26  
**Target**: v0.3.0  
**Implemented in**: PR #213 (2026-04-27)  
**Depends on**: RFC 0005 (persona substrate); PR #210, #211, #212 (predecessor prompt-externalization PRs)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Template Syntax](#a-template-syntax)
  - [B. Section Inventory and File Layout](#b-section-inventory-and-file-layout)
  - [C. Conditional Inclusion Model](#c-conditional-inclusion-model)
  - [D. Composer Algorithm](#d-composer-algorithm)
  - [E. Loader Surface](#e-loader-surface)
  - [F. Byte-Identical Output Contract](#f-byte-identical-output-contract)
  - [G. Persona Config / Prompt Seam](#g-persona-config--prompt-seam)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

The bulk of the persona system prompt — identity, background, communication
style, quirks, goals, current state — is still assembled by an f-string
composer in [`agents/persona_runtime/prompt_assembly.py`](../../agents/persona_runtime/prompt_assembly.py).
PRs #210, #211, and #212 externalized task-agent instructions, four safety
snippets, and the behavioral-dimension descriptions, leaving the section
composer as the last inline persona-prompt surface.

This RFC proposes externalizing each section as a standalone markdown
template under `prompts/runtime/persona/sections/`, rendered through Python
`str.format_map` with named placeholders. Conditional inclusion lives in
the composer (Python), not in the templates. The composer joins rendered
sections with a paragraph separator and produces byte-identical output to
the current f-string composer for every shipped persona configuration.

## Motivation

The f-string composer in [`prompt_assembly.py:41-98`](../../agents/persona_runtime/prompt_assembly.py#L41-L98)
is the last meaningful inline-prompt surface in the persona runtime.
Three concrete problems with the status quo:

1. **Section text is invisible to non-Python contributors.** A documentation
   contributor or a designer iterating on persona-prompt wording today has
   to edit Python source and understand list-of-string composition with
   leading-newline conventions. Markdown files removes that barrier.

2. **Diffs are hard to read.** Wording changes inside an f-string blend
   with control flow in `git diff`. A wording tweak inside a markdown
   section file produces a clean text-only diff, which is what reviewers
   actually want to see.

3. **The composer is the seam future work has to cross.** RFC 0021
   (Persona Temporal Awareness) plans to add a now-anchor section. RFC
   0008 §D will likely add a memory-summary section. RFC 0011 (Channels)
   may add a channel-context section. Each of those is much cleaner to
   add as a new template file + a single composer entry than as another
   conditional block inside a growing f-string.

Predecessor PRs #210/#211/#212 also reserved
`prompts/runtime/persona/sections/` for this work and pointed at PR C of
the prompt-externalization follow-up plan as the inflection point that
locks in the templating syntax. That seam is the subject of this RFC.

## Goals

1. **Each persona-prompt section lives in its own markdown file** under
   `prompts/runtime/persona/sections/<name>.md`.
2. **Template syntax is `str.format_map` with named placeholders.** No
   logic in templates — no conditionals, no loops, no expressions.
3. **Conditional inclusion is composer-driven.** Each section has a
   predicate evaluated in Python; templates are pure content + slots.
4. **The composer's rendering is byte-identical** to the current f-string
   output for every shipped persona configuration. Pinned by a regression
   test so accidental drift fails CI rather than shifting LLM behavior.
5. **Adding a new section is a two-line composer change** plus one new
   markdown file. No template-syntax changes required.
6. **Loader path safety mirrors `load_snippet`**: deny-by-default subtree
   confinement, basename-only names, leading-dot rejection, cached reads.

## Non-Goals

- **Replacing the existing safety-snippet loader.** `load_snippet` already
  handles the snippets loaded by the persona runtime
  (`user-message-delimiters`, `memory-tool-usage`) and stays as-is. The
  new section-template loader is a sibling, not a replacement.
- **Adopting Jinja2 / Mustache / Handlebars.** Each adds a runtime
  dependency and tempts contributors to push logic into templates. Logic
  belongs in Python; templates stay declarative. See OQ-1.
- **Reformatting the persona system prompt.** This RFC is a structural
  refactor, not a behavior change. Output bytes match exactly.
- **Externalizing the user-turn event formatter** in
  [`prompt_assembly.py:100-150`](../../agents/persona_runtime/prompt_assembly.py#L100-L150).
  The PR plan classified that surface as user-turn templating, not
  persona prompt; moving it adds indirection without benefit.
- **Adding template inheritance, partials, or includes.** YAGNI until the
  section count grows past where copy/paste hurts. Revisit if/when a
  third or fourth section duplicates structure.
- **Changing the `persona_cfg` YAML schema.** The composer reads the
  same `persona_cfg` keys the f-string composer reads.

---

## Design / Implementation

### A. Template Syntax

Templates use Python's [`str.format_map`](https://docs.python.org/3/library/stdtypes.html#str.format_map)
with named placeholders. A template file is a UTF-8 markdown document
where placeholders take the form `{name}`. Substitution values are
provided by the composer as a `dict[str, str]`.

**Why `str.format_map` over alternatives:**

| Option | New dependency | Logic in template | Notes |
|--------|:--------------:|:-----------------:|-------|
| `str.format_map` | No | No | Stdlib. Logic-free by construction. ✅ |
| `string.Template` | No | No | Stdlib. `$name` syntax — less familiar in markdown. |
| Jinja2 | Yes | Yes | Powerful, but invites template-side logic. |
| Mustache (`pystache`) | Yes | Partial | Logic-less by design but adds a dep. |
| Custom mini-DSL | No | Configurable | Reinvention; maintenance cost. |

`str.format_map` wins on three axes: zero new dependencies, no
template-side logic by design, and `{name}` syntax that markdown editors
render harmlessly.

**Reserved characters.** Markdown content rarely contains literal `{` or
`}`. When it does, double them (`{{`, `}}`) per `str.format_map`'s
escaping rule. The loader does not pre-process the template — what's in
the file is what `str.format_map` sees.

**Whitespace.** A single trailing newline on the file (editor convention)
is stripped on load, mirroring `load_snippet`'s behavior. All other
whitespace — including internal blank lines — is preserved verbatim.

### B. Section Inventory and File Layout

Six section files live under `prompts/runtime/persona/sections/`:

| File | Always rendered? | Placeholders |
|------|:----------------:|--------------|
| `identity.md` | Yes | `{name}`, `{title_line}`, `{role}` |
| `background.md` | When `persona_cfg.background` is set | `{background}` |
| `behavior.md` | When `render_behavior(...)` is non-empty | `{behavior}` |
| `quirks.md` | When `persona_cfg.quirks` is non-empty | `{quirks}` |
| `goals.md` | When `persona_cfg.goals` has any populated key | `{goals}` |
| `current-state.md` | When `state.to_prompt_section()` is non-empty | `{state}` |

`behavior-dimensions.yaml` (added by PR #212) stays in the same
directory but is loaded through `load_dimension_descriptions`, not
through the new section loader — it has structural shape requirements
the markdown loader cannot enforce.

### C. Conditional Inclusion Model

**Composer-driven**, not template-driven. Each section in the composer's
ordered list carries a predicate `(persona_cfg, state) -> bool`. The
composer skips a section entirely when the predicate returns `False` —
the template is never read, no placeholder is supplied, no empty bullet
appears.

Two consequences worth pinning:

1. **Templates assume their predicate has fired.** A template's
   placeholders are guaranteed to be supplied with non-empty,
   well-typed values. Templates do not need to defend against
   missing or empty inputs.

2. **Intra-section conditionals are pre-rendered into placeholders.**
   `identity.md` includes the optional `Title:` line via a
   `{title_line}` placeholder. The composer fills it with either `""`
   or `"Title: <title>\n"` — the trailing newline is part of the
   pre-rendered fragment so the template can keep the placeholder on
   its own line without producing a stray blank line when absent.

   Pattern: when a section has small intra-section conditionals,
   pre-render the conditional fragment (including any trailing newline
   needed for layout) into a single placeholder and keep the template
   readable.

   When intra-section conditionals grow past one or two slots, split
   the section into two files instead.

### D. Composer Algorithm

The composer is a small function in `agents/persona_runtime/prompt_assembly.py`:

```python
def _render_persona_sections(persona_cfg, state, name, role) -> list[str]:
    """Return the ordered list of rendered section bodies, skipping omitted ones."""
    sections: list[str] = []
    for section in _SECTIONS:
        if section.predicate(persona_cfg, state):
            template = load_persona_section(section.name)
            sections.append(template.format_map(section.context(persona_cfg, state, name, role)))
    return sections
```

`_SECTIONS` is a module-level constant — a tuple of section descriptors
ordered as the prompt should appear. Adding a new section is one entry.

`_build_system_prompt()` becomes a thin shell:

1. Compute `name`, `role`, `state` (already available on `self`).
2. Call `_render_persona_sections(...)` → `list[str]`.
3. Append safety snippets (`user-message-delimiters`, conditionally
   `memory-tool-usage`) — same logic as today, kept here because they
   are loaded through `load_snippet`, not through `load_persona_section`.
4. Return `"\n\n".join(parts)`.

The `"\n\n"` join replaces the current `"\n".join(...)`-with-leading-`\n`-
prefixes idiom. The two produce identical bytes when section bodies are
defined as the *content* of each section without leading or trailing
newlines (Section F).

### E. Loader Surface

A new `load_persona_section(name, repo_root=None) -> str` in
[`agents/prompt_loader.py`](../../agents/prompt_loader.py), modeled
directly on `load_snippet`:

- Resolves to `<repo_root>/prompts/runtime/persona/sections/<name>.md`.
- Rejects path separators, leading dots, empty names.
- Confines reads to the section subtree via `relative_to`.
- Strips a single trailing newline.
- Cached via `functools.lru_cache(maxsize=64)` keyed by
  `(name, sections_root)`.

The loader is a sibling of `load_snippet` — same shape, different
subtree, different cache. They cannot collide because the resolved root
differs: `safety/` vs. `persona/sections/`.

### F. Byte-Identical Output Contract

The new composer must produce byte-identical system prompts for every
**well-formed** shipped persona configuration. Degenerate shapes that
the old f-string composer rendered as orphan section headers (e.g.
`goals: {"primary": ""}` produced a `Goals:` line with no bullets) or
crashed on (e.g. non-dict `goals`) are now collapsed to section
omission by the new predicates. These are intentional improvements,
not regressions; they are pinned by negative tests in
`test_persona_section_composer.py`. To make the byte-identical
contract achievable:

- Each template's content is the section body **without** leading or
  trailing newlines. e.g. `background.md` reads:

  ```
  Background:
  {background}
  ```

  with no blank line above or below.

- The composer joins rendered sections with `"\n\n"`. This produces a
  blank line between sections, matching the current f-string output
  where every section after identity carries a leading `\n` and parts
  are joined with `\n`.

- Pre-rendered conditional fragments include the line break that would
  otherwise be lost: `title_line = "Title: VP\n"` (with trailing `\n`)
  or `""`.

A test class `TestSystemPromptByteIdentity` in
`tests/unit/python/test_persona_section_composer.py` builds a fully-
populated persona, asserts the new composer's output equals a frozen
golden string. A second case with minimal config (no title, no
background, no quirks, no goals, no state delta, no memory tools)
exercises predicate-driven omission. A third case toggles each
predicate independently to catch boundary errors.

### G. Persona Config / Prompt Seam

The seam between `persona_cfg` (YAML) and the persona prompt
(templated markdown) stays on the Python side. The composer reads
config keys (`persona.background`, `persona.quirks`, `persona.goals.*`,
`persona.title`) and feeds them into placeholder dicts. Templates
never see config keys directly.

Two implications for future work:

- **Renaming a config key is a one-place change** in the composer's
  context builders, not a project-wide grep across markdown files.
- **Adding a config key that drives prompt content** requires a
  composer change — there is no escape hatch where a new key auto-
  appears in a template.

This is intentional. The point of the seam is that the prompt has a
stable contract with its templates regardless of how the YAML schema
evolves.

---

## Security Considerations

The new loader inherits the deny-by-default posture of `load_snippet`:

- Path resolution uses `Path.resolve().relative_to(sections_root)`, so
  `..` traversals, absolute paths, and symlink targets that escape
  the subtree all raise `PromptLoadError`.
- `name` rejects path separators (`/`, `\`) and leading dots, so a
  caller cannot escape the subtree via the basename argument.
- The cache key includes the resolved sections root, so test fixtures
  with distinct `tmp_path` roots cannot poison the production cache.

`str.format_map` itself has a known footgun: a malicious template can
read attributes off substitution values via `{x.__class__}` or
`{x.attr}`. We mitigate by passing only `str` values into the format
context — `str` exposes nothing security-relevant via `__class__`
chains. Even so, the templates are repository-controlled markdown,
not user-supplied input, so the threat model is "an attacker with
commit access" — out of scope.

The set of templates loaded is determined by the composer's
`_SECTIONS` table, not by data. A template name cannot be supplied
from `persona_cfg` or from the LLM. This closes the door to a
hypothetical injection where an attacker steers section selection by
crafting a persona config — there is no path from config keys to
template names.

---

## Phased Implementation Plan

This RFC ships in a single PR (PR C of the prompt-externalization
follow-up plan). No phasing — the change is all-or-nothing because the
byte-identical contract requires the new composer and the templates to
land together.

### Phase 1: Templates + Composer + Loader

1. Add `prompts/runtime/persona/sections/identity.md`,
   `background.md`, `behavior.md`, `quirks.md`, `goals.md`,
   `current-state.md`.
2. Add `load_persona_section(name)` to
   [`agents/prompt_loader.py`](../../agents/prompt_loader.py).
3. Replace `_build_system_prompt()`'s f-string composer in
   [`agents/persona_runtime/prompt_assembly.py`](../../agents/persona_runtime/prompt_assembly.py)
   with the section-driven composer described in §D.
4. Add unit tests for the loader (path safety, missing file, caching)
   and the composer (byte-identical parity, predicate-driven omission,
   each predicate boundary).
5. Update [`docs/prompt-organization.md`](../prompt-organization.md)
   and [`prompts/runtime/persona/sections/README.md`](../../prompts/runtime/persona/sections/README.md)
   to reflect that the section composer is now externalized.

**Dependencies**: PR #211 (`load_snippet` infrastructure) and PR #212
(`prompts/runtime/persona/sections/` subtree) — both merged.

---

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | `agents/prompt_loader.py` | Add `load_persona_section` (+~50 LOC) |
| Python agents | `agents/persona_runtime/prompt_assembly.py` | Replace f-string composer with section loop (~+30 / -50 LOC) |
| Prompts | `prompts/runtime/persona/sections/identity.md` | New file |
| Prompts | `prompts/runtime/persona/sections/background.md` | New file |
| Prompts | `prompts/runtime/persona/sections/behavior.md` | New file |
| Prompts | `prompts/runtime/persona/sections/quirks.md` | New file |
| Prompts | `prompts/runtime/persona/sections/goals.md` | New file |
| Prompts | `prompts/runtime/persona/sections/current-state.md` | New file |
| Tests | `tests/unit/python/test_prompt_loader.py` | Loader tests for `load_persona_section` |
| Tests | `tests/unit/python/test_persona_section_composer.py` | Byte-identical + predicate boundary tests |
| Docs | `docs/prompt-organization.md` | Update §"Reserved persona sections (templated)" → live |
| Docs | `prompts/runtime/persona/sections/README.md` | Document section file inventory + template syntax |
| Docs | `ROADMAP.md` | Add RFC 0022 to Master Index |
| Docs | `FILEMAP.md` | Auto-regenerated by pre-commit hook |

Estimated diff: ~250 LOC moved + ~120 LOC new (loader, composer table,
tests). RFC document itself is separate.

## Test Strategy

- **Unit tests** (`tests/unit/python/test_prompt_loader.py`): loader
  surface — happy path, missing file, path-separator rejection,
  leading-dot rejection, symlink escape rejection (skipped on
  Windows), trailing-newline strip, cache repeat-read.
- **Unit tests** (`tests/unit/python/test_persona_section_composer.py`):
  - `TestSystemPromptByteIdentity` — fully-populated persona produces
    a pinned golden string.
  - `TestPredicateBoundaries` — toggling each predicate independently
    produces exactly the right omission/inclusion.
  - `TestMinimalPersona` — empty optional sections produce a clean
    prompt with no stray blank lines.
- **Existing tests** (`test_llm_persona_agent.py`, `test_memory_notes.py`,
  `test_memory_instructions.py`, `test_persona_timeouts.py`,
  `test_relationship_memory_user_prompts.py`): all of these assert
  substrings in `_build_system_prompt()` output. They must continue
  to pass unchanged — this is the strongest signal that the refactor
  preserved behavior.
- **Integration tests** (`tests/integration/test_persona_e2e_*.py`):
  exercise the full prompt-build path end-to-end. No code changes
  required; they pass through the new composer transparently.
- **Manual tests**: `make validate` succeeds; `make test` passes.

## Open Questions

1. ~~Template syntax: `str.format_map` vs. Jinja2 vs. Mustache?~~  
   **Resolved**: `str.format_map`. Stdlib, logic-free by construction,
   familiar `{name}` syntax (2026-04-26).

2. ~~Conditional inclusion: composer-driven vs. template-driven?~~  
   **Resolved**: composer-driven. Templates stay pure content;
   inclusion is a Python-side decision (2026-04-26).

3. ~~Should pre-rendered fragments include their trailing newline, or
   should the template own the newline?~~  
   **Resolved**: pre-rendered fragments include their newline. This
   keeps the template readable (`{title_line}Role: {role}` reads as
   "optional title-line then role-line") and pushes spacing concerns
   into the composer where they're testable (2026-04-26).

4. Does any sub-agent template (RFC 0010) want to share the same
   loader, or stay sibling? Probable answer: sibling, since sub-agents
   are likely to use a different placeholder set and ordering. Decide
   when RFC 0010 reaches design.

5. Is `current-state.md` redundant with [`PersonaState.to_prompt_section`](../../agents/persona_types.py)?
   The section template currently wraps a single `{state}` placeholder
   that is itself constructed in Python. Future work could externalize
   the `to_prompt_section` rendering too, but doing so requires
   structural data (mood enum, stress threshold, etc.) — out of scope
   for a markdown templating RFC. Tracked as a follow-up.

## Decision / Next Steps

This RFC is proposed alongside the implementing PR. On merge:

1. Status advances to **✅ Implemented**.
2. The "Reserved persona sections (templated)" stanza in
   [`docs/prompt-organization.md`](../prompt-organization.md) is
   replaced with a live "Persona section templates" section pointing
   at this RFC and the loader.
3. Future per-section additions (e.g. RFC 0021 now-anchor) follow the
   pattern documented here.

## Related Documentation

- [Prompt Organization](../prompt-organization.md) — the canonical
  layout doc; this RFC fills in its "Reserved persona sections"
  stanza.
- [RFC 0005 — Persona Agent & Memory System](0005-persona-agent-memory.md)
  — the upstream substrate the persona system prompt is built on.
- [RFC 0021 — Persona Temporal Awareness](0021-persona-temporal-awareness.md)
  — first downstream consumer; will add a now-anchor section through
  this seam.
- Predecessor PRs: #210 (task-agent instructions), #211 (safety
  snippets), #212 (behavioral-dimension descriptions).
