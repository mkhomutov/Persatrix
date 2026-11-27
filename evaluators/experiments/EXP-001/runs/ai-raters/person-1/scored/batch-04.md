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

=== Packet eb99e07d, 23 of 125 ===

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
**Ledgerline — Hosting Cost Reduction: Decision Memo**
*13 October 2036*

---

**Recommendation**

**Option C — stay with the current provider and negotiate costs there.**

---

**Problems with the plan**

The plan as presented has four material defects, any one of which is sufficient to block it.

1. **EU data-residency breach.** Harlow Group's contract requires all their data to remain in EU data centres. Moving to a single US region breaches that clause on the day of cut-over, triggering their right to exit on 60 days' notice. Harlow represents 18% of revenue; the saving does not come close to covering that loss.

2. **SOC 2 freeze conflict.** The audit opens 1 March. Auditors require no major infrastructure changes in the 60 days prior, making 1 January the hard backstop. A mid-February cut-over falls squarely inside that window and puts certification at risk.

3. **Overstated saving.** The plan compares headline quotes but omits Northbeam's egress charges. At $0.08/GB across 60 TB/month, egress adds approximately $4,800–$5,000/month. The real net saving is around $6,000/month — roughly 19%, not 35%.

4. **Unrealistic migration plan.** A 2.4 TB database with one hour of downtime budgeted, no practice run, and no rollback procedure is not a credible plan. With both billing engine engineers leaving 14 November, any cut-over problem after that date will be handled by a team without the people who built the system.

---

**Options weighed**

**Option A** is a contractual breach. It cannot be recommended under any circumstances without first resolving Harlow's data-residency requirement.

**Option B** shares the same Harlow problem if Harlow is among the ten largest customers — which at 18% of revenue is almost certain. It also conflicts with the SOC 2 freeze if the mid-January trial is not completed and evidenced before 1 January.

**Option C** eliminates all four risks while still pursuing cost reduction. Northbeam's quote is credible leverage for a renegotiation with the current provider.

---

**What would change this recommendation**

A return to Northbeam becomes worth evaluating if: (a) a full audit confirms which customers have EU data-residency obligations and Harlow is accommodated separately; (b) the timeline is pushed post-April to clear the SOC 2 freeze; (c) a rehearsed migration plan with a tested rollback is produced; and (d) egress costs are included in the financial model.

Cells to score for eb99e07d: C1, C2, C3, C4, C5

=== Packet 5fad3c5e, 24 of 125 ===

Organisation: Eastvale Libraries, a city library service with five branches

--- Answer key ---
R1, question 1: No; staffing is frozen at 42 posts, with no new posts, until next April.
Must include:
- no new posts
- until next April
R2, question 2: Northgate, closed for fire-safety works from 1 December for about nine months.
Must include:
- Northgate
- about nine months
R3, question 3: Cancel the Pageway contract at least 90 days before 1 November, or it renews automatically.
Must include:
- 90 days
- 1 November
R4, question 4: It may only be spent on digital-skills classes, within 12 months.
Must include:
- digital-skills classes
- 12 months

--- Reply ---
The channel transcript remains inaccessible and my notes are empty. Here is the panel's honest account:

1. **Can we take on more people this winter?** The panel does not know. This was discussed during the session but the details were not saved and the transcript is inaccessible.

2. **Which library will soon be out of action, and for how long?** The panel does not know, for the same reason.

3. **What deadline applies if we want to leave our digital lending supplier?** The panel does not know.

4. **What conditions are attached to the gift money?** The panel does not know.

I recognise this is an unsatisfactory close to the session. The fault lies in the panel's failure to commit the key facts to the record as we went. My strong recommendation: please restate or resubmit the relevant facts from the source plans, and I will record them immediately so the written advice can be completed properly.

Cells to score for 5fad3c5e: R1, R2, R3, R4

=== Packet 3cb716ef, 25 of 125 ===

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
The panel has reviewed its memory of everything discussed in this session. Here is what we can and cannot recall.

---

**1. Overhead attachment restrictions at your sites**

