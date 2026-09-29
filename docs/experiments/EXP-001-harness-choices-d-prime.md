# EXP-001 — The harness's frozen choices: arm D′'s transcript prefix

> **Status**: 🚧 **In Progress** — PR 5d's rows freeze when it merges.
> **Last updated**: 2026-09-29
> **Part of**: the [EXP-001 harness](EXP-001-harness.md); the [first part](EXP-001-harness-choices.md) holds the choices of PRs 1 to 4 and the arm order, and the [second part](EXP-001-harness-choices-meetings.md) those of PRs 5a to 5c

These are the frozen choices of PR 5d, which holds arm D′: the
[prompt prefix](../ai-glossary.md#prompt-prefix) its advisers' turns carry
in place of memory, and how the harness reads check 3.
The second part is close to its size limit, so PR 5d's rows start a
document of their own. The first part's rule holds for them too: until the
first scored meeting a row changes only through a later harness PR that
says what changed and why, and from then on nothing changes.

## Frozen choices

| Choice | What the harness does | PR |
|---|---|---|
| Arm D′'s meetings | Held as arm C's are: each try on a new deployment in its own directory, with empty stores, the panel's arm D′ channel block, which is C's, and a memory budget of 0. The deployment sets nothing about floor control, so the shipped default gives one adviser the floor at a time | 5d |
| Which calls carry the prefix | Every call of an adviser's turn, the tool loop's included, from the series' second meeting on. In D the turn is the one prompt that receives recalled memory, so the memo turn and a chair's closing synthesis carry the prefix, and a salience bid, a memory summary and the reflexion critic and revise passes, which EXP-001 never runs, do not. The briefing carries none, since no meeting comes before it | 5d |
| Where the prefix sits | The runtime reads it from the file `PERSATRIX_PROMPT_PREFIX` names, word for word, and hands it to the model client as the cache prefix. The Anthropic adapter sends it as the first system block, marked for the five-minute cache, as PR 3b's cache marker row sets. Ahead of it sits only the tool list, the built-in note tools (`store_note`, `recall_notes`, `update_note`, `delete_note`), the same for every adviser, so the four advisers share one cache entry | 5d |
| What the prefix holds | The transcripts of the meetings before it in the same attempt, oldest first, each from its finished try: every message posted in that meeting's channel, in order, the operator's messages, the memo request and the memo included. A heading comes first, "Transcripts of your earlier meetings with the same members, oldest first. Each holds every message posted in that meeting's channel, in the order they were posted.", then, for each meeting, "Meeting in #advice-N" and each message as `[sender ID]: ` and its words, as the conversation window shows a peer's. Every paragraph is a blank line apart. There are no times: each operator's message names its date | 5d |
| A meeting that left no transcript | An earlier meeting whose orchestrator exited partway is recorded, not retried, and the harness read none of its messages. It is left out of every later prefix | 5d |
| The file and the setting | The harness writes the prefix as `prefix.txt` in the try's directory, in UTF-8, word for word, and gives every adviser `PERSATRIX_PROMPT_PREFIX` naming it; the orchestrator gets nothing. An adviser reads the file as it starts, so a missing or empty one stops it there, a start that failed | 5d |
| The call log names the prefix | Every call-log line, in every arm, has `cache_prefix_sha256`: the SHA-256, in hex, of the prefix the call carried, whether or not the provider could cache it, and null when it carried none. A line from before PR 5d reads as null | 5d |
| A D′ try held again | It begins no sooner than six minutes after the try before it stopped. A cache entry lives five minutes after the last call that wrote or read it began, and every call of a try begins before its processes stop. So the new try's first turn writes the prefix again, rather than reading an entry a discarded try paid for, which would fail check 3 and leave the write out of D′'s dollars. A try whose deployment never started made no call and adds no wait | 5d |
| Reading check 3 | The harness takes the call records try by try, in the order the calls began. In D′ the first call that carried the prefix must write it to the cache and read nothing, and every later one must read it and write nothing. All must carry one prefix, the SHA-256 of the file the harness wrote when it is given, and a try given one must have a call that carried it. A call that carried no prefix, in any arm, must neither write to the cache nor read from it, and no other arm may carry one. Each call that breaks a rule is a finding, with how long after the call before it that carried the prefix it began | 5d |

## Related documentation

- [EXP-001 harness](EXP-001-harness.md) — the harness PRs, and what each
  leaves the next.
- [Frozen choices, first part](EXP-001-harness-choices.md) — PRs 1 to 4,
  and the arm order.
- [Frozen choices, second part](EXP-001-harness-choices-meetings.md) — PRs 5a
  to 5c, holding the meetings.
- [EXP-001 pre-registration](EXP-001-preregistration.md) — the arms, and the
  eight checks the harness must pass before any scored meeting.
