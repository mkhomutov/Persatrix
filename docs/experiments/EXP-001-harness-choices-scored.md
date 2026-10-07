# EXP-001 — The harness's frozen choices: the scored run

> **Status**: 🚧 **In Progress** — PR 6c's rows freeze when it merges ([#1036](https://github.com/mkhomutov/Persatrix/pull/1036)).
> **Last updated**: 2026-10-07
> **Part of**: the [EXP-001 harness](EXP-001-harness.md); the [first part](EXP-001-harness-choices.md) holds the choices of PRs 1 to 4 and the arm order, the [second part](EXP-001-harness-choices-meetings.md) those of PRs 5a to 5c, the [third part](EXP-001-harness-choices-d-prime.md) those of PRs 5d and 5e, the [fourth part](EXP-001-harness-choices-judge.md) PR 6a's, and the [fifth part](EXP-001-harness-choices-practice.md) PR 6b's

These are the frozen choices of PR 6c, which holds the scored run: every
arm's scored series in the order drawn for it, the faults, cap and window
that bound it, and the scored judging.
[Pre-registration §3](EXP-001-preregistration.md#3-running-it) fixes the
order, the attempts and failures, the dollars and the caps; the rows below
are what it leaves open. The first part's rule holds for them too: until the
first scored meeting a row changes only through a later harness PR that says
what changed and why, and from then on nothing changes.

## Frozen choices

| Choice | What the harness does | PR |
|---|---|---|
| The scored run's directory | One directory, kept whole and outside the repository, holds the whole scored run, every window in it. `run.json` keeps the model alias its meetings are held on and each window's record. A start on another alias is refused, so none mixes meetings held offline and on the provider. One run at a time holds the directory: a second started meanwhile is refused | 6c |
| Windows | The run is held in windows, each in a directory of its own, `window-N`, which holds every series from series 1. A window's seven days open as its first try begins: the harness records the moment and says when they close. Before each try it asks whether they have passed, and a try that would begin seven days or more after the window opened is not begun. A series not then held to the end in every arm is not held, and the window's meetings end. A series lost this way counts as a dropped one does. Part 2 §6 makes a run incomplete only by a spend cap, a third harness fault or fewer than four series, so with four or more kept the run goes on to its scoring | 6c |
| The order | Within a window, series 1 to 5 in turn, and each series' arms one at a time, in the order drawn for it ([the arm order](EXP-001-harness-choices.md#the-arm-order)). Each arm's series is a pair, held as PR 6b's rows hold one: each try kept as it ends, and a pair held to the end read back when the run starts again. A pair stopped partway by Ctrl-C, a hangup or a crash is set aside whole into the window's `interrupted` and held again from its briefing. A crash is no harness fault, so it closes no window | 6c |
| A dropped series | A series dropped in any arm is dropped from every arm's comparisons, so the arms after that arm in the series' order never hold it, and none of its answers is gathered. Once two series are dropped, fewer than four can still be kept: the run stops there, incomplete, and holds no later series | 6c |
| A harness fault | A fault while the meetings are held, or one in the scored judging, stops the run and closes its window; `run.json` records it with its message. The judge's choices make an answer it cannot read a harness fault in the scored judging. Every scored output so far is discarded: the window's directory stays whole, the stopped series' tries included, and none of it is ever gathered or judged; the result publishes it. A start after the fault is refused unless `--fixed-by` names the reviewed PR that fixed it, which `run.json` keeps. The next window then holds every series again from series 1, its seven days opening again. A third fault ends the run, incomplete: a later start writes the report and holds nothing, and a fix named then is refused | 6c |
| The $150 cap | Before each try, the harness prices every arm call in the run's call logs with the fixed table: every window, attempt and try, counted in an arm's dollars or not, discarded or not, the pairs set aside included. A set-aside log it cannot read, such as one a crash stopped mid-line, is named in the report and its calls are left out. Once the total reaches $150 no try begins, so the try that crossed the cap is the last; the window's meetings end and the run is incomplete. A call on a model the table does not list stops the run as a harness fault, since PR 1's row refuses to price it at zero. Judge calls never count: they have their own cap | 6c |
| A series kept | A series is kept when every arm held it to the end in the window and none dropped it. Only the kept series' answers are gathered, each from its meeting's finished try, and drawn into packets once, in the window's directory, as PR 6b's rows draw them: each person's packets in a file of their own, the seal kept apart. Nothing is drawn when the window's meetings ended with the run incomplete | 6c |
| The scored judging | The judge scores the window's packets once, in the order drawn for it, as batch `scored` in the window's `judging/scored`. Each window's judging is a batch of its own, under its own $25 cap, since PR 6a's cap reads only its batch's calls: a discarded window's judge calls are reported, and count toward no later window's cap. A provider error the judge's retries do not clear stops the run, and a later start resumes the batch. Offline, nothing is judged | 6c |
| Dollars per plan | For each arm and each series it held to the end in the window, as PR 5b's row counts them: the calls of each meeting's finished try, in the attempt that finished, at the briefing and the four plans, divided by four. The report gives them at the fixed table, and with bids and memory summaries repriced at `claude-haiku-4-5` as PR 2's row reprices them, for part 2 §7 | 6c |
| The 21 days | Part 2 §4's scoring is due 21 days after the moment the window's meetings ended, which `run.json` keeps. The report names that date once the run is complete | 6c |
| The scored run's report | `report.json`, and the same in plain words in `report.txt`, written whenever the run stops or ends. It says how the run stands and why: complete; incomplete, by the cap, by fewer than four series kept, or by a third fault; or stopped by a fault until its fix merges. For each window: when it opened, the fix it started after, the fault that closed it, how its meetings ended and its real spend. For the window whose meetings ended: the series kept, dropped and not held; each try's close, failures and whether its answer is missing, with no memo or transcript text; and dollars per plan. Then real spend against $150, the scored judging's calls and spend against $25 and the packets it never judged, and every call's tokens by model, totalled as the practice report totals them. It never reads the seal, so it names no arm's scores | 6c |
| Offline | `--provider offline` holds the scored series on the offline mock provider, at no cost, to rehearse the run before any scored meeting. Nothing is priced, so the cap never stops it, and nothing is judged. Its directory keeps the mock alias, so the provider's run can never go on in it | 6c |

## Related documentation

- [EXP-001 harness](EXP-001-harness.md) — the harness PRs, and what each
  leaves the next.
- [The harness, second part](EXP-001-harness-run.md) — the judge, the
  practice run and the scored run, and how to hold each.
- [Frozen choices, first part](EXP-001-harness-choices.md) — PRs 1 to 4,
  and the arm order.
- [Frozen choices, second part](EXP-001-harness-choices-meetings.md) — PRs 5a
  to 5c, holding the meetings.
- [Frozen choices, fifth part](EXP-001-harness-choices-practice.md) — PR 6b,
  the practice run.
- [EXP-001 pre-registration](EXP-001-preregistration.md) — the order, the
  attempts and failures, and the caps.
