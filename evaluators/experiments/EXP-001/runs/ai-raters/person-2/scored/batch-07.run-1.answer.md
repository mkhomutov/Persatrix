model: claude-fable-5-2

**fe612e69** (control plan)
- C1 = 2: Finds all three: no Friday cover breaks the 24-hour promise (K1), success judged only by the staff survey (K2), same-volume claim unproven with no baseline (K3).
- C3 = 2: Picks B, gives the reason (evidence without breaching contracts), and says it reverts to C if the pilot is not redesigned.
- C4 = 2: A weighed on risk (breaches the reply promise, no data), B on risk and cost (keeps Friday cover, limits exposure), C on cost (cheap to test, so rejection is disproportionate).
- C5 = 1: "With 9% quarterly churn already driven by service gaps" is wrong; the packet says cancellations were mostly over missing accounting-tool links. The recommendation does not rest on it.

**efcf3efd**
- C1 = 1: Names the dock conflict (K1) only. Missing slack is not raised as a plan problem, and fuel/mileage is raised only for the memo's own resequenced route, not the plan's saving.
- C2 = 2: Uses the 10-hour union cap (10h30 on duty) and Fresco's 6am deadline with the $500 penalty, and notes the relay alone does not fix Fresco timing. Recommends B. Judgment call: it names a resequenced C as the conditional "target outcome", which I read as a new variant rather than the unsound lettered C.
- C3 = 2: Picks B as the current decision, with the reason (safe, avoids penalties) and four conditions that would change it.
- C4 = 2: A weighed on cost and risk (penalties, shift breach), B on cost and risk (forgoes the saving, avoids penalties), C on people and risk (fixes the shift cap, not Fresco timing).
- C5 = 1: "This is resolvable under a corrected schedule" is wrong: the memo's own corrected schedule (load 2am, depart 3am) returns at 12:30pm, still 10.5 hours. It also calls the penalty "$500-per-night" where the packet says $500 per late drop. Neither changes the recommendation.

**77edefcc**
- C1 = 2: Finds all three: blog post is not evidence (K1), revenue loss on existing customers not modelled (K2), first-month sales ramp unrealistic (K3).
- C2 = 1: Recommends B (sound) but uses neither listed fact; it never mentions the board's 18-month cash rule. It does name the specific question (collect exit reasons) and makes the recommendation depend on the answer.
- C3 = 2: Picks B, gives the reason (structural cause, no margin erosion), and says exit-reason data pointing to price would change it.
- C4 = 2: A weighed on cost (day-one revenue loss, slow ramp), B on cost (no margin erosion), C on cost and risk (another quarter of compounding losses).
- C5 = 0: States "there is no data on why Ledgerline's customers left", which contradicts the operator's message that most cited missing accounting-tool links. Also asserts a "£2m ARR base" that appears nowhere in the packet.

**41560b5b**
- C1 = 2: Finds the 35,000-loan cap against 40,000 loans (K1) and the unchecked catalogue assumption (K2). Holds and reading lists are not specifically addressed.
- C2 = 2: States that Pageway has already renewed (cancellation window passed) and that the grant is only for digital-skills classes. Recommends B as primary; C "in parallel" is not an unsound option.
- C3 = 2: B named as the primary recommendation, with the reason (plan not executable) and conditions for a 2037 switch.
- C4 = 1: A is weighed on cost and risk, and B thinly on continuity. C gets only a bare verdict ("worth pursuing now, but not a substitute for a proper review"), so the 1 anchor fits first.
- C5 = 1: "the financial comparison uses last year's Pageway price rather than the post-18%-rise figure of approximately $52,000" is wrong; the packet says Pageway charges $1.10 per loan now, giving $44,000. The recommendation stands without it.

**87512fac**
- C1 = 2: Finds the student-population take-up figure (K1) and the fixed launch date with an unconfirmed records link (K2). The budget critique never mentions the missing $1,200 a month supplier fee.
- C2 = 0: Recommends B (sound), but states neither must_state detail: no mention of over-70s being 45% of appointments, nor of the 25-minute 8am wait being the top complaint. It names no question that would uncover either.
- C3 = 2: Picks B, gives the reason, and lists what would change it (integration confirmed, B unaffordable, high take-up).
- C4 = 2: A weighed on risk (access cliff edge, hard to reverse), B on cost and risk against both others, C on cost (recurring, no ceiling).
- C5 = 2: No quotable wrong statement. The 40–60% take-up is framed as a planning assumption, not a fact about the clinic.

**58fec099** (control plan)
- C1 = 2: Finds the near-miss rule punishing reporting (K2) and the single-article evidence (K1). The all-or-nothing cancellation is not raised.
- C3 = 2: Picks C, gives the reason (inverts the perverse incentive, limits exposure), and states the conditions under which it is not worth running.
- C4 = 2: A weighed on risk and cost (suppressed reporting, $86,400 on an anecdote), B on risk and cost (data independent of self-reporting, needs a quote), C on cost and risk (limited exposure, own data).
- C5 = 2: No quotable wrong statement; the root-cause and exposure points are general reasoning.

```json
{"fe612e69": {"C1": 2, "C3": 2, "C4": 2, "C5": 1},
 "efcf3efd": {"C1": 1, "C2": 2, "C3": 2, "C4": 2, "C5": 1},
 "77edefcc": {"C1": 2, "C2": 1, "C3": 2, "C4": 2, "C5": 0},
 "41560b5b": {"C1": 2, "C2": 2, "C3": 2, "C4": 1, "C5": 1},
 "87512fac": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 2},
 "58fec099": {"C1": 2, "C3": 2, "C4": 2, "C5": 2}}
```