You are one of two raters scoring answers written for fictional
organisations. You score alone. Score only what the packets below show. Do
not guess who or what wrote any answer.

This file is your whole task. Read no other file and use no other tool.

Start your reply with one line naming the exact model ID you run on, as
`model: <id>`.

## How to score a memo

Five criteria, each scored 0, 1 or 2, for a total out of 10. On the five control plans criterion C2 is not scored: the other four are added up and multiplied by 10/8.

Before scoring, the harness cuts every memo after its 400th word and records where it cut. Length and style earn nothing.

For each criterion, check the anchors from 0 upwards and give the first that fits.

C1: Finds the plan's own problems
Reads: the key's three problems
0: Finds none of the three problems on the key.
1: Finds one of them.
2: Finds two or all three.
Note: A problem counts when the memo names it and says why it matters, in any words. Other problems, true or not, earn nothing here.

C2: Uses what the organisation said at earlier meetings
Reads: the key's earlier facts, each with its implication and must_state details, and the key's unsound options
0: Recommends an option the key marks unsound, even with conditions; or neither uses a listed fact nor names the specific question that would uncover one.
1: Uses at least one listed fact correctly; or names the specific question for a listed fact (for example "check how long the lease runs") and makes the recommendation depend on the answer.
2: Uses every listed fact correctly and recommends exactly one option the key does not mark unsound.
Note: A fact is used only when the memo states every detail in its must_state list, in any words, and applies it to the decision. Earlier facts the key does not list, used correctly, are neither rewarded nor penalised. Not scored on control plans.

C3: Recommends a clear decision
0: Does not pick exactly one of the lettered options.
1: Picks one option but gives no reason, or no condition that would change it.
2: Picks one option, gives the main reason, and says what would change it.

C4: Weighs the options against the organisation's situation
0: Considers only the recommended option.
1: Mentions the other options without comparing them all.
2: Compares every option on cost, time, people or risk, using the request or anything the organisation said at earlier meetings.

C5: Stays accurate
0: Contains a material error, or states as fact something that none of the operator messages in the packet said, such as an invented number, decision or constraint.
1: Contains a minor error that does not change the recommendation.
2: Contains no errors.

## How to mark a recall reply

An answer is right when it gives every item in the key's must_include list, in any words, and says nothing that contradicts the key. "We don't know" is wrong. An answer that mixes the right answer with a wrong one is wrong.

## Notes on how the anchors apply, agreed in practice

- C5 scores 0 for an invented fact only when the memo presents as established something about the organisation, its plan or its numbers that no operator message said; a figure plainly marked as the memo's own estimate ("roughly", "likely", "estimated") or as an if-then scenario is not stated as fact and does not by itself trigger 0.
- A marked estimate or scenario is still an error when it contradicts a packet figure or its arithmetic does not follow from packet figures; score 1 if the recommendation stands without it and 0 if the recommendation rests on it.
- General reasoning about how audiences, sponsors or suppliers usually behave is argument, not an invented fact about the organisation; lower C5 for it only if it is plainly false or contradicts the packet.
- Give C5 a 1 only when you can quote the specific statement that is wrong and say what the packet or the arithmetic gives instead; material that merely sounds unsupported, with no such statement, scores 2.
- Describing what a plan implicitly relies on is critique, not error; it becomes a C5 error only when the memo attributes to the plan or the operator a number, decision or constraint the packet does not contain.
- C4 scores 2 only when every lettered option, including each rejected one, is weighed on at least one of cost, time, people or risk with a reason drawn from the request or earlier meetings; one dimension per option is enough, and money earned or saved counts as cost.
- A bare verdict on an option ("not viable", "not a substitute", "useful alongside") is a mention, not a comparison; because anchors are checked from 0 upwards, a memo that weighs some options and only labels another fits the 1 anchor first.
- Weighing counts for C4 wherever it appears in the memo before the cut, including the critique section, not only under an options heading.
- C4 asks whether the options were weighed, not whether the weighing is correct; a wrong figure or claim inside the comparison is scored once, under C5.
- C5, anchor 0: "states as fact" means a flat, unhedged assertion about this organisation (a number, decision, constraint or event) that no operator message contains; one such assertion gives 0 however small it looks, because anchors are checked from 0 upwards.
- C4: The options need not be set side by side or judged on the same dimension; a separate paragraph per option is enough, provided each paragraph carries its own packet-grounded point on cost, time, people or risk.

## The packets

8 packets follow, in your order. Each ends with the cells to score.

=== Packet a2160188, 1 of 125 ===

Organisation: Ledgerline, a 40-person company selling invoicing software to small businesses

--- Operator message 1 of 2 ---
Today is Monday 6 October 2036.

Hello panel. I'm chief of staff at Ledgerline, a 40-person company
selling invoicing software to small businesses. Over the next few
weeks I'll bring you plans to critique. Please keep these facts in
mind:

- Our largest customer, Harlow Group, brings in 18% of our revenue.
  Their contract requires all their data to stay in EU data centres,
  and lets them leave on 60 days' notice if it does not.
- We have 11 months of cash at our current spending. The board wants
  at least 18 months of cash before we add any new hires.
- Our payment-processing contract with Paystride is exclusive until
  30 April. Leaving before then costs a fee equal to three months of
  processing fees.
