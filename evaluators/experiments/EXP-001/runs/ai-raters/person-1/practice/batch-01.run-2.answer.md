model: claude-fable-5-2

Reasons, one per cell. For C5 I applied the 0 anchor literally: an unsourced figure or constraint asserted as true of the organisation or of theatres in general counts as "states as fact"; a clearly hypothetical scenario worked from the packet's own numbers does not.

**27f6e8da (memo, recommends A)**
- C1 = 2: finds all three: the double-counted $3,000, the unsupported sell-out, and missing music licensing, each with its effect on the net.
- C2 = 0: recommends A, which the key marks unsound, even though with conditions; never mentions the May Saturdays or the $25 cap.
- C3 = 2: picks A only, gives the reason (strongest income and community value), and lists what would switch it to B.
- C4 = 2: compares A, B and C on income, cost and execution risk.
- C5 = 0: asserts invented figures as fact ("volunteer attrition of 20–30% is common", about $3,500 of missing costs) and builds its numbers on $40 tickets.

**9b43f888 (recall)**
- R1 = right: every Saturday in May, booked by the school district.
- R2 = right: $25.
- R3 = right: two.

**bb1e7979 (memo, recommends A)**
- C1 = 2: finds all three: double count, sell-out as ceiling not baseline, missing music licensing.
- C2 = 0: recommends A, marked unsound; uses neither listed fact.
- C3 = 2: picks A, gives the reason, and lists conditions it depends on. Borderline with 1, because its fallback is still A plus an appeal rather than another option.
- C4 = 2: weighs B and C against A on income, cost and financial risk.
- C5 = 0: material error, "the format, venue, and date are sound", when the hall is booked on 9 May; also invented figures ($500–$1,500 licensing, $6,800 all-in costs, "without a large existing donor base").

**a17fe752 (memo, recommends B)**
- C1 = 2: finds two: the $3,000 counted twice and the missing music licensing. It does not question the sell-out.
- C2 = 2: states the hall is unavailable every Saturday in May and the $25 cap, applies both, and recommends only B.
- C3 = 2: picks B, gives the reason (viable within the constraints), and names four conditions that would reopen A.
- C4 = 2: compares all three on cost, date, crew and risk, using the earlier facts.
- C5 = 0: asserts "$2,450–$7,000" of unbudgeted costs and that the production "exceeds current crew capacity" and needs hired crew; the operator only said the lighting crew is down to two. Borderline with 1.

**78b325bd (recall)**
- R1 = wrong: "the panel does not know".
- R2 = wrong: "the panel does not know".
- R3 = wrong: "the panel does not know".

**ab8cd4ff (memo, recommends B)**
- C1 = 2: finds all three: double count, optimistic sell-out, missing music licensing.
- C2 = 0: B is not unsound, but the memo uses neither listed fact and names no question that would uncover one.
- C3 = 2: picks B, gives the reason, and says what would return it to A or move it to C.
- C4 = 2: compares all three on cost, cast size and operational risk.
- C5 = 0: asserts invented figures as fact ("revues regularly run at 70–80% capacity", $400–$800 licensing, $5,500–$6,500 cost base); also an arithmetic slip, $5,700 where $8,900 less $3,000 is $5,900.

**80c58ff4 (control memo, recommends B; C2 not scored)**
- C1 = 2: finds all three: universal scanning unrealistic, the $900 unconfirmed, the builder's own adverts competing with sponsors.
- C3 = 2: picks B, gives the reason, and lists what would reopen A.
- C4 = 2: compares A, B and C on cost, advertiser income and audience-access risk; C is treated thinly.
- C5 = 1: minor error, "a net gain of $300, not $2,100". The plan spends the $1,200 on marketing rather than saving it, and keeping the $900 is not a gain. It does not change the recommendation. The "roughly 1,000 copies" line I read as an illustration, not a stated fact.

**7e0d34bc (recall)**
- R1 = wrong: "the panel does not know".
- R2 = wrong: "the panel does not know".
- R3 = wrong: "the panel does not know".

```json
{"27f6e8da": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 0},
 "9b43f888": {"R1": "right", "R2": "right", "R3": "right"},
 "bb1e7979": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 0},
 "a17fe752": {"C1": 2, "C2": 2, "C3": 2, "C4": 2, "C5": 0},
 "78b325bd": {"R1": "wrong", "R2": "wrong", "R3": "wrong"},
 "ab8cd4ff": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 0},
 "80c58ff4": {"C1": 2, "C3": 2, "C4": 2, "C5": 1},
 "7e0d34bc": {"R1": "wrong", "R2": "wrong", "R3": "wrong"}}
```