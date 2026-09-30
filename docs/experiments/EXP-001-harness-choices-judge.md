# EXP-001 — The harness's frozen choices: the judge

> **Status**: 🚧 **In Progress** — PR 6a's rows freeze when it merges ([#1023](https://github.com/mkhomutov/Persatrix/pull/1023)).
> **Last updated**: 2026-09-30
> **Part of**: the [EXP-001 harness](EXP-001-harness.md); the [first part](EXP-001-harness-choices.md) holds the choices of PRs 1 to 4 and the arm order, the [second part](EXP-001-harness-choices-meetings.md) those of PRs 5a to 5c, and the [third part](EXP-001-harness-choices-d-prime.md) PR 5d's

These are the frozen choices of PR 6a, which adds the LLM judge: the third
of the three raters in [part 2 §2](EXP-001-preregistration-scoring.md#2-raters-and-blinding)
of the pre-registration, `claude-opus-5` with its default settings, one pass
per memo and per recall check, its spend capped at $25. The documents fix
the model, the prompts in `rubric.yaml` and the cap; the rows below are what
they leave open. The first part's rule holds for them too: until the first
scored meeting a row changes only through a later harness PR that says what
changed and why, and from then on nothing changes.

## Frozen choices

| Choice | What the harness does | PR |
|---|---|---|
| A packet as a rater reads it | One text for every rater, the judge included. A memo's packet is `Organisation:` and the organisation's line; then each operator message, oldest first, headed `--- Operator message N of M ---`, the last one `, the request the memo answers`; then `--- Answer key ---`: the plan's problems by their IDs, each earlier fact by its ID with its implication and `Must state:` and one line per detail, and the options those facts make unsound, or `none.` when the key lists none; then `--- Memo ---` and the memo as blinded and cut. A recall packet is the organisation, then the key, each answer as `R1, question 1:` and its answer with `Must include:` and one line per item, then `--- Reply ---` and the reply. Sections are a blank line apart | 6a |
| The judge's prompts | The rubric's `memo_prompt` and `recall_prompt`, word for word, with `{criteria}` and `{rule}` replaced exactly, as the rubric's templating note says, and every other brace left as it is. `{rule}` is the rubric's recall rule. `{criteria}` is each memo criterion in the rubric's order, a blank line apart: `C1: ` and its name, `Reads: ` and what it reads when the rubric says, each anchor as `0: `, `1: `, `2: ` and its words, and `Note: ` and its note when it has one. A rubric whose prompt does not hold its placeholder exactly once is refused. The filled prompt is the system prompt and the packet the one user message | 6a |
| The judge's call | `claude-opus-5` with no alias, no tools, no cache prefix and at most 16 000 output tokens, which keeps a request that is not streamed inside the provider library's time limit. The request sets no thinking and no effort, so the model's defaults run: adaptive thinking, whose tokens count toward that limit and are billed as output. The harness passes a temperature of 1.0, the API's default, and the adapter sends this model none, since it takes none. There is no fallback model: a refusal is the judge's answer, not re-served by another model | 6a |
| One pass | Each packet gets one answer. A call that fails with a provider error, as the arms' retries read it, gave no answer, so it is tried again after 60, 300 and 900 seconds; the error that fails a fourth time stops the batch, and a later run resumes it. An answer is written to the batch's `replies.jsonl` as it arrives, before it is read, and a packet with an answer there is never asked again, whatever the answer says. The kept line holds the provider's own stop reason, so a refusal is kept as `refusal`, and a SHA-256 of what was asked: the model, the token limit, the temperature, the prompt and the packet text. One run at a time holds a batch's directory, and a second is refused. A call that fails with any other error is a harness fault, and so is a batch that cannot keep one pass: a second kept answer for a packet, a packet listed twice, a call the log shows answered with no answer kept, or a kept answer asked another way, since a batch judged under a changed call starts in a directory of its own | 6a |
| Reading an answer | The one JSON object in the answer: each `{` outside an object already read is tried in turn, so a code fence, a line of prose around it or a brace in that prose does no harm, while two objects, or a key given twice, are unreadable. A memo's answer needs `C1` to `C5`, each the whole number 0, 1 or 2, except that `C2` is null exactly when the key lists no earlier facts, as on a control plan. `C1_problems_found` and `C2_facts_used` must be lists of IDs and `reasons` a map of sentences when given, and a null there, or as a reason, counts as not given; they are kept for the report and change no score. A recall answer needs `right` or `wrong`, in any case, for exactly the key's questions, each named by its ID in any case. An answer cut off at the token limit, a refusal, one with no such object, or one that breaks these rules is unreadable: judging stops on a harness fault, and the answer stays kept. Before any scored meeting, a reviewed fix to the reader reads it again without a second pass. In the scored judging, the fault is a harness fault under pre-registration §3: the fix goes through a reviewed PR, and every scored output so far is discarded | 6a |
| The judge's call log | Each batch of packets, the practice judging or the scored one, has a directory of its own and its own `calls.jsonl`. Every call is logged with the runtime's own purpose `judge` and the tags `rater` (`judge`), `batch`, `packet` (its ID), `kind` (`plan`, `control` or `recall`) and `try`, and never with the packet's arm, series or meeting, so the log keeps the seal. A packet's `try` counts on from its last try in the log, so a resumed batch never reuses one. Read back, a judge call's record has an empty arm, the batch as its series and the packet's ID as its meeting. A line without those tags or that purpose is refused, and an arm's call log refuses a `judge` line. A line for another batch or for a packet not in the batch, or one the harness cannot read or price, stops judging as a harness fault | 6a |
| The $25 cap | Before each call the harness prices every call in the batch's log so far, the calls of an earlier run of the batch included, with the fixed table. Once they reach the cap, no packet left is asked, and the batch reports the cap reached and which packets were never judged. So the call that crosses the cap is the last. The practice judging has its own directory, so none of its calls counts toward the scored judging's cap. This narrows PR 1's "Judging spend" row, which counted every `judge` call: the cap now reads only the batch's own calls | 6a |

## Related documentation

- [EXP-001 harness](EXP-001-harness.md) — the harness PRs, and what each
  leaves the next.
- [Frozen choices, first part](EXP-001-harness-choices.md) — PRs 1 to 4,
  and the arm order.
- [Frozen choices, second part](EXP-001-harness-choices-meetings.md) — PRs 5a
  to 5c, holding the meetings.
- [Frozen choices, third part](EXP-001-harness-choices-d-prime.md) — PR 5d,
  arm D′'s transcript prefix.
- [EXP-001 pre-registration, part 2](EXP-001-preregistration-scoring.md) —
  the raters, blinding and the judge's cap.
- [Scoring rubric](../../evaluators/experiments/EXP-001/rubric.yaml) — the
  anchors and the judge's prompts.