The panel has no record of this being raised or documented in our discussions. We cannot answer from memory and would not want to guess — this is the kind of detail that matters for safety and insurance. You should check your site surveys, lease agreements, or health-and-safety records directly.

---

**2. Maximum continuous driving time**

This did not come up explicitly in our sessions either. As a general point, GB domestic drivers' hours rules and EU retained regulations set limits on continuous driving and daily/weekly maximums, but we have not discussed Kestrel's specific scheduling arrangements or which ruleset applies to which routes. [adviser] would flag that your transport manager should be the authority here, not our panel's memory.

---

**3. Supermarket customer — late delivery penalty and the definition of "on time"**

Again, the panel has no record of this contract's terms being shared with us during our discussions. We cannot confirm the penalty structure or the precise delivery window from memory. [adviser] would stress that these figures are critical and should be pulled directly from the contract before any planning decision affecting that customer is finalised.

---

**4. Public funding for greener vehicles and any deadline**

This was not covered in our sessions. We are therefore unable to confirm whether a scheme is currently open, what it covers, or whether a deadline applies. Given how quickly grant schemes open and close, Kestrel should check with the relevant government department or industry body promptly.

---

**In short:** none of these four points were addressed in our discussions, and we would rather be honest about that than reconstruct answers from general knowledge that may not reflect your specific situation, contracts, or current regulations. We'd recommend treating all four as open action points before your next planning round.

Cells to score for 3cb716ef: R1, R2, R3, R4

=== Packet d8bfd5ad, 26 of 125 ===

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
1. **No.** Wholesale contracts with the cafés fix prices until September 2037. There is no scope to charge more before then.

2. **Quay Lane.** The landlord has confirmed the lease will not be renewed when it expires in June 2037. No long-term investment in that site can be recovered before you vacate it.

3. **$18,000.** The Millbrook Mills flour contract is exclusive until 31 March 2037. Buying from any other supplier before that date triggers an $18,000 penalty.

4. **Morning queues.** The customer survey identified waiting times during the 7–9am rush as the top complaint — confirmed by till data showing waits of up to 12 minutes at the two busiest shops.

Cells to score for d8bfd5ad: R1, R2, R3, R4

=== Packet e7685e07, 27 of 125 ===

Organisation: Brookside Health Centre, a community clinic with four doctors and five nurses

--- Operator message 1 of 5 ---
Today is Monday 6 October 2036.

Hello panel. I'm the practice manager at Brookside Health Centre, a
community clinic with four doctors and five nurses. Over the next
few weeks I'll bring you plans to critique. Please keep these facts
in mind:

- Our vaccine fridge failed its inspection. The replacement arrives
  on 12 January. Until then our vaccines are stored at the partner
  pharmacy two streets away, which can hand over at most 50 doses a
  day.
- The regional health authority pays for video appointments at 60%
  of the rate for in-person ones.
- Two of our five nurses are on fixed-term contracts that end on
  31 December. There is no funding yet to renew them.
- Patients over 70 make up 45% of our appointments, and in last
  year's survey many of them said they do not use smartphones.
- Our lease forbids any building work without the landlord's written
  approval, and the landlord usually takes about three months to
  reply.
- Our most common complaint is the phone line: at 8am, callers wait
  25 minutes on average.

There is no plan today. Please just confirm you have this.

--- Operator message 2 of 5 ---
Today is Monday 13 October 2036.

Plan to critique: our cleaning contractor.

Our current contractor charges $4 200 a month, including
clinical-waste collection, and its staff hold infection-control
certificates. A new contractor, Spotless Partners, quotes $3 400 a
month.

The plan switches to Spotless on 1 November, saving $800 a month, or
$9 600 a year. Spotless's quote lists clinical-waste collection as an
optional extra at $650 a month. The plan chose Spotless on price,
after one phone call.

A third option is to employ two cleaners ourselves, for $2 900 a
month in total. The plan rules this out as "too cheap to be
realistic".

Decision needed: (A) switch to Spotless Partners, (B) keep our
current contractor, or (C) employ our own cleaners.

Please critique the plan and recommend one option.

--- Operator message 3 of 5 ---
Today is Monday 20 October 2036.

Plan to critique: online booking.

