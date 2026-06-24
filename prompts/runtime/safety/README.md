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
| `chair-escalation.md`         | [`agents/persona_runtime/prompt_assembly.py`](../../../agents/persona_runtime/prompt_assembly.py) | Per-event (not system prompt): the chair-stall-escalation forced-turn framing, rendered ahead of the re-delivered stimulus when the `chair_escalation` wire marker is set — synthesize + vote, or call on the member best placed (amendment §C item 2, v0.3.8). |
| `chair-escalation-resynthesize.md` | [`agents/persona_runtime/prompt_assembly.py`](../../../agents/persona_runtime/prompt_assembly.py) | Per-event sibling of `chair-escalation.md`: the synthesize-only framing for the SECOND forced turn, selected when the `chair_escalation_resynthesize` refinement rides alongside `chair_escalation`. Drops the hand-off outcome (the move that just provably reached no floor-capable member) and forces the end-vote (ISSUE-0099, v0.3.8). |
| `memory-tool-usage.md`        | [`agents/persona_runtime/prompt_assembly.py`](../../../agents/persona_runtime/prompt_assembly.py) | Persona system prompt — nudges the LLM to actually call memory tools.  |
| `reflection-nudge.md`         | [`agents/tools/builtin.py`](../../../agents/tools/builtin.py)    | Periodic auto-reflection trigger appended to the next agent turn.      |
| `episode-summarizer.md`       | [`agents/memory/episodic_retention.py`](../../../agents/memory/episodic_retention.py) | System prompt for the episodic-summary compression LLM call.           |
| `salience-bid-system.md`      | [`agents/salience_bid.py`](../../../agents/salience_bid.py)      | System prompt for the RFC 0030 Tier B `fast`-model salience bid — "decide ONLY whether to speak". Templated: `{persona_name}` / `{persona_role}` (the only braces). |
| `salience-bid-user.md`        | [`agents/salience_bid.py`](../../../agents/salience_bid.py)      | User message for the Tier B salience bid — the speak/score instruction + the ISSUE-0097 opening-round calibration (an unanswered direct question to the room is itself salient). Templated: `{note_tail}` only (the inbound message + transcript are concatenated, never formatted in). |
| `salience-bid-addressing-self.md`  | [`agents/salience_bid.py`](../../../agents/salience_bid.py) | Tier B bid NL-addressing nudge (RFC 0030 Tier B PR 3, v0.3.8) — the persona is invited by name; lean toward speaking. |
| `salience-bid-addressing-other.md` | [`agents/salience_bid.py`](../../../agents/salience_bid.py) | Tier B bid NL-addressing nudge (RFC 0030 Tier B PR 3, v0.3.8) — someone else is invited by name; defer unless genuinely novel. |
| `salience-bid-reasoning-system.md` | [`agents/salience_bid.py`](../../../agents/salience_bid.py) | System prompt for the RFC 0051 structured silence verdict (`reasoning.mode: bid`/`plan`) — "decide ONLY whether to post". Selected by [`agents/salience_deliberation.py`](../../../agents/salience_deliberation.py) `system_snippet`. Templated: `{persona_name}` / `{persona_role}`. |
| `salience-bid-reasoning-user.md` | [`agents/salience_bid.py`](../../../agents/salience_bid.py) | User message for the RFC 0051 structured silence verdict — the `should_post`/`reason_code`/optional `reason_note` grammar. Selected by [`agents/salience_deliberation.py`](../../../agents/salience_deliberation.py) `user_snippet`. Templated: `{note_tail}` only. |
| `salience-bid-plan-system.md` | [`agents/salience_bid.py`](../../../agents/salience_bid.py) | System prompt for the RFC 0051 PR 3 `reasoning.mode: plan` rung — decide, then privately plan the post. Selected by [`agents/salience_deliberation.py`](../../../agents/salience_deliberation.py) `system_snippet`. Templated: `{persona_name}` / `{persona_role}`. |
| `salience-bid-plan-user.md` | [`agents/salience_bid.py`](../../../agents/salience_bid.py) | User message for `reasoning.mode: plan` — the verdict lines plus the `CompositionPlan` fields (intent / key_points / addressed_to / avoid_restating), parsed by [`agents/persona_runtime/deliberation_plan.py`](../../../agents/persona_runtime/deliberation_plan.py). Templated: `{note_tail}` only. |

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
