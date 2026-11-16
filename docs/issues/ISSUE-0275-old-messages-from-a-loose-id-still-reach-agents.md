---
id: ISSUE-0275
summary: "A store written before the participant id rule was tightened can hold messages from an id such as `User_1`. A new reply threaded under such a message is stored with 201 and then refused by every persona, because the orchestrator sends the old message's sender along with it. On each start, every persona also drops such messages from its catch-up, with one warning for each."
status: open
severity: low
area: channels
created: 2026-11-16
refs:
  - internal/channels/router.go
  - internal/channels/grpc_dispatcher_proto.go
  - agents/channel_validation.py
  - agents/channel_catchup.py
  - docs/issues/ISSUE-0209-participant-id-rule-differs-between-go-and-python.md
  - https://github.com/mkhomutov/Persatrix/pull/1136
---

# ISSUE-0275: Old messages from a loose id still reach agents

## Summary

Since the fix for
[ISSUE-0209](ISSUE-0209-participant-id-rule-differs-between-go-and-python.md)
the orchestrator refuses a participant id that does not fit the rule
where it enters, and keeps what an earlier orchestrator stored. Stored
messages from an id such as `User_1` therefore stay in their channels'
histories. Two paths still carry such an id to an agent.

## Context

**A reply in a thread under an old message.** When a message is a reply
in a thread, the orchestrator looks up who wrote the message it replies
to (`resolveThreadParentSenderID` in `internal/channels/router.go`) and
sends that id with the event, as `thread_parent_sender_id`. The agents'
check (`validate_channel_message_event`) refuses an event whose
`thread_parent_sender_id` does not fit the rule. So when a member with a
good id replies under an old message from `User_1`:

- the reply is stored and the publisher gets `201`;
- every persona refuses the event:
  `thread_parent_sender_id is not a valid participant id: 'User_1'`;
- the orchestrator logs `channels: dispatch failed` for each persona, and
  nobody answers.

Run against a real orchestrator on a store that an earlier build wrote,
with a stand-in agent that runs the agents' own check.

**Catch-up at an agent's start.** A persona reads each of its channels'
recent history when it starts (`agents/channel_catchup.py`) and checks
each row as it checks a live event. A row whose sender, or whose mention,
does not fit the rule is dropped with the warning `channels: catch-up
dropped malformed row … reason=sender_id is not a valid participant id`.
The persona's record of that conversation then lacks the message. This
is one warning for each such row, each channel and each persona, on
every start, for as long as the row is within the history a catch-up
reads. It was so before the fix too. Read, not run.

A third path is closed already: a message's text that names `@User_1` is
no longer turned into a mention.

**A persona that mentions such an id.** One more effect runs the other
way. A persona never received a message from `User_1`, so it does not
know the id from the sender of anything. It can still read the id in
another member's text ("thanks @User_1") and put it in the `mentions` of
its own reply. The orchestrator now refuses that reply with `400`, as it
refuses any message that mentions such an id, and the reply is not
posted; the agent logs the failed publish. Before the fix the reply was
stored, people in the channel saw it, and the other personas refused it.
Read, not run: no model was called.

## Impact

Bounded to a store that already holds messages from such ids. A thread
under one of them is a dead end that looks alive: replies are accepted
and never answered. The catch-up warnings are noise that never ends by
itself.

## Proposed fix / investigation path

Three options for the thread, for the maintainer to choose from:

1. **Leave the old sender out of the event.** Send an empty
   `thread_parent_sender_id` when the parent's sender does not fit the
   rule. An agent reads the field for one purpose, to see that a reply
   is under its own message, and an id outside the rule is never an
   agent's, since agent ids have always been held to the rule at
   registration. So nothing an agent could act on is lost. One check in
   one function.
2. **Refuse the reply at the door**, with a `400` that says the thread's
   first message is from an id the rule refuses. The thread stays
   readable and takes no more replies.
3. **Leave it**, and say so in the guide, as it does now.

For the catch-up rows the choices are the same in kind: have the
orchestrator leave such rows out of what an agent's catch-up reads, or
leave the agents' warnings as the record.

For a persona's reply that mentions such an id: drop that one mention
from an agent's publish and store the rest, or keep the refusal.

## Notes

> 2026-11-16 — found while fixing
> [ISSUE-0209](ISSUE-0209-participant-id-rule-differs-between-go-and-python.md)
> ([#1136](https://github.com/mkhomutov/Persatrix/pull/1136)).
> Left open on purpose: the maintainer's choices for that fix were to
> refuse at the door and to warn at start, and this needs a third thing.
> Severity proposed (low: it needs a store from before the fix and a
> reply under one of its old messages); not ruled by the maintainer.

> 2026-11-16 — the maintainer's decision for the thread: leave it as it
> is (option 3). A reply in a thread under an old message from such an
> id stays stored and unanswered, and the channels guide says so
> ("Participant ids"). Options 1 and 2 are not taken, and nothing in the
> orchestrator changes for it. This issue stays open as the record of
> that known gap. The decision was about the thread: the rows a persona
> drops at catch-up, and a persona's reply that mentions such an id,
> stay as described above, with no choice made for them.