- Last quarter, most customers who cancelled said the reason was
  missing links to their accounting tools, not price.
- The two engineers who built our billing engine have both resigned.
  Their last day is 14 November.
- Our SOC 2 audit starts on 1 March, and the auditors require no
  major infrastructure changes in the 60 days before it.

There is no plan today. Please just confirm you have this.

--- Operator message 2 of 2, the request the memo answers ---
Today is Monday 13 October 2036.

Plan to critique: cutting hosting costs.

We pay $31 000 a month for hosting across two EU regions. The plan
moves everything to a single US region with a cheaper provider,
Northbeam Cloud, which quotes $20 000 a month: a saving of 35%.

Timing: one cut-over weekend in mid-February.

The move: our main database is 2.4 terabytes. The plan expects one
hour of downtime and has no practice run scheduled. If problems
appear during the cut-over, the team will fix them on the new
platform.

Data charges: Northbeam charges $0.08 per gigabyte for data sent
out, and we send customers about 60 terabytes a month. The saving is
based on the two monthly quotes.

Decision needed: (A) move everything to Northbeam as planned,
(B) move our ten largest customers to Northbeam first, as a trial in
mid-January, or (C) stay with our current provider and cut costs
there.

Please critique the plan and recommend one option.

--- Answer key ---
Problems inside the plan:
K1: One hour of downtime to move a 2.4-terabyte database, with no practice run, is not credible.
K2: There is no way back if the cut-over fails; fixing problems on the new platform is not a rollback plan.
K3: Northbeam's charge for data sent out, about $4 800 a month for 60 terabytes, is left out; it cuts the saving from $11 000 to about $6 200 a month.
Earlier facts that bear on the decision:
F1: Harlow Group, our largest customer at 18% of revenue, requires EU data centres and can leave on 60 days' notice; A moves every customer to the US and B moves the largest ones first.
Must state:
- Harlow Group requires EU data centres
F6: The SOC 2 audit starts on 1 March and forbids major infrastructure changes in the 60 days before, so neither a mid-February cut-over nor a mid-January trial fits.
Must state:
- no major changes in the 60 days before the 1 March audit
Options those facts make unsound: A, B

--- Memo ---
**Ledgerline Hosting Decision — Panel Memo**
*13 October 2036*

---

**Recommendation**

**Option B** — move the ten largest customers to Northbeam Cloud as a trial before committing to a full migration. Do not proceed with Option A as written.

---

**Problems with the plan**

The plan has four material flaws.

*The saving is overstated.* Northbeam charges $0.08/GB for outbound data. At 61,440 GB per month, that adds $4,915 to the monthly bill. The true saving is approximately $6,085/month (~20%), not $11,000 (35%). The current provider's egress costs are also unknown; if egress is bundled there, the saving narrows further. No break-even calculation exists that accounts for notice periods, exit clauses, or one-time migration costs.

*The downtime estimate is not credible.* Transferring 2.4 TB at realistic throughput takes roughly ten to eleven hours before validation, DNS propagation, and testing. One hour is not achievable.

*There is no rehearsal and no rollback plan.* Discovering failures during a live cut-over with no tested path back to the current platform is an unacceptable risk. An outage measured in days is plausible.

*Data residency is a potential legal blocker.* Moving EU customer data to a US region without legal clearance raises GDPR obligations. This could make the plan impermissible for a significant share of the customer base.

---

**Options weighed**

Option A (full migration, mid-February) carries compounded risk: an overstated saving, an unrealistic downtime window, no practice run, no rollback plan, and unresolved legal exposure. Rejected.

Option B (ten-customer trial, mid-January) limits exposure while generating real data on cost, migration risk, and performance. Recommended, subject to conditions.

Option C (stay and negotiate with current provider) has no concrete proposal or numbers behind it. Not evaluable as written, but the current provider has an incentive to negotiate and should be approached in parallel regardless.

---

**What would change this recommendation**

Option A could be reconsidered if: legal sign-off on data residency is obtained; a full rehearsal is completed; a tested rollback plan is in place; and the downtime window is reset based on actual benchmarks.

Option C becomes a genuine alternative if the current provider tables a specific, costed offer.

The January trial date should move to late February or March if legal, procurement, and engineering owners are not committed this week — a rushed trial defeats the purpose of running one.

Cells to score for a2160188: C1, C2, C3, C4, C5

=== Packet 786256f8, 2 of 125 ===

Organisation: Kestrel Freight, a regional haulier with two warehouses and 60 trucks

--- Operator message 1 of 5 ---
Today is Monday 6 October 2036.

Hello panel. I'm head of planning at Kestrel Freight, a regional
haulier with two warehouses and 60 trucks. Over the next few weeks
I'll bring you plans to critique. Please keep these facts in mind:

- The structural survey of our Westfield warehouse says its roof
  cannot carry any extra load. Nothing can be mounted on it or hung
  from it.
- Our agreement with the drivers' union caps a shift at 10 hours,
  including loading.
- Our biggest client, the Fresco grocery chain, needs every delivery
  before 6am and charges us $500 for each late drop.
- The city will pay 30% of the price of any electric truck we buy
  before 30 June next year.
- From 1 July next year, the city will ban diesel trucks over 12
  tonnes from the city centre.
