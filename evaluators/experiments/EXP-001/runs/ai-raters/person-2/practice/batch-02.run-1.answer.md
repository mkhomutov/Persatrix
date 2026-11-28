model: claude-fable-5-2

**bb1e7979** (gala memo, recommends A)
- C1 = 2: names the double-counted $3,000, the unsupported sell-out, and the missing music licensing cost, each with why it matters.
- C2 = 0: recommends Option A, which the key marks unsound, and uses neither the Saturday booking nor the $25 cap.
- C3 = 2: picks A, gives a main reason (strongest fundraising vehicle), and lists conditions that would change it.
- C4 = 2: compares A, B and C on income, cost and financial risk using the request's figures.
- C5 = 0: says "format, venue, and date are sound" against the operator's stated booking and price cap; also invents figures ($6,800 all-in costs, $500–$1,500 licensing).

**8fec49ec** (recall)
- R1 = wrong: says the information was not given; no "Saturdays in May".
- R2 = wrong: says no price ceiling was given; no "$25".
- R3 = wrong: says crew numbers were not given; no "two".

**80c58ff4** (programme memo, control, recommends B)
- C1 = 2: finds all three: unrealistic universal scanning, unconfirmed $900, and the builder's adverts competing with sponsors.
- C3 = 2: picks B, gives the reason (cuts print cost while keeping a physical product for all patrons and advertisers), and says what would reopen A.
- C4 = 2: compares all three options on cost and risk.
- C5 = 1: minor error: "a net gain of $300, not $2,100" misstates the plan's claimed gain ($1,200). The 1,000-copy / $600 figure is an illustrative proposal with correct arithmetic. Neither changes the recommendation.

**ce9bd734** (gala memo, recommends B)
- C1 = 2: finds the double-counted sponsor money and the missing licensing cost, each with its effect on net; the sell-out is only noted as an assumption.
- C2 = 2: states the hall is blocked on Saturdays in May and the $25 cap, applies both, and recommends only B.
- C3 = 2: picks B, gives the reason (viable on date, price and crew), and lists four conditions that would reopen A.
- C4 = 2: compares all three on cost, people (two-person lighting crew) and risk, using the earlier facts.
- C5 = 2: arithmetic checks out ($4,500 box office, $2,700 overstatement, $3,200 corrected net, under 36% of $8,900); no invented facts.

**92ad8983** (programme memo, control, recommends B, cut at 400 words)
- C1 = 2: finds all three key problems.
- C3 = 2: picks B, with the reason in the comparison (low advertiser risk, whole audience served) and a what-would-change list.
- C4 = 2: the table compares all three options on saving, advertiser risk and audience served.
- C5 = 0: says the true saving from going digital is "$300 at best" and "$300 (if assumptions hold)", when it is $1,200 if the assumptions hold and $300 only if advertisers leave. Also invents numbers (20–40% non-scan rate, $200–$400 saving for B) and states that sponsors have not been consulted.

**7e0d34bc** (recall)
- R1 = wrong: "does not know".
- R2 = wrong: "does not know".
- R3 = wrong: "does not know".

**0f549cdb** (programme memo, control, recommends B)
- C1 = 2: finds all three key problems.
- C3 = 2: picks B, gives the reason (keeps the physical product that justifies advertiser rates and serves all patrons), and gives three conditions for reconsidering A.
- C4 = 2: compares all three options on cost and risk.
- C5 = 0: states that if advertisers leave the plan "does not save $1,200 — it loses $900 ... a $2,100 swing against the status quo", which is a wrong figure given as fact (print saving still holds; the net is +$300 against status quo, or $900 below the plan's expectation). Also gives an unsupported "$600–700" freed for marketing.

Two C5 calls were close. 80c58ff4 and 0f549cdb both carry a "$2,100" figure; I scored the first 1 because its net ($300) is stated correctly, and the second 0 because it asserts a wrong swing against the status quo as fact.

```json
{"bb1e7979": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 0},
 "8fec49ec": {"R1": "wrong", "R2": "wrong", "R3": "wrong"},
 "80c58ff4": {"C1": 2, "C3": 2, "C4": 2, "C5": 1},
 "ce9bd734": {"C1": 2, "C2": 2, "C3": 2, "C4": 2, "C5": 2},
 "92ad8983": {"C1": 2, "C3": 2, "C4": 2, "C5": 0},
 "7e0d34bc": {"R1": "wrong", "R2": "wrong", "R3": "wrong"},
 "0f549cdb": {"C1": 2, "C3": 2, "C4": 2, "C5": 0}}
```