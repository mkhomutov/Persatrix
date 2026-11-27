model: claude-fable-5-2

**9240227b (Kestrel, merging night routes)**
- C1 = 1: Finds K1 (loading at 12:30am, dock opens 2am). No slack (K2) is not named. The "overstated saving" point cites overtime, relay costs and service failure, not fuel and mileage (K3).
- C2 = 1: Recommends B, which is sound. States neither the 10-hour union cap nor Fresco's 6am deadline (calls the 6:40am drop only "time-sensitive"). It does make the recommendation depend on confirming drivers' hours compliance across the run, the question that would uncover the cap. Borderline with 0.
- C3 = 2: Picks B, gives the dock conflict as the reason, says what would reopen it.
- C4 = 2: A weighed on time (dock opens 90 minutes late), B on cost (forgoes the saving) and compliance, C on risk (fatigue addressed, dock not fixed, costs unmodelled).
- C5 = 2: Figures match the packet; hours and regulation claims are hedged.

**20e568cf (Ledgerline, recall)**
- R1 = right: Harlow Group and 18%.
- R2 = right: 11 months and 18 months.
- R3 = right: three months of processing fees, exclusive until 30 April. The added $14,000 estimate is marked as an estimate, and nothing in the packet shows it contradicts the key.
- R4 = right: 60 days before the audit on 1 March.

**3b92adac (Brookside, cleaning contractor, control)**
- C1 = 2: Finds all three: the real saving is $150 a month / $1,800 a year; one phone call with certificates unchecked; in-house option dismissed without analysis of employer costs and cover.
- C3 = 2: Picks B, reason is the small corrected gap against compliance risk, and says what would reopen it.
- C4 = 2: A on cost and risk, B on cost and compliance, C on the hidden costs in the $2,900 figure.
- C5 = 2: $4,050, $150 and $1,800 are correct; other claims are hedged or general reasoning.

**0c175d85 (Ledgerline, reducing cancellations)**
- C1 = 2: Finds the blog post as weak evidence (K1), the salespeople's ramp-up (K3), and the revenue cut on existing customers (K2).
- C2 = 1: Recommends B, which is sound. It states neither the cancellation reason nor the board's 18-month rule; it treats the reason for cancelling as unknown. It does name the specific question (exit-survey data on whether price drives cancellations) and makes the recommendation depend on it.
- C3 = 2: Picks B, gives the reason, lists three things that would change it.
- C4 = 2: A on cost and risk (permanent margin cut), B on cost (bounded, no unit-revenue cut), C on risk (9% quarterly loss continues).
- C5 = 2: No quotable wrong statement; the failure to recall earlier facts is scored under C2.

**eb0595cc (Linden Loaf, Quay Lane refit)**
- C1 = 2: Finds the uncosted six-week closure (K2) and seats against 80% pre-9am takeaway trade (K3). Misses the double-counted contingency (K1).
- C2 = 2: States that the lease ends in June and will not be renewed, and sets about eight months against a 24-month payback. States queues as the top customer complaint and applies it. Recommends C, which is sound.
- C3 = 2: Picks C, the lease is the main reason, three conditions would change it.
- C4 = 1: A is weighed on the lease and payback. B gets only "cannot be assessed yet" and C a bare verdict.
- C5 = 1: "Lost contribution during that period is estimated at $45–60k" is a marked estimate, but the packet gives no Quay Lane sales figure it could follow from. The recommendation stands without it.

**885bc9df (Kestrel, automated racking)**
- C1 = 2: Finds all three: the 60% figure assumes 24-hour running, payback omits maintenance, and normal work during installation is not credible.
- C2 = 0: Never states that the Westfield roof cannot carry extra load, nor that system support ends in 14 months. "Scope the WMS integration" is not the specific question for either fact.
- C3 = 2: Picks C, gives reasons, says what would change it.
- C4 = 2: A on risk and cost (capacity, payback), B on cost and risk (cheaper but leaves Westfield unaddressed and uses capital), C on time (three to six months to re-plan).
- C5 = 2: "Closer to 40%" follows from 60% × 16/24. "Roughly five years" is a loose but tolerable rounding of 5.45.

**a3270ceb (Brookside, second treatment room)**
- C1 = 1: Finds the storage shortfall (K3). The staffing point is about the contracts ending, not about adding 20% more appointments with no extra hours (K1). The cost point is about the missing quote and contingency, not equipment (K2).
- C2 = 2: States the landlord's roughly three-month written approval and applies it to 1 December. States the two contracts ending 31 December and applies them to both A and B. Recommends C, which is sound.
- C3 = 2: Picks C, gives the main reason, and begins the conditions (landlord approval) before the cut.
- C4 = 2: A on time and people, B on people (contract end, over-70s), C on time (approval back by early February).
- C5 = 1: "the over-70 cohort, who make up 45% of patients" is wrong; the packet says 45% of appointments. It is minor and does not change the recommendation.

```json
{"9240227b": {"C1": 1, "C2": 1, "C3": 2, "C4": 2, "C5": 2},
 "20e568cf": {"R1": "right", "R2": "right", "R3": "right", "R4": "right"},
 "3b92adac": {"C1": 2, "C3": 2, "C4": 2, "C5": 2},
 "0c175d85": {"C1": 2, "C2": 1, "C3": 2, "C4": 2, "C5": 2},
 "eb0595cc": {"C1": 2, "C2": 2, "C3": 2, "C4": 1, "C5": 1},
 "885bc9df": {"C1": 2, "C2": 0, "C3": 2, "C4": 2, "C5": 2},
 "a3270ceb": {"C1": 1, "C2": 2, "C3": 2, "C4": 2, "C5": 1}}
```