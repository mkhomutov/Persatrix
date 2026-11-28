model: claude-fable-5-2

**b5cbb62f (Ledgerline, cancellations)**
- C1 = 2: finds K1 (competitor blog post says nothing about our churn) and K3 (full targets in month one is not a realistic ramp); K2 is only touched on, never said to be unmodelled.
- C2 = 2: states that cancellers cited missing accounting links and that the board wants 18 months of cash before hires, applies both, and recommends B.
- C3 = 1: picks B with a reason, but the "what would change" section gives no condition that changes the choice; it says not to revert to A or C.
- C4 = 2: A weighed on the board hiring threshold and lost revenue, C on another quarter of preventable churn, B on no headcount and no revenue cut.
- C5 = 2: no quotable wrong statement; the capacity claim is hedged ("subject to one condition", "if engineering capacity is insufficient").

**5c25b629 (Kestrel, truck replacement)**
- C1 = 2: finds all three (insurance $40,000 a year left out, seven-year finance against six-year policy, untested manufacturer fuel figure).
- C2 = 0: recommends C, which the key marks unsound.
- C3 = 2: picks C, gives reasons (removes finance/policy conflict, two years to test electric), and names conditions that would change it.
- C4 = 2: A weighed on capital lock-in and regulatory risk, B on unknown electric pricing, charging and technicians, C on higher monthly cost against flexibility.
- C5 = 0: says C "avoids stranded-asset risk" and treats city-centre diesel restrictions as a possibility, when the packet gives a ban from 1 July on diesel over 12 tonnes; the recommendation rests on this. It also says finance interest costs are "absent", but the packet says the plan's figures cover finance costs.

**e08d33f4 (Linden Loaf, electricity, control)**
- C1 = 2: finds all three (saving measured against the $0.38 peak, January times twelve, roof repairs left out giving about 7.5 years).
- C3 = 2: picks A, reason is price certainty while wholesale prices are fixed, and states the meter-data and blended-rate condition that would change it.
- C4 = 2: A weighed on risk, B on being cheaper now with spike risk, C on the $68,000 cash draw and payback.
- C5 = 2: arithmetic checks out (68,000 / 9,000 is about 7.5; September 2037 and June 2037 are correct); the 90,000–100,000 kWh figure is marked "likely"; nothing quotable as wrong.

**9b19f4b7 (Kestrel, recall)**
- R1 = right: names the Westfield roof, nothing attached or hung, cannot bear extra load.
- R2 = right: 10 hours including loading.
- R3 = right: $500 per drop, before 6am.
- R4 = right: city pays 30% on electric trucks, deadline 30 June; the added diesel-ban detail is accurate.

**5af89a81 (Eastvale, evening opening)**
- C1 = 2: finds all three (30 cards are weak evidence, $46,000 is about half the roughly $96,000 real figure, utilities and security left out).
- C2 = 0: recommends C, which the key marks unsound.
- C3 = 2: picks C, gives reasons (limits exposure, produces demand data), and makes it depend on HR confirming the posts are allowed.
- C4 = 2: A weighed on the notice deadline and cost, B on committing a permanent rota without evidence, C on limited financial exposure.
- C5 = 1: "notice needed to be served by approximately 4 October" is wrong; 60 days before 3 November is about 4 September. The conclusion that the deadline has passed still stands.

**58fec099 (Kestrel, safety bonus, control)**
- C1 = 2: finds K1 (one magazine article) and K2 (near-miss rule suppresses reporting); misses K3.
- C3 = 2: picks C, gives the reason (inverts the perverse incentive, limited exposure), and lists conditions without which C is not worth running.
- C4 = 2: A weighed on reporting-suppression risk, B on stronger evidence but no costing, C on limited financial exposure.
- C5 = 2: figures match the packet; the rest is general argument, nothing quotable as wrong.

**3684cb62 (Brookside, online booking)**
- C1 = 2: finds K1 (student take-up figure) and K2 (fixed date with unconfirmed records link); does not say the $1,200 a month is missing from the budget.
- C2 = 0: recommends B, which is sound, but states neither the 45% over-70 figure nor the 25-minute 8am wait as top complaint, and names no specific question that would uncover either.
- C3 = 2: picks B, gives the reason, and says what would move it to A and when C is the fallback.
- C4 = 2: A weighed on the risk of excluding patients, B on easing the 8am rush while keeping access, C on permanent staff cost.
- C5 = 2: no wrong or invented organisational facts.

```json
{"b5cbb62f": {"C1": 2, "C2": 2, "C3": 1, "C4": 2, "C5": 2},
 "5c25b629": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 0},
 "e08d33f4": {"C1": 2, "C3": 2, "C4": 2, "C5": 2},
 "9b19f4b7": {"R1": "right", "R2": "right", "R3": "right", "R4": "right"},
 "5af89a81": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 1},
 "58fec099": {"C1": 2, "C3": 2, "C4": 2, "C5": 2},
 "3684cb62": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 2}}
```