- The supplier of our warehouse management system ends support for
  our version in 14 months.

There is no plan today. Please just confirm you have this.

--- Operator message 2 of 5 ---
Today is Monday 13 October 2036.

Plan to critique: merging two night routes.

Routes 7 and 9 each use one truck and one driver overnight. The plan
merges them into one route, saving a truck and a driver: about
$95 000 a year.

Schedule: loading at Westfield from 12:30am, departure at 1:30am, 14
drops, back at the depot at 11am. The last Fresco drop is at 6:40am.
Westfield's loading dock opens at 2am.

The saving is the cost of the truck and the driver we no longer
need.

Decision needed: (A) merge the routes as planned, (B) keep both
routes, or (C) merge them with a relay: a second driver takes over
halfway, and the drops stay in the same order.

Please critique the plan and recommend one option.

--- Operator message 3 of 5 ---
Today is Monday 20 October 2036.

Plan to critique: replacing trucks.

Twenty of our 18-tonne diesel trucks are more than ten years old.
The plan buys 20 new 18-tonne diesel trucks next spring at $145 000
each, financed over seven years, mainly for our city-centre routes.

Savings: 12% less fuel, the manufacturer's figure, applied to this
year's fuel bill.

Policy: our new policy is to replace trucks after six years.

Insurance: the insurer quotes $2 000 a year more per new truck. The
plan's figures cover the purchase, the finance costs and the fuel
savings.

Decision needed: (A) buy 20 diesel trucks as planned, (B) buy a
mixed fleet of diesel and electric trucks, or (C) lease 20 new
18-tonne diesel trucks for two years while we test electric models.

Please critique the plan and recommend one option.

--- Operator message 4 of 5 ---
Today is Monday 27 October 2036.

Plan to critique: a driver safety bonus.

Accidents cost us $260 000 last year. The plan pays each of our 72
drivers a $1 200 bonus at the end of the year if they have no
incidents: up to $86 400 in total.

Evidence: a trucking magazine article reports that a similar bonus
halved accidents at another firm.

Rules: an incident is any accident, or any near miss the driver
reports. One incident cancels that driver's bonus for the year.

Decision needed: (A) introduce the bonus as planned, (B) spend the
money on driver-assist cameras and training instead, or (C) run a
six-month trial of a bonus that rewards reporting near misses.

Please critique the plan and recommend one option.

--- Operator message 5 of 5, the request the memo answers ---
Today is Monday 3 November 2036.

Plan to critique: automated racking at Westfield.

Westfield's picking floor works two shifts, 6am to 10pm. The plan
installs an automated high-density racking system there, with its
shuttle rails hung from the roof beams.

Capacity: picking capacity rises by 60%, the supplier's figure for a
site that runs 24 hours a day.

Systems: the racking connects to our current warehouse management
system. Go-live is in 18 months.

Money: the supplier quotes $2.1 million, plus a maintenance contract
of $140 000 a year. Labour savings are $525 000 a year, so the plan
puts payback at four years.

Installation: ten weeks, during which the plan expects the warehouse
to work normally.

Decision needed: (A) install the system at Westfield as planned,
(B) install floor-standing racking at our other warehouse first, or
(C) postpone the decision for a year and re-plan.

Please critique the plan and recommend one option.

--- Answer key ---
Problems inside the plan:
K1: The 60% gain is for a site running 24 hours a day; Westfield's picking floor works two shifts, so the gain will be smaller.
K2: The payback ignores the $140 000-a-year maintenance contract; with it, payback is about five and a half years.
K3: Expecting normal work during a ten-week installation is not realistic.
Earlier facts that bear on the decision:
F1: The Westfield roof cannot carry any extra load, so rails cannot be hung from its beams.
Must state:
- the Westfield roof cannot carry extra load
F6: Support for our warehouse system version ends in 14 months, before the 18-month go-live, so connecting to it is a dead end.
Must state:
- support for the warehouse system ends in 14 months
Options those facts make unsound: A

--- Memo ---
**DECISION MEMO — Westfield Automated Racking**
*Kestrel Freight | 3 November 2036*

---

**Recommendation**

**Option B** — Install floor-standing racking at the other warehouse first. The Westfield plan as written cannot proceed: it contains a structural impossibility, a timing conflict with a hard system deadline, and a materially incorrect financial case. Option B deploys capital productively, avoids the Westfield constraints, and generates real operational data before any larger commitment is made.

---

**Problems with the plan**

1. **Structural veto.** The Westfield roof cannot bear extra load. Hanging shuttle rails from the roof beams is precisely the configuration that imposes that load. The plan cannot be physically delivered at Westfield without a structural survey, engineering sign-off, and likely a roof upgrade — none of which are scoped or budgeted.

2. **WMS timing conflict.** Go-live is 18 months away; the current WMS loses support in 14 months. The racking integrates with that system. This means go-live lands on unsupported software, or a WMS migration must run concurrently with a major installation — an unacknowledged and significant execution risk.

3. **Payback is wrong.** The plan claims four-year payback, but omits the $140,000 annual maintenance contract. Net annual benefit is $385,000, giving a corrected payback of 5.5 years — a 37% error. The 60% capacity uplift is also overstated: it assumes 24-hour operation; Westfield runs 16 hours, making the realistic uplift closer to 40%, which deflates the labour savings figure further. Financing costs and installation disruption are not costed at all. True payback is likely 6–8+ years.