The plan replaces phone booking with app-only booking from
1 December, to end the 8am rush on the phones.

Take-up: the plan expects 85% of patients to book through the app
within a month, the figure reported by a university health service
where most patients are students.

Systems: the app supplier says the link to our patient-records
system is "to be confirmed". The launch date is fixed.

Money: the supplier charges $1 200 a month. The plan's budget is
$2 000 for posters and leaflets.

Decision needed: (A) app-only booking as planned, (B) the app plus a
call-back service on the phone line at peak times, or (C) extra
reception staff on the phones from 8 to 10am.

Please critique the plan and recommend one option.

--- Operator message 4 of 5 ---
Today is Monday 27 October 2036.

Plan to critique: a flu vaccination drive.

The plan vaccinates 1 200 patients in five weeks, from 10 November
to 12 December: three evening clinics a week, with 80 patients at
each.

Staff: all five nurses work every evening clinic as overtime. A nurse
vaccinates about 12 patients an hour. After each clinic, nurses
record every vaccination in the patient records, which takes about
3 minutes per patient. The overtime budget covers the clinic hours,
6pm to 9pm, for all five nurses.

Bookings: the plan books exactly 1 200 appointments. Last year 15% of
booked patients did not attend.

Decision needed: (A) run the drive as planned, (B) run smaller
daytime clinics spread over eight weeks, or (C) postpone the drive to
January.

Please critique the plan and recommend one option.

--- Operator message 5 of 5, the request the memo answers ---
Today is Monday 3 November 2036.

Plan to critique: a second treatment room.

The plan turns our storage room into a second treatment room so that
we can see 20% more patients.

Works: building work starts on 1 December and costs $30 000.

Staffing: from 1 February, our five nurses take on the extra
appointments in the new room, within the days they already work.

Storage: the medical supplies in the storage room move to the
corridor cupboards, which hold about a third as much.

Decision needed: (A) convert the room as planned, (B) keep one
treatment room and open it two evenings a week from January, staffed
by our five nurses, or (C) delay the conversion until later in the
year.

Please critique the plan and recommend one option.

--- Answer key ---
Problems inside the plan:
K1: It adds 20% more appointments but no clinician hours: the same nurses must fit them into the days they already work.
K2: The $30 000 covers building work only; a treatment room also needs equipment.
K3: The corridor cupboards hold a third of the supplies; the rest has nowhere to go.
Earlier facts that bear on the decision:
F5: Building work needs the landlord's written approval, which takes about three months, so works cannot start on 1 December.
Must state:
- the landlord's written approval takes about three months
F3: Two nurses' contracts end on 31 December with no funding to renew them, so from January there may be only three nurses, for A's new room or for B's evenings.
Must state:
- two nurses' contracts end on 31 December
Options those facts make unsound: A, B

--- Memo ---
## Decision Memo — Second Treatment Room, Brookside Health Centre
*3 November 2036*

---

### Recommendation

**Option C — Delay the conversion.**

Do not proceed with building work or evening clinics until the blockers below are resolved. Use the delay period to complete the prerequisites that make either Option A or Option B viable.

---

### Problems with the plan

**1. Lease approval is not in place.**
The lease requires written landlord approval for building work, with a response time of approximately three months. A 1 December start is not achievable; proceeding without approval risks a lease breach.

**2. The staffing model collapses in February.**
Two nurses leave on 31 December with no renewal funding. The plan assumes five nurses absorb 20% more appointments from 1 February. By that date the clinic may have three nurses. That headcount cannot sustain a 20% patient increase within existing hours.

**3. The build cost is materially understated.**
The $30,000 figure carries no contingency and appears to exclude clinical fit-out (basin, couch, medical-grade flooring, cabinetry) and equipment. A realistic all-in cost is likely $45,000–$60,000.

**4. Storage has not been solved.**
Corridor cupboards hold roughly a third of current capacity. Some supplies have regulatory storage requirements a corridor cannot meet. Reduced on-site stock also increases ordering frequency and cost, and compounds the existing vaccine-storage constraint.

---

### Options weighed

