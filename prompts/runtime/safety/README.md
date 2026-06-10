# Safety & Behavior Prompt Snippets

Short prompt fragments loaded by the agent runtime via
[`agents.prompt_loader.load_snippet`](../../../agents/prompt_loader.py).

## Current snippets

| File                          | Loaded by                                                        | Purpose                                                                |
| ----------------------------- | ---------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `user-message-delimiters.md`  | [`agents/persona_runtime/prompt_assembly.py`](../../../agents/persona_runtime/prompt_assembly.py) | Persona system prompt — `<\|user_message\|>` delimiter contract.       |
| `external-data-handling.md`   | [`agents/persona_runtime/prompt_assembly.py`](../../../agents/persona_runtime/prompt_assembly.py) | Persona system prompt — `<external_data>` envelope contract (RFC 0009). |
| `reply-discretion.md`         | [`agents/persona_runtime/prompt_assembly.py`](../../../agents/persona_runtime/prompt_assembly.py) | Persona system prompt — silence is a valid outcome on group channels; DMs always reply. |
| `conversational-pacing.md`    | [`agents/persona_runtime/prompt_assembly.py`](../../../agents/persona_runtime/prompt_assembly.py) | Persona system prompt — match the length and register of the inbound message. |
| `peer-conversation-voice.md`  | [`agents/persona_runtime/prompt_assembly.py`](../../../agents/persona_runtime/prompt_assembly.py) | Persona system prompt — frame the persona as a colleague among peers in group channels, not an assistant serving a user (RFC 0030 relevance amendment, v0.3.7). |
| `end-interaction-vote.md`     | [`agents/persona_runtime/prompt_assembly.py`](../../../agents/persona_runtime/prompt_assembly.py) | Persona system prompt — the RFC 0030 Layer 4 end-of-discussion vote vocabulary: the JSON action form and when voting is appropriate (producer plan PR 2, v0.3.8). |
| `memory-tool-usage.md`        | [`agents/persona_runtime/prompt_assembly.py`](../../../agents/persona_runtime/prompt_assembly.py) | Persona system prompt — nudges the LLM to actually call memory tools.  |
| `reflection-nudge.md`         | [`agents/tools/builtin.py`](../../../agents/tools/builtin.py)    | Periodic auto-reflection trigger appended to the next agent turn.      |
| `episode-summarizer.md`       | [`agents/memory/episodic_retention.py`](../../../agents/memory/episodic_retention.py) | System prompt for the episodic-summary compression LLM call.           |

## Contract

- File names are kebab-case basenames; `load_snippet("name")` reads
  `prompts/runtime/safety/name.md`.
- A single trailing newline is stripped on load so editor convention
  doesn't drift the bytes the runtime sees from what was previously
  inlined in source.
- Paths must resolve under this directory; `..` traversals, absolute
  paths, and symlink targets that escape the subtree are rejected.
- The loader is cached, so production reads are one-shot per snippet.

To add a new snippet, drop a markdown file here and call `load_snippet`
from the runtime. Keep snippets short and behavior-shaping; long prose
belongs in the persona section files (forthcoming, see
[`prompts/runtime/persona/sections/`](../persona/sections/)).
