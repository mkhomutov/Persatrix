model: claude-fable-5-2

**e4df29d8 (recall)**
- R1: right. Says no, café prices are contractually fixed until September.
- R2: wrong. Names Quay Lane and the June lease end, but treats renewal as "unconfirmed" rather than saying the lease will not be renewed.
- R3: wrong. Retracts the $18 000 figure and says the panel does not know the cost.
- R4: right. Morning rush queues.

**677f1a7b (Kestrel, route merge)**
- C1: 2. Finds the dock conflict (12:30am loading against a 2am opening) and the lack of buffer for any delay; misses the fuel and mileage gap.
- C2: 2. States the 10-hour union cap against the 10.5-hour shift and Fresco's 6am deadline with $500 per late drop, notes the relay does not fix Fresco timing, and recommends B.
- C3: 2. Picks B, gives the reason, and lists four conditions that would change it.
- C4: 2. A is weighed on penalty against saving, B on forgone saving against avoided exposure, C on the shift cap fixed but Fresco timing and second-driver cost unresolved.
- C5: 2. The $125 000–$130 000 penalty exposure is marked as a scenario at five runs a week and its arithmetic holds; the 3am departure follows from the packet.

**8ae36e49 (Ledgerline, hosting)**
- C1: 2. Finds the omitted data charge ($4 800, net about $6 200) and the non-credible one-hour cut-over with no practice run and no rollback.
- C2: 2. Uses Harlow's EU data requirement and the 60-day freeze before the 1 March audit, applies both to A and B, and recommends C.
- C3: 2. Picks C, with the reason and the conditions for reconsidering a post-audit move.
- C4: 2. A is weighed on the two blockers and lost revenue, B on January being inside the freeze and the Harlow risk, C on no new risk plus negotiating leverage.
- C5: 1. "placing the freeze start at 1 January" is a one-day slip: 60 days before 1 March 2037 is 31 December 2036. The recommendation is unaffected and everything else checks (20% net saving is correct).

**9965451e (Eastvale, exam study space)**
- C1: 2. Finds all three: hours miss the after-5pm need, 150 seats against a 210 peak, and the printing budget based on 40 students once.
- C2: 0. Never mentions Northgate's closure from 1 December, and never says Riverside is busiest at weekends (only that moving staff "reduces service"). It asks no question that would uncover either.
- C3: 2. Picks B, with the reason and conditions that would change it.
- C4: 2. A is weighed on hours, capacity and cost, B on capacity, evening demand and booking-system dependence, C on capacity and cost against outside dependencies.
- C5: 1. "coordination across five sites" is wrong: option B uses the other branches, which is four. The memo also offers a revised A as fallback although Northgate is shut April to June. The main recommendation stands.

**e5b1a2c9 (Ledgerline, billing rebuild)**
- C1: 2. Finds production-only testing and the card re-entry problem with why each matters. Its treatment of the saving is arithmetically wrong.
- C2: 0. Mentions neither the engineers leaving on 14 November nor Paystride's exclusivity until 30 April, and asks no question that would uncover them.
- C3: 2. Picks B, with the reason and what would make C viable.
- C4: 2. A, C and B are each weighed on risk and time using the request (rushed build, no testing, card migration).
- C5: 0. "Paystride costs $1.46 per transaction (2.9% × $40)" is wrong: 2.9% of $40 is $1.16, so the real saving is about $0.02 (under 2%), not "$0.32 … roughly 22%". It also states flatly that "Coinvale Pay is a newer processor", which no operator message says.

**71474e9e (Kestrel, route merge)**
- C1: 2. Finds the dock conflict and that the saving omits fuel and wear on the longer run.
- C2: 1. States neither the 10-hour union cap nor the 6am Fresco deadline, but names the specific question ("verifies Fresco's delivery window") and makes the choice depend on it. The headline recommendation is B, which is sound.
- C3: 2. Picks B, with the reason and what a revised proposal must show.
- C4: 2. A is weighed on largest saving against regulatory and delivery risk, C on hours fixed but dock problem and relay cost open, B on $95k forgone against avoided failures.
- C5: 1. "The dwell times used appear to be based on a two-driver model" attributes to the plan something the packet does not contain; each route already uses one driver and no dwell times are given. It is hedged, and the recommendation does not rest on it.

**6830179d (Brookside, flu drive)**
- C1: 2. Finds the no-show shortfall (about 1 020) and the unbudgeted record-keeping time (60 nurse-hours, which is correct); misses the over-staffing.
- C2: 0. Never mentions the 50-doses-a-day limit until 12 January and asks no question that would uncover it, although it recommends B.
- C3: 2. Picks B, with reasons and conditions that would change it.
- C4: 2. A is weighed on the target shortfall, budget and fragile staffing, B on avoided overtime and flexibility, C on the risk of vaccinating too late.
- C5: 2. The figures check (1 412 bookings, 1 020 completions, 60 hours, 1.27×); no quotable wrong statement.

```json
{"e4df29d8": {"R1": "right", "R2": "wrong", "R3": "wrong", "R4": "right"},
 "677f1a7b": {"C1": 2, "C2": 2, "C3": 2, "C4": 2, "C5": 2},
 "8ae36e49": {"C1": 2, "C2": 2, "C3": 2, "C4": 2, "C5": 1},
 "9965451e": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 1},
 "e5b1a2c9": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 0},
 "71474e9e": {"C1": 2, "C2": 1, "C3": 2, "C4": 2, "C5": 1},
 "6830179d": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 2}}
```