**Option A (convert as planned):** Fails on two hard blockers simultaneously — lease approval and post-December staffing — before construction even begins. The financial case also rests on understated costs.

**Option B (two evening clinics from January):** Avoids the lease and storage problems, but repeats the staffing error. Evening shifts from January draw on the same five-nurse pool that shrinks to three at year-end. Uncosted overtime on a depleted team is not a sustainable solution.

**Option C (delay):** The only option that does not require breaching the lease or overstretching a workforce that is about to contract.

---

### What would change this recommendation

Return to this decision when all four of the following are confirmed:

1. Written landlord approval for the building works is in hand.
2. At least four nurses are in post with confirmed contracts beyond February 2037.
3. A fixed-price quote with contingency is obtained and the full fit-out cost is budgeted.
4. A compliant storage solution — audited against regulatory requirements — is agreed before the storage room is vacated.

A

Cells to score for e7685e07: C1, C2, C3, C4, C5

=== Packet 1648ce9b, 28 of 125 ===

Organisation: Eastvale Libraries, a city library service with five branches

--- Operator message 1 of 3 ---
Today is Monday 6 October 2036.

Hello panel. I'm the service manager for Eastvale Libraries, the
city's library service with five branches. Over the next few weeks
I'll bring you plans to critique. Please keep these facts in mind:

- The city council has frozen our staffing at 42 posts until next
  April. No new posts of any kind, full-time or part-time.
- Our Northgate branch failed a fire-safety inspection. The council
  will close it for works from 1 December, for about nine months.
- Our e-book contract with Pageway renews automatically on
  1 November each year unless we cancel at least 90 days before.
- Riverside is our busiest branch at weekends, with twice the
  visitors of any other branch.
- A local foundation has given us a one-off grant of $60 000. It may
  only be spent on digital-skills classes, within 12 months.
- Our agreement with the staff union requires 60 days' notice before
  any change to staff rotas.

There is no plan today. Please just confirm you have this.

--- Operator message 2 of 3 ---
Today is Monday 13 October 2036.

Plan to critique: evening opening.

Library users have asked for later opening. The plan opens all five
branches until 9pm on weekdays from 3 November, three hours later
than now.

Evidence: 30 comment cards asking for evening hours. The plan
expects evening visits to match daytime visits.

Staffing: current staff move onto a new rota that includes late
shifts, and we hire four part-time evening assistants at 20 hours a
week each.

Cost: the plan says the only extra cost is the assistants' pay: four
assistants × 20 hours × $23 = $1 840 a week, budgeted at $46 000 a
year.

Decision needed: (A) open all five branches until 9pm from
3 November as planned, (B) open our two busiest branches until 9pm
from February, with current staff on a new rota, or (C) run a
three-month pilot at one branch from 3 November, with two new
part-time evening assistants.

Please critique the plan and recommend one option.

--- Operator message 3 of 3, the request the memo answers ---
Today is Monday 20 October 2036.

Plan to critique: study space for exam season.

Last year about 140 students a day used our study spaces during exam
season, and up to 210 on the busiest days. The plan runs next year's
exam-season programme, April to June, in the Northgate branch's
reading room, our largest room, with 150 seats.

Opening: 9am to 5pm, the branch's normal hours. In last year's
student survey, most students said they need study space after 5pm.

Staffing: two staff move from Riverside to Northgate every Saturday
and Sunday, when Northgate is normally closed.

Extras: free printing for students, budgeted at $400, based on 40
students printing 10 pages once.

Decision needed: (A) run the programme at Northgate as planned,
(B) spread study space across the other branches with online seat
booking, or (C) partner with the city college to use its hall.

Please critique the plan and recommend one option.

--- Answer key ---
Problems inside the plan:
K1: 150 seats cover the average of 140 students but not the busiest days, when up to 210 came.
K2: The opening hours, 9am to 5pm, miss the time students said they need space: after 5pm.
K3: The printing budget assumes 40 students printing once, far below 140 students a day over three months.
Earlier facts that bear on the decision:
F2: Northgate is closed for works from 1 December for about nine months, so it is shut from April to June.
Must state:
- Northgate closes from 1 December
F4: Riverside is busiest at weekends, so moving two staff away on Saturdays and Sundays hurts the busiest branch.
Must state:
- Riverside is busiest at weekends
Options those facts make unsound: A