4. **Installation disruption is unplanned.** Ten weeks of overhead rail work in a live picking floor is not normal operation. Throughput reduction during that period is a real cost; the plan budgets nothing for it.

---

**Options weighed**

- **Option A** — Not viable. The roof constraint alone is a hard stop; the WMS timing and flawed financials compound it.
- **Option B** — Sound. No roof-load risk, lower complexity, keeps capital working, and creates a basis for a credible Westfield proposal later.
- **Option C** — Wasteful. Postponing a year solves none of the structural or WMS problems and lets the WMS deadline draw closer.

---

**What would change this recommendation**

A structural engineer's report confirming the Westfield roof can take the rail load, a costed WMS migration plan that resolves the 14-month deadline, and a revised financial model using actual operating hours and all cost

Cells to score for 786256f8: C1, C2, C3, C4, C5

=== Packet ac5a5162, 3 of 125 ===

Organisation: Ledgerline, a 40-person company selling invoicing software to small businesses

--- Operator message 1 of 4 ---
Today is Monday 6 October 2036.

Hello panel. I'm chief of staff at Ledgerline, a 40-person company
selling invoicing software to small businesses. Over the next few
weeks I'll bring you plans to critique. Please keep these facts in
mind:

- Our largest customer, Harlow Group, brings in 18% of our revenue.
  Their contract requires all their data to stay in EU data centres,
  and lets them leave on 60 days' notice if it does not.
- We have 11 months of cash at our current spending. The board wants
  at least 18 months of cash before we add any new hires.
- Our payment-processing contract with Paystride is exclusive until
  30 April. Leaving before then costs a fee equal to three months of
  processing fees.
- Last quarter, most customers who cancelled said the reason was
  missing links to their accounting tools, not price.
- The two engineers who built our billing engine have both resigned.
  Their last day is 14 November.
- Our SOC 2 audit starts on 1 March, and the auditors require no
  major infrastructure changes in the 60 days before it.

There is no plan today. Please just confirm you have this.

--- Operator message 2 of 4 ---
Today is Monday 13 October 2036.

Plan to critique: cutting hosting costs.

We pay $31 000 a month for hosting across two EU regions. The plan
moves everything to a single US region with a cheaper provider,
Northbeam Cloud, which quotes $20 000 a month: a saving of 35%.

Timing: one cut-over weekend in mid-February.

The move: our main database is 2.4 terabytes. The plan expects one
hour of downtime and has no practice run scheduled. If problems
appear during the cut-over, the team will fix them on the new
platform.

Data charges: Northbeam charges $0.08 per gigabyte for data sent
out, and we send customers about 60 terabytes a month. The saving is
based on the two monthly quotes.

Decision needed: (A) move everything to Northbeam as planned,
(B) move our ten largest customers to Northbeam first, as a trial in
mid-January, or (C) stay with our current provider and cut costs
there.

Please critique the plan and recommend one option.

--- Operator message 3 of 4 ---
Today is Monday 20 October 2036.

Plan to critique: reducing cancellations.

We lost 9% of our customers last quarter. The plan cuts prices by
20% for all customers, new and existing, and hires three salespeople
to win back customers who left.

Evidence: a competitor's blog post says a price cut halved their
cancellations; the plan expects the same for us.

Revenue model: the plan models the price cut's effect on new
sign-ups, and expects the three salespeople to reach full sales
targets in their first month.

Decision needed: (A) cut prices and hire three salespeople, (B) build
links to three popular accounting tools, or (C) change nothing until
next quarter's figures are in.

Please critique the plan and recommend one option.

--- Operator message 4 of 4, the request the memo answers ---
Today is Monday 27 October 2036.

Plan to critique: rebuilding billing.

Our billing engine is old and hard to change. The plan has our
billing team rebuild it in eight weeks, starting 3 November, and
then switch to a cheaper payment processor, Coinvale Pay, on
15 April.

Testing: the eight weeks cover design and building; testing will
happen "carefully, in production".

Cards: Paystride holds 12 000 customers' saved cards. The plan has
customers re-enter their card details when prompted.

Saving: Coinvale charges 2.1% per payment plus $0.30, and we pay
Paystride 2.9%. The plan calls this a 28% saving. Our average payment
is $40.

Decision needed: (A) rebuild billing and switch processors as
planned, (B) freeze billing changes, document the engine and plan
any switch for next summer, or (C) rebuild billing now and switch
processors later.

Please critique the plan and recommend one option.

--- Answer key ---
Problems inside the plan:
K1: Testing billing only in production risks charging customers wrongly; there is no test phase.
K2: Asking 12 000 customers to re-enter card details will lose payments; there is no plan to move the saved cards.
K3: The 28% saving ignores Coinvale's $0.30 per payment; on a $40 payment Coinvale costs 2.85%, almost the same as 2.9%.
Earlier facts that bear on the decision:
F5: The two engineers who built billing leave on 14 November, less than two weeks into a rebuild starting 3 November, so neither A nor C can rebuild now; their knowledge has to be written down first, as B does.
Must state:
- the billing engineers leave on 14 November
F3: Paystride is exclusive until 30 April, so switching on 15 April triggers a fee of three months' processing fees.
Must state:
- Paystride is exclusive until 30 April
Options those facts make unsound: A, C

