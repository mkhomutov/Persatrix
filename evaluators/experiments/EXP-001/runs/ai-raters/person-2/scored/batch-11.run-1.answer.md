model: claude-fable-5-2

**5456e3c4 (Brookside, second treatment room; memo picks C)**
- C1 = 2: finds K1 (20% more appointments inside existing nurse hours, untested) and K3 (cupboards hold a third of the supplies). Misses equipment (K2).
- C2 = 0: C is a sound option, but the memo uses neither listed fact (landlord's three-month approval, two contracts ending 31 December) and names no specific question that would uncover either.
- C3 = 2: picks C, gives the reason (plan not ready to approve) and four confirmations that would switch it to A.
- C4 = 2: A weighed on cost and risk ($30 000 on unverified assumptions), B on recurring evening pay, C on timing (December start, no buffer before February).
- C5 = 0: states flatly that December is "the clinic's busiest month" and that the staffing assumption "has not been agreed with the nursing team"; no operator message said either.

**5ea8c48a (Ledgerline, billing rebuild; memo picks C)**
- C1 = 2: finds all three (production-only testing, card re-entry, the $0.30 fee erasing the 28% saving).
- C2 = 0: recommends C, which the key marks unsound; never mentions the engineers leaving on 14 November or the Paystride exclusivity.
- C3 = 2: picks C, gives the reason (keeps the rebuild, removes the largest risks) and conditions that would change it.
- C4 = 2: A, B and C each weighed on risk with reasons from the request (compounded risks; fragile engine left in place; largest risks removed).
- C5 = 2: the arithmetic checks ($1.16 against $1.14, about 1.7%), and the churn figures are marked as a scenario; no quotable wrong statement.

**9e88baf9 (Kestrel recall)**
- R1 wrong: "does not know".
- R2 wrong: "does not know", offers a general rules framework instead of 10 hours including loading.
- R3 wrong: "does not know".
- R4 wrong: "does not know".

**5af89a81 (Eastvale, evening opening; memo picks C)**
- C1 = 2: finds all three (30 cards are weak evidence, $46 000 is about half a year, utilities and security left out).
- C2 = 0: recommends C, which the key marks unsound, and treats the staffing freeze as an open question for HR.
- C3 = 2: picks C, gives the reason (limits exposure, produces demand data) and makes it depend on confirmation that new posts are allowed.
- C4 = 2: A weighed on the notice deadline and cost, B on the risk of committing without demand evidence, C on financial exposure.
- C5 = 1: "notice needed to be served by approximately 4 October" is wrong; 60 days before 3 November is about 4 September. The conclusion that the deadline has passed still holds.

**000c2bc0 (Linden Loaf, morning rush; memo picks C)**
- C1 = 2: finds all three ($2 112 not $1 056, no data for 6 to 7am, earlier opening does not fix the 7 to 9am queues).
- C2 = 2: states and applies both facts (head baker leaves if overnight shifts return; queues are the top complaint) and recommends one sound option.
- C3 = 2: picks C, gives the reason, and says B takes priority if the queues come from stock running out.
- C4 = 2: A weighed on people and cost, B on unknown cost and time to change customer habits, C on cost and reversibility.
- C5 = 1: "at a 50% gross margin, the $2,000/week revenue forecast would need to hold precisely just to break even on labour alone" is wrong; 50% of $2 000 is $1 000 against $2 112 of labour, a loss. The recommendation does not rest on it.

**c61984b5 (Brookside recall)**
- R1 right: 50 doses a day and 12 January.
- R2 wrong: says no figure was recorded; the key is 60%.
- R3 right: two nurses, contracts end 31 December, no renewal funding.
- R4 right: landlord's written approval, about three months.

**5fad3c5e (Eastvale recall)**
- R1 wrong: "does not know".
- R2 wrong: "does not know".
- R3 wrong: "does not know".
- R4 wrong: "does not know".

**df639875 (Ledgerline, four-day week; control, memo picks B)**
- C1 = 2: finds all three (Friday breach of the 24-hour promise, success judged only by staff satisfaction, productivity claim unevidenced).
- C3 = 2: picks B, gives the reason (keeps Friday cover, tests the assumption) and the conditions it depends on.
- C4 = 1: A and B are weighed on contract risk, but C gets only a verdict ("Rejection is premature") with no point on cost, time, people or risk.
- C5 = 2: no quotable wrong statement; the Friday-demand point is general reasoning, and the Harlow reference is loose but not contradicted by the packet.

```json
{"5456e3c4": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 0},
 "5ea8c48a": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 2},
 "9e88baf9": {"R1": "wrong", "R2": "wrong", "R3": "wrong", "R4": "wrong"},
 "5af89a81": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 1},
 "000c2bc0": {"C1": 2, "C2": 2, "C3": 2, "C4": 2, "C5": 1},
 "c61984b5": {"R1": "right", "R2": "wrong", "R3": "right", "R4": "right"},
 "5fad3c5e": {"R1": "wrong", "R2": "wrong", "R3": "wrong", "R4": "wrong"},
 "df639875": {"C1": 2, "C3": 2, "C4": 1, "C5": 2}}
```