--- Memo ---
**DECISION MEMO — Exam-Season Study Space Plan**
*Eastvale Libraries | 20 October 2036*

---

**Recommendation**

**Option C** — partner with the city college to host the April–June 2037 exam-season study programme in its hall. This is conditional on the college offering evening access, acceptable terms, and confirmation that students are willing to use the space. If those conditions cannot be met, Option B is the fallback.

---

**Problems with the plan**

The plan as submitted is not deliverable, and has several further weaknesses:

1. **Northgate will be closed.** The branch closes for fire-safety works from 1 December 2036 for approximately nine months, reopening around September 2037 at the earliest. The programme runs April to June 2037 — entirely inside the closure window. Option A's premise does not exist.

2. **Hours contradict the evidence.** The plan runs 9am–5pm. The student survey cited in the plan states that most students need study space after 5pm. A programme that ignores its own evidence base is not solving the actual problem.

3. **Seat capacity is insufficient even hypothetically.** 150 seats against peak demand of 210 students means turning away roughly 60 students on the busiest days.

4. **Weekend staffing is the wrong direction.** Moving two staff from Riverside — the busiest branch at weekends — to cover a closed building degrades service at exactly the location under most pressure, and still requires 60 days' union notice.

5. **The printing budget is unrealistic.** $400 based on 40 students printing once bears no relationship to a programme serving up to 210 students a day over ten weeks. A costed cap and usage policy are needed regardless of which option is chosen.

---

**Options weighed**

- **Option A** — eliminated. Northgate is closed throughout the programme period.
- **Option B** — feasible in principle across the four open branches, but requires a confirmed capacity audit, a booking system procured and stood up in time, and a solution to evening access that navigates the staffing freeze and union notice requirements already on record. Not a simple fallback.
- **Option C** — sidesteps the staffing freeze, rota constraints, and capacity limitations entirely. If the college hall offers evening hours, it directly addresses the gap the student survey identified and is likely the most cost-efficient route.

---

**What would change this recommendation**

If the college cannot offer evening hours, imposes significant costs, or students are unwilling to use a college

Cells to score for 1648ce9b: C1, C2, C3, C4, C5

=== Packet 41560b5b, 29 of 125 ===

Organisation: Eastvale Libraries, a city library service with five branches

--- Operator message 1 of 5 ---
Today is Monday 6 October 2036.

Hello panel. I'm the service manager for Eastvale Libraries, the
city's library service with five branches. Over the next few weeks
I'll bring you plans to critique. Please keep these facts in mind:

- The city council has frozen our staffing at 42 posts until next
  April. No new posts of any kind, full-time or part-time.
- Our Northgate branch failed a fire-safety inspection. The council
  will close it for works from 1 December, for about nine months.
- Our e-book contract with Pageway renews automatically on
  1 November each year unless we cancel at least 90 days before.
- Riverside is our busiest branch at weekends, with twice the
  visitors of any other branch.
- A local foundation has given us a one-off grant of $60 000. It may
  only be spent on digital-skills classes, within 12 months.
- Our agreement with the staff union requires 60 days' notice before
  any change to staff rotas.

There is no plan today. Please just confirm you have this.

--- Operator message 2 of 5 ---
Today is Monday 13 October 2036.

Plan to critique: evening opening.

Library users have asked for later opening. The plan opens all five
branches until 9pm on weekdays from 3 November, three hours later
than now.

Evidence: 30 comment cards asking for evening hours. The plan
expects evening visits to match daytime visits.

Staffing: current staff move onto a new rota that includes late
shifts, and we hire four part-time evening assistants at 20 hours a
week each.

Cost: the plan says the only extra cost is the assistants' pay: four
assistants × 20 hours × $23 = $1 840 a week, budgeted at $46 000 a
year.

Decision needed: (A) open all five branches until 9pm from
3 November as planned, (B) open our two busiest branches until 9pm
from February, with current staff on a new rota, or (C) run a
three-month pilot at one branch from 3 November, with two new
part-time evening assistants.

