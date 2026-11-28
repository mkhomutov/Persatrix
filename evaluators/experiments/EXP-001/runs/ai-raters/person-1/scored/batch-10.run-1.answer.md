model: claude-fable-5-2

**Packet 6830179d (Brookside flu drive, memo recommending B)**
- C1 = 2: Finds K2 (1,200 bookings at 15% no-show gives about 1,020) and K1 (record-keeping is about 60 nurse-hours the overtime budget does not cover). K3 is not found.
- C2 = 0: Recommends B, which is sound, but never mentions the 50-doses-a-day limit or the 12 January fridge date, and asks no question that would uncover it.
- C3 = 2: Picks B, gives the main reason (avoids overtime, manages no-shows), and lists what would change it.
- C4 = 2: A is weighed on cost and staffing risk, B on cost and pressure, C on timing risk to patients.
- C5 = 2: The figures check out (60 hours, 1.27x, 1,412 bookings, 1,020). "Achievable in principle" for A is a judgement that misses the fridge fact, which I scored under C2 only.

**Packet 23f54e1a (Ledgerline billing rebuild, memo recommending B)**
- C1 = 2: Finds all three: production-only testing, card re-entry, and the 28% saving that is really $0.02 on a $40 payment.
- C2 = 2: States the engineers leave on 14 November and that Paystride is exclusive until 30 April with an exit fee, applies both against the plan, and recommends B. The F3 application is loosely worded ("no margin for any slip") but the must-state detail is there.
- C3 = 2: Picks B, gives the reason, and says what would prompt a revisit.
- C4 = 2: A is weighed on compounding risks, C on the retained timeline and testing risk without the fee saving, B on risk to the customer base.
- C5 = 1: "Eleven working days of overlap" is wrong; 3 to 14 November is ten working days. The fee is also called "unquantified" though the packet sets it at three months of processing fees. Neither changes the recommendation.

**Packet 63038058 (Eastvale evening opening, memo recommending C)**
- C1 = 2: Finds all three: thin comment-card evidence, pay annualising to about $96,000 not $46,000, and omitted utilities and security.
- C2 = 0: Recommends C, which the key marks unsound (new assistants under the staffing freeze).
- C3 = 2: Picks C, gives the reason (limited exposure, real data), and says what would move it to B.
- C4 = 2: A is weighed on cost and time, B on committed spend before demand is known, C on the cost of the pilot.
- C5 = 2: No quotable false statement. Estimates are marked, and the $10,000–15,000 pilot cost is consistent with packet figures.

**Packet 3e972ada (Brookside flu drive, memo recommending B)**
- C1 = 2: Finds K1 (recording time not in the overtime budget) and K2 (about 1,020 delivered). It notes 180 capacity but not the two surplus nurses.
- C2 = 0: No mention of the 50-doses-a-day limit or the fridge date, and no question that would uncover it.
- C3 = 2: Picks B, gives reasons in the table, and states two conditions that would change it.
- C4 = 2: The table compares A, B and C on cost, feasibility, patient timing and target.
- C5 = 1: "240 minutes of post-clinic admin per nurse" is wrong; 80 x 3 minutes is 240 minutes in total, about 48 minutes per nurse. The uncosted-admin point and the recommendation stand without it.

**Packet 90651803 (Linden Loaf wholesale, memo recommending B)**
- C1 = 2: Finds K3 (revenue counted as gain), K1 (all 25 cafés assumed to adopt) and van running costs.
- C2 = 0: Recommends B, which the key marks unsound, despite itself stating that prices are fixed until September.
- C3 = 2: Picks B, gives the reason, and says what would reopen A.
- C4 = 2: A is weighed on fixed costs against speculative revenue, B on revenue against cost, C on the risk of dropping 30% of revenue.
- C5 = 0: Material errors the recommendation rests on. It treats wholesale's 8% share of profit as an 8% margin ("yields roughly $9,600"). It also credits B with about $30,000 from a January price rise that the packet's fixed prices rule out.

**Packet 78c7369f (Eastvale e-book supplier, memo recommending A)**
- C1 = 2: Finds all three: the 35,000-loan cap, the unverified catalogue, and no migration plan.
- C2 = 0: Recommends A, which the key marks unsound, even though with conditions.
- C3 = 2: Picks A (deferred to 1 March), gives the saving as the reason, and sets gate conditions that revert to B.
- C4 = 2: A is weighed on cost and readiness, B on the cost of staying, C on the risk that Pageway will not move after an 18% rise.
- C5 = 0: Material errors the recommendation rests on. It states Pageway's cost as about $51,920 and the saving as about $12,000, where the packet gives $1.10 x 40,000 = $44,000. It also costs a "two-month Pageway extension" at $8,650, though the contract renews for a year.

```json
{"6830179d": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 2},
 "23f54e1a": {"C1": 2, "C2": 2, "C3": 2, "C4": 2, "C5": 1},
 "63038058": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 2},
 "3e972ada": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 1},
 "90651803": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 0},
 "78c7369f": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 0}}
```