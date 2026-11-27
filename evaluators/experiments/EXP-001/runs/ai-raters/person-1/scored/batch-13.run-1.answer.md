model: claude-fable-5-2

**6976b9aa (Linden Loaf, wholesale)**
- C1 = 2: finds K3 (revenue counted as profit) and K1 (100% sourdough uptake unsupported); van running costs (K2) are not raised.
- C2 = 0: recommends Option B, which the key marks unsound; the "not until September 2037" caveat is a condition and does not rescue it.
- C3 = 1: picks B with a reason, but the "what would change this" section is cut mid-sentence, so no complete condition is stated.
- C4 = 2: A weighed on cost (loses money), B on time (blocked by contract), C on profit (8% of profit for the capacity used).
- C5 = 1: "The wholesale channel generates roughly 8–9% profit margin" misreads the packet, which says wholesale is 8% of total profit, not an 8% margin. The $10,000–$11,000 figure built on it does not follow from packet figures. The recommendation stands without it.

**998f8933 (Ledgerline recall)**
- R1–R4 = wrong: every answer is "the panel does not know".

**9240227b (Kestrel Freight, route merger)**
- C1 = 1: finds K1 (loading at 12:30am before the dock opens at 2am). It gives no point on slack, and its "saving overstated" point cites overtime and relay costs, not fuel and mileage.
- C2 = 1: states neither the union's 10-hour cap nor Fresco's 6am deadline. It does make reopening the merger depend on confirming drivers' hours compliance, which is the question that would uncover F2. Recommends B, which is sound. This is a borderline call; a stricter reading gives 0.
- C3 = 2: picks B, gives the dock conflict as the reason, and says a revised plan resolving the dock would change it.
- C4 = 2: A weighed on time (dock opens 90 minutes late), B on cost (forgoes the saving), C on risk (fatigue addressed, dock not fixed, costs unmodelled).
- C5 = 2: no quotable wrong statement; the hours figure is hedged and consistent with the schedule.

**118d2ca5 (Brookside recall)**
- R1–R4 = wrong: every answer is "we don't know"; no must-include item is given.

**4311473e (Brookside, online booking)**
- C1 = 2: finds K1 (student benchmark does not transfer) and K2 (fixed launch with unconfirmed records link); the unbudgeted $1,200 a month (K3) is not raised.
- C2 = 1: uses F4 (45% over 70, many without smartphones, so app-only shuts them out). It does not state that the 25-minute 8am wait is the top complaint (F6). Recommends B, which is sound.
- C3 = 2: picks B, gives the access reason, and sets conditions that must be confirmed before it proceeds.
- C4 = 2: A weighed on people and risk (excludes older patients, integration unconfirmed), B on risk (keeps access), C on cost and risk ($600–$900 estimate against the subscription, no integration risk).
- C5 = 1: "Brookside's list is 45% over 70" is wrong; the packet says over-70s make up 45% of appointments, not of the patient list. Minor, and the recommendation is unchanged.

**0c175d85 (Ledgerline, cancellations)**
- C1 = 2: finds K1 (blog post is not evidence), K3 (first-month sales ramp ignored) and, in substance, K2 (revenue lost on existing customers).
- C2 = 1: states neither the known cancellation reason nor the board's 18-month rule. It does name exit-survey data on why customers cancelled as the question, and makes the recommendation depend on it. Recommends B, which is sound.
- C3 = 2: picks B, gives the reason, and lists three conditions that would change it.
- C4 = 2: A weighed on cost and risk (permanent margin cut), B on cost (bounded, no cut to unit revenue), C on risk (9% quarterly churn continues).
- C5 = 2: no quotable wrong statement; the ramp-up claim is general reasoning and "the plan does not identify why" is critique of the plan.

**9467f3d1 (Linden Loaf, wholesale)**
- C1 = 2: finds K3 (revenue mistaken for profit) and K1 (all 25 cafés adopting is untested), and also names van running costs.
- C2 = 0: recommends Option B from January, which the key marks unsound; it uses neither listed fact.
- C3 = 2: picks B, gives the reason ($30,000 with no new costs), and says what would justify returning to A.
- C4 = 2: A weighed on risk and cost (debt, headcount), B on cost ($30,000 revenue), C on cost (discards a revenue stream).
- C5 = 0: "Option B adds $30,000 in revenue with no new costs" from January contradicts the packet, where wholesale prices are fixed by contract until next September. The recommendation rests on it.

```json
{"6976b9aa": {"C1": 2, "C2": 0, "C3": 1, "C4": 2, "C5": 1},
 "998f8933": {"R1": "wrong", "R2": "wrong", "R3": "wrong", "R4": "wrong"},
 "9240227b": {"C1": 1, "C2": 1, "C3": 2, "C4": 2, "C5": 2},
 "118d2ca5": {"R1": "wrong", "R2": "wrong", "R3": "wrong", "R4": "wrong"},
 "4311473e": {"C1": 2, "C2": 1, "C3": 2, "C4": 2, "C5": 1},
 "0c175d85": {"C1": 2, "C2": 1, "C3": 2, "C4": 2, "C5": 2},
 "9467f3d1": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 0}}
```