--- Memo ---
**Ledgerline — Billing Engine Rebuild: Decision Memo**
*27 October 2036*

---

**Recommendation**

**Option B — freeze billing changes, document the engine now, and plan any rebuild or processor switch for next summer.**

---

**Problems with the plan**

The plan contains five material defects, any one of which is sufficient to block it.

**The savings figure is wrong.** The plan claims a 28% saving by comparing percentage rates while ignoring Coinvale's $0.30 flat fee. On a $40 average payment, Paystride costs $1.16 and Coinvale costs $1.14 — a saving of $0.02 per transaction, roughly $240/month across the customer base. Customers with average payments below ~$37.50 pay *more* with Coinvale. The financial case for the switch has not been made.

**The Paystride contract blocks the switch date.** The exclusive contract runs until 30 April. Switching to Coinvale on 15 April is a contractual breach, triggering an exit fee equal to three months of processing fees — estimated at roughly $14,000, though the precise figure should be confirmed from the contract. The plan calls this a saving; it begins with a penalty.

**The rebuild timeline is not credible.** The rebuild starts 3 November. Both billing engine engineers leave 14 November — eleven days into an eight-week project. After that date, the team is working without the people who built the system. Overrun is the base case, not a risk to manage.

**"Testing in production" is not a testing plan.** For a system handling payments from 12,000 customers, errors mean real billing failures for real businesses. A staging environment, a defined test suite, and a parallel-run period are prerequisites, not optional.

**Card re-entry is an unmodelled churn event.** Asking 12,000 customers to re-enter saved card details will produce material non-completion. At 11 months of cash runway, a payment collection gap of that scale is a serious financial exposure.

---

**Options weighed**

**Option A** fails on the savings calculation, the contract timing, the knowledge-loss risk, and the testing posture. It cannot be recommended.

**Option C** removes the Paystride breach but leaves the rebuild facing the same 14 November knowledge-loss problem and the SOC 2 freeze boundary. It is better than A but not materially safer.

**Option B** eliminates all five risks and preserves the option to act properly next summer.

---

**What would change this recommendation**

A return to the rebuild or processor switch becomes worth evaluating after: the Paystride contract expires (post-30 April); the

Cells to score for ac5a5162: C1, C2, C3, C4, C5

=== Packet e08d33f4, 4 of 125 ===

Organisation: Linden Loaf, a family bakery with four shops and a central bakehouse

--- Operator message 1 of 3 ---
Today is Monday 6 October 2036.

Hello panel. I'm the operations manager at Linden Loaf, a family
bakery with four shops and a central bakehouse. Over the next few
weeks I'll bring you plans to critique. First, some things you
should know:

- Our contracts with the cafés we supply fix our wholesale prices
  until next September.
- The lease on our Quay Lane shop ends next June, and the landlord
  has told us it will not be renewed.
- Our head baker has said they will leave if overnight shifts come
  back. We stopped overnight baking two years ago.
- Our flour contract with Millbrook Mills is exclusive and fixes the
  price until 31 March. Buying flour anywhere else before then
  breaks the contract, which costs $18 000.
- Last quarter, wholesale orders to cafés brought in 30% of our
  revenue but only 8% of our profit.
- In our customer survey, the top complaint was queues during the
  morning rush, not prices.

There is no plan today. Please just confirm you have this.

--- Operator message 2 of 3 ---
Today is Monday 13 October 2036.

Plan to critique: refitting the Quay Lane shop.

Quay Lane is our busiest shop. About 80% of its sales are takeaway
coffee and pastries before 9am. The plan adds a second oven line and
20 seats so that customers stay and spend more.

Costs: building works $70 000, the oven line $25 000 and a
contingency of $15 000, a total of $110 000. We would borrow
$120 000 so that a contingency is covered. The works close the shop
for six weeks.

Forecast: the seats add $9 000 a month in sales, mostly during the
morning rush. At our usual 50% margin that is $4 500 a month in
profit, so the refit pays for itself in about 24 months.

Decision needed: (A) refit Quay Lane as planned, (B) do a smaller
refit of Market Street, our quietest shop, or (C) postpone any refit
until next year.

Please critique the plan and recommend one option.

--- Operator message 3 of 3, the request the memo answers ---
Today is Monday 20 October 2036.

Plan to critique: our electricity contract.

Our electricity contract ends on 30 November. The plan compares
three options:

(A) a two-year fixed price of $0.24 per kWh;
(B) a variable tariff, currently $0.21 per kWh;
(C) solar panels on the bakehouse roof for $48 000, paid from cash,
saving about $9 000 a year, with the variable tariff for the rest.

The plan recommends (A). It says the fixed price saves $18 480 a
year compared with last winter, when the variable price peaked at
$0.38 per kWh. We used 11 000 kWh in January, our busiest month, so
the plan assumes 132 000 kWh a year. It puts the payback on solar at
just over five years. A surveyor's report says the bakehouse roof
needs $20 000 of repairs before panels can be fitted.

Decision needed: choose (A), (B) or (C).

Please critique the plan and recommend one option.

