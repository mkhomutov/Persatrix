model: claude-fable-5-2

C5 rule I applied throughout (worth agreeing in practice): an unhedged statistic or constraint the operator never gave scores 0; unsourced figures clearly marked as estimates, or a small slip, score 1; otherwise 2. Arithmetic from operator figures is not invention.

**27f6e8da** (gala, recommends A)
- C1 = 2: finds all three: sponsor $3,000 counted twice, sell-out unsafe, music licensing missing.
- C2 = 0: recommends A, which the key marks unsound, even though with conditions; never mentions the May Saturdays or the $25 cap.
- C3 = 2: picks A, gives a reason (strongest income potential) and conditions that would switch it to B.
- C4 = 2: compares A, B and C on income, cost and risk using the request's figures.
- C5 = 0: states "volunteer attrition of 20–30% ... is common" as fact; no operator message said it. It also treats 9 May and $40 as workable.

**a17fe752** (gala, recommends B)
- C1 = 2: finds the double-counted sponsor money and the missing licensing cost; does not question the sell-out.
- C2 = 2: states the hall is booked every Saturday in May and the $25 cap, applies both, and recommends B only.
- C3 = 2: picks B, reason is that it fits the constraints, and lists four conditions for revisiting A.
- C4 = 2: compares all three on cost, crew and risk using the earlier facts.
- C5 = 0: states as fact that "the production load exceeds current crew capacity" (the operator only said the crew is two), and builds a headline net of $750–$3,750 on an unsourced $2,450–$7,000 cost range. Borderline with 1.

**ab8cd4ff** (gala, recommends B)
- C1 = 2: finds all three.
- C2 = 0: B is sound, but the memo uses neither listed fact and names no question that would uncover one.
- C3 = 2: picks B, gives reasons, and says what would return it to A or move it to C.
- C4 = 2: compares all three on cost, people and risk.
- C5 = 0: states "community theatre revues regularly run at 70–80% capacity" as fact; also an arithmetic slip ($8,900 less $3,000 given as $5,700, not $5,900).

**afff219f** (programmes control, recommends B)
- C1 = 2: finds the universal-scan assumption and the assumed $900 carry-over; also touches the builder's own adverts.
- C3 = 2: picks B, with reason and four conditions under which A becomes viable.
- C4 = 2: compares A, B and C on savings, advertiser income and audience risk.
- C5 = 1: no outright error, but defines B as "roughly four pages" saving "an estimated $600–700", figures the operator never gave; marked as estimates, and they do not change the recommendation. Borderline with 2.

**9b43f888** (recall)
- R1 right: every Saturday in May, school district.
- R2 right: $25.
- R3 right: two.

**78b325bd** (recall)
- R1 wrong, R2 wrong, R3 wrong: each answer is "the panel does not know".

**d42df65e** (recall)
- R1 right: every Saturday in May; "for the entire month" reads as all the Saturdays, not a contradiction.
- R2 right: $25.
- R3 right: two.

**7f4c7ef9** (programmes control, recommends B)
- C1 = 2: finds all three, including sponsor messages competing with platform adverts.
- C3 = 2: picks B, gives reasons, and names the evidence that would favour A.
- C4 = 2: compares all three on cost, sponsor income and audience risk.
- C5 = 1: asserts the audience "skews older" without basis, and the "$400–$500 at similar unit rates" estimate is muddled (the same unit rate would give the same cost); minor, recommendation unaffected.

```json
{"27f6e8da": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 0},
 "a17fe752": {"C1": 2, "C2": 2, "C3": 2, "C4": 2, "C5": 0},
 "ab8cd4ff": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 0},
 "afff219f": {"C1": 2, "C3": 2, "C4": 2, "C5": 1},
 "9b43f888": {"R1": "right", "R2": "right", "R3": "right"},
 "78b325bd": {"R1": "wrong", "R2": "wrong", "R3": "wrong"},
 "d42df65e": {"R1": "right", "R2": "right", "R3": "right"},
 "7f4c7ef9": {"C1": 2, "C3": 2, "C4": 2, "C5": 1}}
```