---
id: ISSUE-0174
summary: "A restart can write one channel conversation to memory twice. A conversation written before the restart, closed live or (since ISSUE-0172) at the stop, is stored under a random interaction id. On the real clock the next start's catch-up replays the channel's last 50 messages and derives the same conversation again under a digest id, because its guard against deriving twice matches only an earlier replay's digest. The persona then holds two episodes of one conversation, its facts are extracted twice, and one extra summary is paid for."
status: open
severity: low
area: memory
created: 2026-09-28
refs:
  - https://github.com/mkhomutov/Persatrix/pull/1014
  - docs/issues/ISSUE-0172-stopping-agent-drops-open-conversations.md
  - agents/persona_runtime/replay_identity.py
  - agents/memory/_replay_bookkeeping.py
  - agents/channel_catchup.py
  - docs/manual-tests/MT-MEMORY-GROUP-TENANT-001.md
  - docs/rfcs/0020-interaction-lifecycle.md
---

# ISSUE-0174: A restart can write one channel conversation to memory twice

## Summary

When a persona starts, its catch-up reads each channel's last 50 messages
and, where it knows whose they were, writes them to memory as a replayed
conversation. Its guard against writing a window twice compares a digest
of the messages with the ids of earlier replayed conversations. A
conversation the persona already wrote while it was running carries a
random id instead, so the guard cannot see it, and the same messages are
written a second time.

This happened before for conversations closed while the persona ran; the
manual test MT-MEMORY-GROUP-TENANT-001 records it. Since
[ISSUE-0172](ISSUE-0172-stopping-agent-drops-open-conversations.md), a
stopping persona also writes the conversations it still holds open, so
those are written twice too.

## Context

Found in the review of the ISSUE-0172 fix
([#1014](https://github.com/mkhomutov/Persatrix/pull/1014)). A two-start
test on one store gave one episode when the stop wrote nothing and two
when it wrote the open conversation: `shutdown` under a random id, then
`catchup_complete` under a `replay-` digest id. The duplicate needs the
real clock, an orchestrator on channel-store v12 or later, and the
conversation still inside the next start's window. Where the replay cannot
read the window again (a clock anchored after it, as EXP-001's arms use,
or a conversation pushed out of the last 50), the stop's write is the only
one, which is why ISSUE-0172 keeps it.

Removing duplicates while the messages come in was ruled out for the
cross-start case: the tracker lives in memory, so a new start knows
nothing of the last one (`_replay_bookkeeping.py`).

## Impact

After each restart, recall can return two episodes of one conversation,
facts are extracted from it twice, a direct message counts twice in the
relationship tier, and one extra summary is paid for. It is bounded: one
extra copy per conversation per restart, and only inside the window.

## Proposed fix / investigation path

- Record, when a conversation is written live, what the next catch-up needs
  to recognise it: a per-channel mark of the last message already written
  (the watermark of RFC 0011 open question 8), or the digest the replay
  would compute for those messages.
- Or let the replay skip messages that fall inside a live episode of the
  same speaker and channel.

## Notes

> 2026-09-28 — filed from the review of #1014 (finding F-1).