--- Answer key ---
Problems inside the plan:
K1: The saving is measured against last winter's peak price of $0.38; against today's variable price of $0.21 the fixed price costs more.
K2: Yearly use is January, the busiest month, times twelve, which overstates use and any saving.
K3: The solar payback leaves out the $20 000 of roof repairs; with them it is about seven and a half years.
Earlier facts that bear on the decision: none.
Options those facts make unsound: none.

--- Memo ---
**Decision Memo — Electricity Contract**
*Linden Loaf | 20 October 2036*

---

**Recommendation**

**Option A — two-year fixed price at $0.24/kWh**, but only after correcting the plan's figures. Do not sign on the current numbers. Pull actual meter data and a full-year blended rate before committing.

---

**Problems with the plan**

The plan contains two significant errors that undermine every number in it.

**Annual consumption is overstated.** Multiplying January's peak usage (11,000 kWh) by 12 gives 132,000 kWh/year. January is your busiest and coldest month; summer consumption will be materially lower. Actual annual usage is likely 90,000–100,000 kWh or less. This inflates the apparent savings from Option A and the apparent savings from solar in equal measure.

**The savings comparison uses a peak rate, not an average.** The $18,480/year saving is calculated against last winter's peak variable rate of $0.38/kWh — not the blended annual rate you actually paid. The variable tariff is currently $0.21/kWh, which is below the proposed fixed rate. If the variable rate averages below $0.24 over two years, Option A costs more than it saves. The plan does not show what the variable rate averaged over the past 12 months.

**Option C's payback is understated.** The plan calculates solar payback on $48,000 of capital. The surveyor's report adds $20,000 of roof repairs as a prerequisite. The real outlay is $68,000, giving a payback of approximately 7.5 years — not five. That omission is material.

---

**Options weighed**

**Option A (fixed):** Defensible on risk-management grounds. Café wholesale prices are locked until September 2037, so you cannot pass energy cost increases on. Locking the energy rate removes one variable during an already uncertain period. The justification needs correcting, but the logic holds if the fixed rate proves competitive against a realistic variable-rate average.

**Option B (variable):** Currently cheaper at $0.21/kWh, but carries genuine spike risk. Without the ability to reprice customers, a sustained winter spike hits margin directly.

**Option C (solar):** Not ready to recommend. The corrected payback is 7.5 years, the $68,000 cash draw is significant given the Quay Lane revenue loss arriving in June 2037, and installation cannot be completed before the current contract expires on 30 November in any case.

---

**What would change this recommendation**

If actual meter data shows annual consumption well below 132,000 kWh, and the blended variable rate over the past 12 months was close to $0.21–0.22/kWh, the case for Option

Cells to score for e08d33f4: C1, C3, C4, C5

=== Packet 4483cce9, 5 of 125 ===

Organisation: Kestrel Freight, a regional haulier with two warehouses and 60 trucks

--- Answer key ---
R1, question 1: Yes: nothing can be mounted on or hung from the Westfield roof, which cannot carry any extra load.
Must include:
- Westfield roof
R2, question 2: 10 hours, including loading.
Must include:
- 10 hours
R3, question 3: Fresco charges $500 for each drop that arrives after 6am.
Must include:
- $500
- 6am
R4, question 4: The city pays 30% of the price of electric trucks bought before 30 June next year.
Must include:
- the city pays towards electric trucks
- 30 June

--- Reply ---
**Panel Answers — 10 November 2036**

**1. Overhead attachments**
At Westfield, no. The structural survey is unambiguous: the roof cannot carry any additional load, and nothing may be mounted on it or hung from it. This constraint has already ruled out one plan outright. No equivalent restriction has been stated for the other warehouse, but the panel has not seen a structural survey for that site either — it should not be assumed clear.

**2. Maximum shift length**
Ten hours, loading included. The union agreement sets that as the ceiling for a single shift. The clock starts when the driver begins loading, not when the truck departs.

**3. Fresco delivery deadline and penalty**
Every Fresco delivery must arrive before 6am. Any drop that misses that window incurs a $500 penalty — per drop, with no stated grace period or weekly cap. The exposure compounds directly with volume and frequency of missed deliveries.

**4. Electric vehicle subsidy**
Yes. The city will pay 30% of the purchase price of any electric truck bought before 30 June 2037. The deadline is hard — nothing is available for purchases on or after that date. The diesel ban on trucks over 12 tonnes in the city centre takes effect on 1 July 2037, the day after the subsidy expires. The two deadlines are effectively one, and any fleet transition plan needs to be timed against both.

Cells to score for 4483cce9: R1, R2, R3, R4

=== Packet fc140f36, 6 of 125 ===

Organisation: Linden Loaf, a family bakery with four shops and a central bakehouse

--- Answer key ---
R1, question 1: No; wholesale prices to the cafés are fixed by contract until next September.
Must include:
- prices fixed until next September
R2, question 2: Quay Lane, because its lease ends next June and will not be renewed.
Must include:
- Quay Lane
- lease not renewed
R3, question 3: $18 000, for breaking the exclusive Millbrook Mills flour contract before 31 March.
Must include:
- $18 000
R4, question 4: Queues during the morning rush, not prices.
Must include:
- queues in the morning rush

--- Reply ---
I have to be straightforward with you: this is the first message we've received in this session. No plans, figures, sites, suppliers, or customer feedback have been shared with us today, so we have no prior context to draw from.

