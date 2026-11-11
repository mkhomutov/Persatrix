---
id: ISSUE-0230
summary: "The persona agent is one class cut into twelve classes across twelve files, about 2,700 lines of class body. The mixins share their state through `self` and each restates the attributes it needs, so the coupling of a single large class remains but can no longer be seen in any one file, and tests reach into its private methods hundreds of times."
status: open
severity: low
area: agents/persona_runtime
created: 2026-11-10
refs:
  - agents/persona_runtime/__init__.py
  - agents/persona_runtime/episode_routing.py
  - agents/persona.py
  - agents/base.py
  - docs/issues/ISSUE-0231-size-check-counts-comment-lines.md
---

# ISSUE-0230: The persona class is one class cut into mixins

## Summary

`_LLMPersonaAgent` (`agents/persona_runtime/__init__.py`) resolves to
twelve first-party classes in twelve files:

`_LLMPersonaAgent` → `_ActionLoopMixin` → `_ToolRoundMixin` →
`_ConversationWindowMixin` → `_MemoryContextMixin` →
`_PromptAssemblyMixin` → `_EpisodeRoutingMixin` → `_CloseLifecycleMixin`
→ `_StatePersistenceMixin` → `PersonaAgent` → `BaseAgent` →
`_TaskToolRoundMixin`

The twelve class bodies come to 2,731 lines; the files that hold them
total 3,785. The last three classes are bases shared with task agents.
The nine persona-runtime classes are 2,124 lines.

## Context

- The mixins are not independent parts. They read and write the same
  instance attributes, and each one re-declares the attributes it uses
  as annotations so the type checker accepts it (`episode_routing.py`
  is an example). One re-declaration exists only to silence a
  method-resolution conflict.
- The modules' own docstrings give the file-length check as the reason
  for the split
  ([ISSUE-0231](ISSUE-0231-size-check-counts-comment-lines.md)), not a
  division of responsibility. `EpisodicMemory`, `MemoryStore`,
  `EventLoop` and `NoteStore` are built the same way.
- The tests call the class's private methods directly, at several
  hundred call sites: `_inject_memory_context` alone has 137,
  `_store_event_episode` 88 and `_build_system_prompt` 56.

This is a judgement about cost, not a defect that can be reproduced.

## Impact

To understand one persona turn a reader has to hold twelve files in
mind. Turning a mixin into a real component breaks the tests that call
its private methods, although those tests guard nothing public, so the
structure resists the change that would improve it.

## Proposed fix / investigation path

Add no new mixins. When a change touches one of the narrowest ones
(state persistence, the conversation window), turn it into a
collaborator object that receives what it needs in its constructor, and
move its tests to that object's public methods.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; the class and line counts were measured from the imported
> class.