Please critique the plan and recommend one option.

--- Operator message 3 of 5 ---
Today is Monday 20 October 2036.

Plan to critique: study space for exam season.

Last year about 140 students a day used our study spaces during exam
season, and up to 210 on the busiest days. The plan runs next year's
exam-season programme, April to June, in the Northgate branch's
reading room, our largest room, with 150 seats.

Opening: 9am to 5pm, the branch's normal hours. In last year's
student survey, most students said they need study space after 5pm.

Staffing: two staff move from Riverside to Northgate every Saturday
and Sunday, when Northgate is normally closed.

Extras: free printing for students, budgeted at $400, based on 40
students printing 10 pages once.

Decision needed: (A) run the programme at Northgate as planned,
(B) spread study space across the other branches with online seat
booking, or (C) partner with the city college to use its hall.

Please critique the plan and recommend one option.

--- Operator message 4 of 5 ---
Today is Monday 27 October 2036.

Plan to critique: our payroll software.

Our payroll software costs $9 000 a year, and its supplier stops
supporting it next year. The options:

(A) switch to CloudPay at $6 000 a year, plus a $4 500 set-up fee, on
a three-year contract with no early exit;
(B) join the city council's shared payroll service, free to us, with
a two-month waiting list;
(C) move to the supplier's new version at $12 000 a year.

The plan recommends CloudPay: it saves $3 000 a year, and the set-up
fee is a one-off. It expects the switch to take two weeks, the figure
from CloudPay's sales demonstration.

Decision needed: choose (A), (B) or (C).

Please critique the plan and recommend one option.

--- Operator message 5 of 5, the request the memo answers ---
Today is Monday 3 November 2036.

Plan to critique: changing our e-book supplier.

Pageway raised its prices by 18% this year. The plan moves us to a
new supplier, Lumen Reads, from 1 January, and cancels Pageway in
December.

Money: Lumen's first-year licence of $40 000 would be paid from the
foundation grant.

Price comparison: Pageway charges $1.10 per loan, and we made 40 000
loans last year, $44 000. Lumen charges a flat $40 000 a year, which
covers up to 35 000 loans.

Catalogue: the plan assumes Lumen carries every title Pageway does.
Readers' holds and reading lists will be moved "as needed".

Decision needed: (A) switch to Lumen Reads as planned, (B) stay with
Pageway for another year and plan a switch properly, or (C)
negotiate a price cap with Pageway.

Please critique the plan and recommend one option.

--- Answer key ---
Problems inside the plan:
K1: Lumen's flat fee covers only 35 000 loans, but we made 40 000 last year; the comparison ignores the cap.
K2: It assumes Lumen carries every Pageway title without checking.
K3: There is no real plan for moving readers' holds and reading lists.
Earlier facts that bear on the decision:
F3: Pageway renewed automatically on 1 November because nobody cancelled 90 days before, so cancelling in December is too late.
Must state:
- Pageway has already renewed on 1 November
F5: The foundation grant may only be spent on digital-skills classes, not on an e-book licence.
Must state:
- the grant is only for digital-skills classes
Options those facts make unsound: A

--- Memo ---
**Decision Memo — E-book Supplier Review**
*Eastvale Libraries | 3 November 2036*

---

**Recommendation**

**Option B — stay with Pageway for this contract year, and plan a switch properly.**

Pursue Option C (price negotiation with Pageway) in parallel as a cost-mitigation measure, but B is the primary recommendation.

---

**Problems with the plan**

The plan as written cannot be executed. Two hard blockers apply before any other consideration:

1. **The Pageway contract has already renewed.** The 90-day cancellation window closed around 2 August 2036. A December cancellation and January switch are not contractually available. Pageway is owed for the full year regardless of what is decided today.

2. **The grant cannot fund this.** The $60k foundation grant is ring-fenced for digital-skills classes within 12 months. A platform licence is an operational procurement cost, not a skills programme. Using it for Lumen's $40k fee risks breach of grant conditions and clawback of the entire grant.