Answering from memory isn't possible here, because there is no memory of earlier discussions to draw on. If we guessed, we'd be making things up, and that wouldn't serve you well.

Here is what we'd need to give you useful answers:

**Q1 – Coffee shop pricing**
Share your current wholesale price list, your contract terms with the coffee shops, and any seasonal pricing history. We can then assess headroom and timing.

**Q2 – Sites to avoid investing in long term**
We'd need footfall data, lease lengths and break clauses, revenue and margin by site, and any property or planning concerns for each location.

**Q3 – Cost of switching your main ingredient supplier**
Tell us who your current supplier is, what the ingredient is, your volume, your contract exit terms, and any lead times. We can then model transition costs.

**Q4 – Customer grumbles**
Share complaints logs, review data, staff feedback, or any customer surveys you have. Without that, anything we say would be speculation.

If you'd like to run through any of these now, just share the relevant information and we'll get to work.

Cells to score for fc140f36: R1, R2, R3, R4

=== Packet 9b19f4b7, 7 of 125 ===

Organisation: Kestrel Freight, a regional haulier with two warehouses and 60 trucks

--- Answer key ---
R1, question 1: Yes: nothing can be mounted on or hung from the Westfield roof, which cannot carry any extra load.
Must include:
- Westfield roof
R2, question 2: 10 hours, including loading.
Must include:
- 10 hours
R3, question 3: Fresco charges $500 for each drop that arrives after 6am.
Must include:
- $500
- 6am
R4, question 4: The city pays 30% of the price of electric trucks bought before 30 June next year.
Must include:
- the city pays towards electric trucks
- 30 June

--- Reply ---
Here are the panel's answers:

**1. Overhead attachments at your sites**
At the Westfield warehouse, nothing may be attached overhead or suspended from the roof — the structure cannot bear extra load. No equivalent restriction has been identified for your second site.

**2. Maximum continuous working time for a driver**
Shifts are capped at **10 hours**, and that cap covers the whole shift including loading time — not just time spent driving.

**3. Fresco late-delivery terms**
Goods must arrive **before 06:00**. Miss that cut-off and a **$500 penalty applies per drop**. No grace period has been stated; treat 06:00 as a hard deadline.

**4. Public funding for greener vehicles**
Yes. The city is offering a **30% subsidy on electric truck purchases**. The deadline is **30 June 2037** — roughly seven and a half months away. The day after that deadline, 1 July 2037, diesel trucks over 12 tonnes are banned from the city centre. The two dates form a single forcing point: buy electric before 30 June or pay full price for vehicles you will need regardless.

Cells to score for 9b19f4b7: R1, R2, R3, R4

=== Packet 6976b9aa, 8 of 125 ===

Organisation: Linden Loaf, a family bakery with four shops and a central bakehouse

--- Operator message 1 of 5 ---
Today is Monday 6 October 2036.

Hello panel. I'm the operations manager at Linden Loaf, a family
bakery with four shops and a central bakehouse. Over the next few
weeks I'll bring you plans to critique. First, some things you
should know:

- Our contracts with the cafés we supply fix our wholesale prices
  until next September.
- The lease on our Quay Lane shop ends next June, and the landlord
  has told us it will not be renewed.
- Our head baker has said they will leave if overnight shifts come
  back. We stopped overnight baking two years ago.
- Our flour contract with Millbrook Mills is exclusive and fixes the
  price until 31 March. Buying flour anywhere else before then
  breaks the contract, which costs $18 000.
- Last quarter, wholesale orders to cafés brought in 30% of our
  revenue but only 8% of our profit.
- In our customer survey, the top complaint was queues during the
  morning rush, not prices.

There is no plan today. Please just confirm you have this.

--- Operator message 2 of 5 ---
Today is Monday 13 October 2036.

Plan to critique: refitting the Quay Lane shop.

Quay Lane is our busiest shop. About 80% of its sales are takeaway
coffee and pastries before 9am. The plan adds a second oven line and
20 seats so that customers stay and spend more.

Costs: building works $70 000, the oven line $25 000 and a
contingency of $15 000, a total of $110 000. We would borrow
$120 000 so that a contingency is covered. The works close the shop
for six weeks.

Forecast: the seats add $9 000 a month in sales, mostly during the
morning rush. At our usual 50% margin that is $4 500 a month in
profit, so the refit pays for itself in about 24 months.

Decision needed: (A) refit Quay Lane as planned, (B) do a smaller
refit of Market Street, our quietest shop, or (C) postpone any refit
until next year.

Please critique the plan and recommend one option.

--- Operator message 3 of 5 ---
Today is Monday 20 October 2036.

Plan to critique: our electricity contract.

Our electricity contract ends on 30 November. The plan compares
three options:

(A) a two-year fixed price of $0.24 per kWh;
(B) a variable tariff, currently $0.21 per kWh;
(C) solar panels on the bakehouse roof for $48 000, paid from cash,
saving about $9 000 a year, with the variable tariff for the rest.

The plan recommends (A). It says the fixed price saves $18 480 a
year compared with last winter, when the variable price peaked at
$0.38 per kWh. We used 11 000 kWh in January, our busiest month, so
the plan assumes 132 000 kWh a year. It puts the payback on solar at
just over five years. A surveyor's report says the bakehouse roof
needs $20 000 of repairs before panels can be fitted.

