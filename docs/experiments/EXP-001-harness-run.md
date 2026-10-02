# EXP-001 — The harness, second part: the judge and the practice run

> **Status**: 🚧 **In Progress** — PRs 6a and 6b merged ([#1023](https://github.com/mkhomutov/Persatrix/pull/1023), [#1024](https://github.com/mkhomutov/Persatrix/pull/1024)); the practice run held on 2026-10-02 confirmed the quiet-spell gap PR 5d left, and PR 5e closes it.
> **Last updated**: 2026-10-02
> **Part of**: the [EXP-001 harness](EXP-001-harness.md), whose first part lists every harness PR and what PRs 1 to 5d leave the next

The [first part](EXP-001-harness.md) reached its size limit, so the account
of the judge and the practice run moved here as it was. What the practice
run then showed follows it, and so does PR 5e, which the run asked for.

## The judge

PR 6a adds the judge, the third rater, which scores the blinded packets PR 2
builds. It reads each packet in the same words as the two people, and
answers with the JSON the rubric's prompts ask for. Each packet gets one
answer. A provider error is tried again, since no answer came, but an answer
that came is kept and never asked for again, and one the harness cannot read
stops judging as a harness fault. The judge's calls go to a call log of
their own, with a purpose of their own, tagged with the packet's ID and
never its arm, and the $25 cap is read from that log before each call.

PR 6a leaves three things for PR 6b. Its run judges the practice memos in a
batch of their own, so their calls never count toward the scored judging's
cap, and gives the judge the order `rater_orders` draws for it. The practice
run shows whether the judge answers every packet readably within 16 000
output tokens, its default thinking included. An answer cut off, a refusal,
or a control plan scored on C2 would stop judging, and a fix to the reader
or to the call is a harness PR before any scored meeting. And before any
scored meeting, the run projects the scored judging's spend from the
practice batch's calls: 100 times a memo packet's mean cost plus 25 times a
recall packet's. At the token limit a call costs about $0.41, so the 125
scored calls fit the $25 cap only if they average under about 7 600 output
tokens, thinking included. A projection over the cap needs a change to the
call before the first scored meeting, since the caps freeze then.

## The practice run

PR 6b holds the practice run. Each arm named holds the practice series in
turn, and each try is kept as it ends, so a run started again goes on where
it stopped. The harness then draws the packets, once: each person's packets
go to a file of their own, and the seal is kept apart. The judge scores
them as batch `practice`, and the harness writes a report of what the run
shows:

- check 3, naming any memo turn that wrote the prefix again after a quiet
  spell outlasted the cache entry;
- each D′ discussion's tokens against the cost close, and projected to
  the five transcripts a scored series' recall check carries;
- what each meeting recorded, and the judge's marks on each arm's recall
  check, which D's chair can answer only from memory (check 2);
- the judge's largest answer against its token limit, and the scored
  judging's projected spend;
- every call's tokens by model, to compare with the provider's usage
  report.

To hold it, with the orchestrator built (`make build-orchestrator`), a key
used by nothing else in `ANTHROPIC_API_KEY`, and a directory outside the
repository, since memos and transcripts stay out of it:

```bash
python -m evaluators.exp001 practice ~/exp001/practice-1 --provider anthropic
```

`--provider offline` holds the meetings on the mock provider at no cost,
and judges nothing. PR 6b leaves the scored run to PR 6c: every arm's series
in its drawn order, the faults and dollars PR 5b names, the $150 cap, which
every scored attempt counts toward, discarded or not, and the seven-day
window.

PR 6b's review adds one thing, its finding F-8. The report projects the
scored judging's spend from the practice packets, but a scored memo packet's
input is about 28% longer. That adds only about $0.24, yet longer packets
may also take more thinking, which practice packets cannot show. PR 6c
settles the projection at the scored packets' own sizes, before the first
scored meeting, when the caps freeze.

## What the practice run showed

The practice run was held on the real provider on 2026-10-02, every arm in
turn, after the fixes for
[ISSUE-0159](../issues/ISSUE-0159-episodic-score-inverts-bm25.md) and
[ISSUE-0163](../issues/ISSUE-0163-withheld-episodes-reinforced-before-the-gate.md)
had merged. All 20 meetings were held at their first try, with no provider
error. The run was stopped once, between arms C and D, and started again in
the same directory; it went on with arm D and held nothing twice. The
report showed:

- **Check 3** named three calls, one in each D′ meeting after the briefing,
  and each was the memo turn. Each of those discussions closed by its idle
  window, so each memo turn began 610 to 626 seconds after the call before
  it that carried the prefix, and wrote the prefix again (1 588, 5 656 and
  10 061 tokens) instead of reading it: the gap PR 5d said the run should
  confirm.
- **Check 2** held: D's chair answered all three of the recall check's
  questions from memory, and D′'s chair from its prefix; A, B and C, which
  carry nothing from the briefing, answered none.
- **The cost close** is far off: the largest D′ discussion came to 131 255
  tokens, projected to 233 063 with five transcripts, against 1 776 000.
- **The judge** answered all 15 packets readably, the largest answer using
  1 279 of its 16 000 output tokens, and the scored judging is projected at
  $3.83, within its $25 cap.
- **The call log** totalled $6.34: $5.95 for the arms' 528 calls, all on
  `claude-sonnet-4-6`, and $0.39 for the judge's 15 on `claude-opus-5`.

## PR 5e: keeping arm D′'s cache entry alive

PR 5d named two ways to close the gap: a longer-lived cache, which changes
the fixed price table and so needs an amendment, or a harness that keeps
the entry alive. PR 5e takes the second, so the price table stands.

From the series' second meeting on, the chair alone gets one more setting,
`PERSATRIX_PROMPT_PREFIX_KEEPALIVE`. Its process looks at the try's call
log every 10 seconds. Once no call that carried the prefix has begun for
four minutes, it sends a [prefix keep-alive](../ai-glossary.md#prefix-keep-alive):
a call that offers a turn's tools, carries the prefix and asks for no
output. The provider reads the entry, which keeps it for another five
minutes, and writes and answers nothing, so the call costs a read of the
prefix and a few hundred input tokens. A ten-minute quiet spell takes two,
and the memo turn after an idle close reads the prefix as the turns before
it did. One keep-alive serves the room, since the four advisers share one
entry, and the chair's process is up for the whole meeting.

A keep-alive is a call like any other. The call log names its purpose,
`keepalive`, and it is priced and counts in D′'s dollars per plan, since it
is part of how D′ carries its transcripts. It takes no lease, so it is no
part of the discussion's tokens against the cost close. One that fails, or
has not answered after 30 seconds, is tried again at the next look while
the entry may still live, and holds no meeting again: at worst the next
turn writes the prefix again, which check 3 names. Nothing is sent before
the first call that carried the prefix has answered, so that call still
writes the entry, and nothing once the entry's five minutes have passed.
The runtime half sits beside the call log, in `agents/prefix_keepalive.py`,
since it reads real time, as the provider's cache does; the persona runtime
reads only agent time.

## Related documentation

- [EXP-001 harness](EXP-001-harness.md) — the first part: every harness PR,
  and what PRs 1 to 5d leave the next.
- [Frozen choices: arm D′'s transcript prefix](EXP-001-harness-choices-d-prime.md)
  — the choices of PRs 5d and 5e.
- [Frozen choices: the judge](EXP-001-harness-choices-judge.md) — the choices
  of PR 6a.
- [Frozen choices: the practice run](EXP-001-harness-choices-practice.md) —
  the choices of PR 6b.
- [EXP-001 pre-registration](EXP-001-preregistration.md) — the arms, and the
  eight checks the harness must pass before any scored meeting.