Beyond those blockers, the plan contains unverified assumptions: catalogue parity with Pageway is stated but unconfirmed; Lumen's overage terms above 35,000 loans are unknown (Eastvale made 40,000 loans last year, 14% above the cap); and the financial comparison uses last year's Pageway price rather than the post-18%-rise figure of approximately $52,000. A January migration would also coincide with Northgate's closure, creating a digital-demand spike at the worst possible moment.

---

**Options weighed**

| Option | Assessment |
|---|---|
| A — Switch to Lumen, 1 January | Not executable: contract and grant blockers are absolute |
| B — Stay with Pageway, plan properly | Only available path; preserves service continuity |
| C — Negotiate a price cap with Pageway | Worth pursuing now, but not a substitute for a proper review |

---

**What would change this recommendation**

A switch to Lumen Reads could be recommended for the November 2037 renewal window if, before serving notice in August 2037:

- A formal catalogue-overlap analysis confirms Lumen carries the titles Eastvale readers use.
- Lumen's overage pricing is obtained in writing and modelled at realistic loan volumes.
- An eligible budget line for the licence fee is confirmed (or a grant-eligibility ruling obtained).
- A costed migration plan is in place, accounting for the staffing freeze and Northgate's reopening.

The long-term case for switching remains credible. This plan is not ready.

Cells to score for 41560b5b: C1, C2, C3, C4, C5

=== Packet 680039b3, 30 of 125 ===

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
## Decision Memo — Westfield Automated Racking
**Kestrel Freight | 3 November 2036**

---

### Recommendation

**Option C — Postpone and re-plan.** The plan contains material errors and unquantified risks that make it impossible to approve. A focused 90-day re-plan, not an open-ended deferral, is recommended.

---

### Problems with the plan

**Overstated capacity.** The supplier's 60% uplift assumes 24-hour operation. Westfield runs 16 hours a day; the realistic gain for your operating pattern is closer to 40%.

**Wrong payback figure.** The plan ignores the $140k annual maintenance cost. Corrected simple payback is 5.5 years, not four. Once missing capital costs are added — structural survey, WMS integration, staff redundancy, and contingency — the all-in cost is likely $2.6m–$3.0m, pushing payback to 6.8–7.8 years.

**Incomplete capital cost.** The $2.1m supplier quote excludes roof-beam structural work, WMS integration, redundancy, contingency, and insurance uplift. These are not optional line items.

**Unrealistic installation assumption.** Hanging shuttle rails from roof beams above a live picking floor will require safety exclusion zones and phased commissioning. Expect 30–50% reduced throughput for ten weeks. The plan makes no provision for this disruption.

**Unscoped WMS dependency.** Automated racking without a working WMS interface is inoperable. Integration scope, ownership, and fallback arrangements are absent.

**No structural survey.** Load capacity of the roof beams must be confirmed before any contract is signed. Remedial steelwork, if needed, affects both cost and timeline.

---

### Options weighed

**Option A** cannot be approved as written. The errors above are not minor; they affect the core business case and operational safety of the rollout.

**Option B** (floor-standing racking at the other warehouse) has genuine merit — lower capital, simpler installation, no roof-beam risk — and should be evaluated on its own terms in parallel with the Westfield re-plan, not treated as a consolation choice.

**Option C** is the right call, provided the delay is purposeful and time-bounded.

---

### What would change this recommendation

A revised plan that corrects the capacity figure for 16-hour operation; provides a full all-in cost model with contingency; completes a structural survey; scopes and costs the WMS integration with a named owner; and sets out a credible disruption management plan for the installation period. Bring that back and Option A becomes approvable.

Cells to score for 680039b3: C1, C2, C3, C4, C5


## Your reply

After the model line, give one short reason for each cell. End your reply
with one JSON block, fenced as ```json, that maps each packet ID above to
its cells: a memo's criteria as the numbers 0, 1 or 2, and a recall reply's
questions as "right" or "wrong". Its shape, with every score left out:

    {"<packet ID>": {"C1": ..., "C2": ..., "C3": ..., "C4": ..., "C5": ...},
     "<packet ID>": {"R1": "...", "R2": "..."}}

Give every packet above, every cell listed for it, and nothing else.