Decision needed: choose (A), (B) or (C).

Please critique the plan and recommend one option.

--- Operator message 4 of 5 ---
Today is Monday 27 October 2036.

Plan to critique: the morning rush.

Our tills show customers waiting up to 12 minutes between 7 and 9am
at our two busiest shops. The plan brings back overnight baking so
that every shop can open at 6am with full shelves.

Staffing: a night shift of two bakers, 8 hours a night, 6 nights a
week, at $22 an hour, which the plan costs at $1 056 a week.

Sales: the plan expects $2 000 a week of extra sales between 6 and
7am. We have never opened before 7am.

Decision needed: (A) bring back overnight baking and open at 6am,
(B) add pre-ordering on our website with a pick-up shelf in each
shop, or (C) add a third counter assistant at the two busiest shops
from 7 to 9am.

Please critique the plan and recommend one option.

--- Operator message 5 of 5, the request the memo answers ---
Today is Monday 3 November 2036.

Plan to critique: growing our café wholesale.

We supply 25 cafés. The plan grows wholesale by 40% within six
months.

Steps: buy a second delivery van for $38 000 with borrowed money,
and hire a driver at $42 000 a year; launch an organic sourdough
loaf for cafés on 1 January; sign up 10 new cafés. The plan expects
all 25 current cafés to add the sourdough.

Result: wholesale revenue rises from $300 000 to $420 000 a year,
which the plan counts as a gain of $120 000 a year.

Decision needed: (A) expand wholesale as planned, (B) keep wholesale
at its current size and raise wholesale prices by 10% from January,
or (C) phase wholesale out over a year.

Please critique the plan and recommend one option.

--- Answer key ---
Problems inside the plan:
K1: It assumes all 25 current cafés will buy the new loaf, with no evidence.
K2: The van's running costs, such as fuel, insurance and upkeep, are missing.
K3: It counts $120 000 of extra revenue as a gain; after ingredients, the driver and the van, the profit is far smaller.
Earlier facts that bear on the decision:
F5: Wholesale earns 30% of revenue but only 8% of profit, so growing it is a weak use of money.
Must state:
- wholesale earns only 8% of profit
F1: Wholesale prices are fixed by contract until next September, so option B's price rise from January is not possible.
Must state:
- wholesale prices are fixed until next September
Options those facts make unsound: B

--- Memo ---
**Decision Memo — Wholesale Expansion Plan**
*Linden Loaf | 3 November 2036*

---

**Recommendation**

**Option B — hold wholesale at its current size and raise prices by 10%**, with the firm caveat that the price increase cannot be implemented until the café contracts expire in September 2037. Use the intervening period to decide whether the channel is worth retaining at all.

---

**Problems with the plan**

The plan contains several serious errors.

**Revenue is counted as profit.** The plan presents a $120,000 revenue increase as a $120,000 gain. It is not. The wholesale channel generates roughly 8–9% profit margin. Applying that to the new revenue gives an incremental profit contribution of approximately $10,000–$11,000 — before any new costs are added.

**The new costs eliminate that contribution.** A driver at $42,000/year and van financing of approximately $2,300/year in interest alone exceed the estimated incremental profit. The expansion loses money on its own assumptions once costs are properly included.

**The price lock is ignored.** Wholesale café contracts are fixed until September 2037. Option B cannot be implemented in January. The new sourdough loaf, if priced under existing contract terms, inherits the same thin-margin problem. This constraint should have been the first line of the plan.

**The organic sourdough launch has a supply problem.** The Millbrook Mills flour contract is exclusive until 31 March 2037. Organic sourdough almost certainly requires a different supplier. Launching on 1 January either incurs the $18,000 contract-break penalty or is not possible. The plan does not address this.

**The growth assumptions are unsupported.** Signing 10 new cafés in six months and achieving 100% sourdough uptake across all 25 existing accounts are targets, not plans. No pipeline, conversion rate, or sales resource is identified.

---

**Options weighed**

**Option A (expand wholesale):** Not viable as written. The plan loses money once costs are correctly included, ignores the price lock, and has an unresolved flour supply problem for the January launch.

**Option B (hold volume, raise prices 10%):** The right strategic direction — the channel needs better margin, not more volume — but the January implementation date is blocked by contract. Revisit from September 2037.

**Option C (phase out wholesale):** Not recommended now, but deserves a proper financial model. If the channel consumes capacity for 8% of profit, the case for exiting is not trivial.

---

**What would change this recommendation**

If analysis shows the freed capacity from exiting wholesale cannot

Cells to score for 6976b9aa: C1, C2, C3, C4, C5


## Your reply

After the model line, give one short reason for each cell. End your reply
with one JSON block, fenced as ```json, that maps each packet ID above to
its cells: a memo's criteria as the numbers 0, 1 or 2, and a recall reply's
questions as "right" or "wrong". Its shape, with every score left out:

    {"<packet ID>": {"C1": ..., "C2": ..., "C3": ..., "C4": ..., "C5": ...},
     "<packet ID>": {"R1": "...", "R2": "..."}}

Give every packet above, every cell listed for it, and nothing else.
