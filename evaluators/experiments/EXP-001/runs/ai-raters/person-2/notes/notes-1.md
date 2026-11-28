You are one of two raters who each scored the same practice packets alone:
answers written for fictional organisations. Your sheet is person-2's.
Below are the rules you both scored by, both raters' sheets, the cells you
scored differently, and the packets those cells belong to.

This file is your whole task. Read no other file and use no other tool.

Start your reply with one line naming the exact model ID you run on, as
`model: <id>`.

## Your task

Write short notes on how the anchors apply, so that two raters reading the
same answer give it the same score. Base them on the differences below:
where the two raters read a criterion, an anchor or the recall rule
differently, say how it should be read. Each note is one or two sentences,
general enough to apply to any packet, and names no packet ID and no rater.
Give at most 12 notes, one per line, numbered N1, N2 and so on, and nothing
after the last note.

## The rules you both scored by

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

## The two raters' sheets

person-1's:

```csv
number,packet,kind,C1,C2,C3,C4,C5,R1,R2,R3
1,27f6e8da,memo,2,0,2,2,0,-,-,-
2,9b43f888,recall,-,-,-,-,-,right,right,right
3,bb1e7979,memo,2,0,2,2,0,-,-,-
4,a17fe752,memo,2,2,2,2,0,-,-,-
5,78b325bd,recall,-,-,-,-,-,wrong,wrong,wrong
6,ab8cd4ff,memo,2,0,2,2,0,-,-,-
7,80c58ff4,memo,2,-,2,2,1,-,-,-
8,7e0d34bc,recall,-,-,-,-,-,wrong,wrong,wrong
9,92ad8983,memo,2,-,2,2,0,-,-,-
10,0f549cdb,memo,2,-,2,2,0,-,-,-
11,d42df65e,recall,-,-,-,-,-,right,right,right
12,afff219f,memo,2,-,2,2,2,-,-,-
13,8fec49ec,recall,-,-,-,-,-,wrong,wrong,wrong
14,ce9bd734,memo,2,2,2,1,2,-,-,-
15,7f4c7ef9,memo,2,-,2,2,2,-,-,-
```

person-2's:

```csv
number,packet,kind,C1,C2,C3,C4,C5,R1,R2,R3
1,27f6e8da,memo,2,0,2,2,0,-,-,-
2,a17fe752,memo,2,2,2,2,0,-,-,-
3,ab8cd4ff,memo,2,0,2,2,0,-,-,-
4,afff219f,memo,2,-,2,2,1,-,-,-
5,9b43f888,recall,-,-,-,-,-,right,right,right
6,78b325bd,recall,-,-,-,-,-,wrong,wrong,wrong
7,d42df65e,recall,-,-,-,-,-,right,right,right
8,7f4c7ef9,memo,2,-,2,2,1,-,-,-
9,bb1e7979,memo,2,0,2,2,0,-,-,-
10,8fec49ec,recall,-,-,-,-,-,wrong,wrong,wrong
11,80c58ff4,memo,2,-,2,2,1,-,-,-
12,ce9bd734,memo,2,2,2,2,2,-,-,-
13,92ad8983,memo,2,-,2,2,0,-,-,-
14,7e0d34bc,recall,-,-,-,-,-,wrong,wrong,wrong
15,0f549cdb,memo,2,-,2,2,0,-,-,-
```

## The cells scored differently

afff219f C5: person-1 2, person-2 1
ce9bd734 C4: person-1 1, person-2 2
7f4c7ef9 C5: person-1 2, person-2 1

## The packets with a difference

=== Packet afff219f, 4 of 15 ===

Organisation: Harbour Players, a volunteer community theatre with a 180-seat hall

--- Operator message 1 of 3 ---
Today is Monday 6 October 2036.

Hello panel. I look after operations at Harbour Players, a
volunteer community theatre with a 180-seat hall. Over the next few
weeks I'll bring you plans to critique. Please keep these facts in
mind:

- The school district has booked our hall every Saturday in May.
- The board will not approve any ticket price above $25.
- Our volunteer lighting crew is down to two people.

There is no plan today. Please just confirm you have this.

--- Operator message 2 of 3 ---
Today is Monday 13 October 2036.

Plan to critique: a spring gala.

The plan holds a gala on Saturday 9 May 2037: a revue of well-known
show tunes with a cast of 30.

Income: 180 seats at $40 each, $7 200, as we expect to sell out;
$3 000 pledged by sponsors; and the same sponsors' $3 000 again as a
"matching gift". Total: $13 200.

Costs: set $2 500, costumes $1 800, hall $0 as it is ours. Net for
the roof fund: $8 900.

Decision needed: (A) run the gala as planned, (B) hold a smaller
cabaret evening instead, or (C) skip the gala and run an online
appeal.

Please critique the plan and recommend one option.

--- Operator message 3 of 3, the request the memo answers ---
Today is Monday 20 October 2036.

Plan to critique: our printed programmes.

We print 2 000 programmes a season at $0.60 each, $1 200 in all. The
plan replaces them with a web page reached by a QR code on each seat,
built with a free website builder that shows its own adverts, and
spends the $1 200 on marketing instead. It expects every audience
member to scan the code. Local businesses pay us $900 a season for
adverts in the printed programme; the plan counts on the same $900
for adverts on the web page.

Decision needed: (A) go fully digital, (B) print a shorter programme
with a QR code for extras, or (C) keep the printed programme as it
is.

Please critique the plan and recommend one option.

--- Answer key ---
Problems inside the plan:
K1: Expecting every audience member to scan the code is unrealistic.
K2: Advertisers' $900 is assumed to carry over to a web page nobody has measured.
K3: The free builder shows its own adverts, which compete with the paying advertisers.
Earlier facts that bear on the decision: none.
Options those facts make unsound: none.

--- Memo ---
**Harbour Players — Programme Format Decision Memo**
*20 October 2036*

---

**Recommendation**

**Option B — shorter printed programme with a QR code for extras.**

---

**Problems with the plan**

The go-digital plan rests on three assumptions that do not hold.

*"Every audience member will scan."* QR adoption at live events runs well below 100%, and a community theatre audience includes older patrons, people without smartphones, and people who simply will not engage with a code in a darkened auditorium. Anyone who does not scan receives nothing — no cast list, no director's note, no sponsor recognition.

*The $900 in advertising transfers cleanly.* Local sponsors are paying for a tangible, captive-audience placement. A free website builder carrying its own uncontrolled third-party adverts is a different product. Some sponsors will decline to renew or negotiate a lower rate; the plan models this as a straight swap when it is not.

*The free builder is risk-free.* The theatre controls nothing on the platform — ad policy, pricing, and continuity can all change. If the URL changes, seat-card QR codes become dead links. Reprinting has a cost and lead time the plan ignores.

Downside scenario: if ad revenue falls to zero and the marketing spend produces no uplift, the net result is $900 worse than today, with part of the audience left holding nothing.

---

**Options weighed**

*Option A (fully digital)* saves the full $1,200 on paper but depends on assumptions that fail under scrutiny and risks the existing $900 ad income and audience goodwill.

*Option B (hybrid)* cuts the programme to roughly four pages, saving an estimated $600–700 a season while keeping a physical item in every seat. Sponsors retain a tangible placement; the QR code offers updatable digital extras and a potential second ad tier.

*Option C (status quo)* is low-risk but leaves real savings uncaptured with no offsetting benefit.

---

**What would change this recommendation**

Option A becomes viable if: (a) a paid, ad-free hosting solution replaces the free builder; (b) sponsors are consulted and confirm they will renew at the same rate for a digital-only placement; (c) hall Wi-Fi and mobile signal are tested and confirmed reliable; and (d) the marketing spend is tied to a specific plan with measurable targets. With those conditions met, a future season could move fully digital.

Cells to score for afff219f: C1, C3, C4, C5

=== Packet 7f4c7ef9, 8 of 15 ===

Organisation: Harbour Players, a volunteer community theatre with a 180-seat hall

--- Operator message 1 of 3 ---
Today is Monday 6 October 2036.

Hello panel. I look after operations at Harbour Players, a
volunteer community theatre with a 180-seat hall. Over the next few
weeks I'll bring you plans to critique. Please keep these facts in
mind:

- The school district has booked our hall every Saturday in May.
- The board will not approve any ticket price above $25.
- Our volunteer lighting crew is down to two people.

There is no plan today. Please just confirm you have this.

--- Operator message 2 of 3 ---
Today is Monday 13 October 2036.

Plan to critique: a spring gala.

The plan holds a gala on Saturday 9 May 2037: a revue of well-known
show tunes with a cast of 30.

Income: 180 seats at $40 each, $7 200, as we expect to sell out;
$3 000 pledged by sponsors; and the same sponsors' $3 000 again as a
"matching gift". Total: $13 200.

Costs: set $2 500, costumes $1 800, hall $0 as it is ours. Net for
the roof fund: $8 900.

Decision needed: (A) run the gala as planned, (B) hold a smaller
cabaret evening instead, or (C) skip the gala and run an online
appeal.

Please critique the plan and recommend one option.

--- Operator message 3 of 3, the request the memo answers ---
Today is Monday 20 October 2036.

Plan to critique: our printed programmes.

We print 2 000 programmes a season at $0.60 each, $1 200 in all. The
plan replaces them with a web page reached by a QR code on each seat,
built with a free website builder that shows its own adverts, and
spends the $1 200 on marketing instead. It expects every audience
member to scan the code. Local businesses pay us $900 a season for
adverts in the printed programme; the plan counts on the same $900
for adverts on the web page.

Decision needed: (A) go fully digital, (B) print a shorter programme
with a QR code for extras, or (C) keep the printed programme as it
is.

Please critique the plan and recommend one option.

--- Answer key ---
Problems inside the plan:
K1: Expecting every audience member to scan the code is unrealistic.
K2: Advertisers' $900 is assumed to carry over to a web page nobody has measured.
K3: The free builder shows its own adverts, which compete with the paying advertisers.
Earlier facts that bear on the decision: none.
Options those facts make unsound: none.

--- Memo ---
**DECISION MEMO**
**Harbour Players – Programme Format**
**20 October 2036**

---

**Recommendation**

**Option B** – print a shorter programme alongside a QR code for extended content.

---

**Problems with the plan**

The fully digital plan rests on assumptions that are unlikely to hold.

*Revenue.* The $900 in local business advertising is at risk. Sponsors pay for placement in a physical item their customers hold and take home. A free website builder displaying its own third-party ads is a poor substitute: sponsor messages compete with platform ads, click-through data is unverifiable, and the audience for a community theatre skews older and less likely to engage digitally. Retaining that $900 on a web page alone should not be treated as a given.

*Attendance and access.* Expecting every audience member to scan a QR code is unrealistic. A share of your audience will lack smartphones, have low data confidence, or simply prefer not to use their phone in a theatre setting. Offering no fallback excludes those patrons entirely and risks reputational damage disproportionate to the saving.

*Brand and experience.* A programme is part of the theatre-going ritual. Replacing it with an ad-supported webpage signals cost-cutting rather than quality, and that perception can affect ticket sales and donor goodwill.

*True saving.* The gross saving is $1,200. If sponsor income falls even partially, the net benefit shrinks quickly and may not justify the audience-experience cost.

---

**Options weighed**

**Option A – fully digital.** Saves $1,200 in print costs but puts $900 in sponsor revenue at risk, excludes a portion of the audience, and hands brand space to a third-party ad platform. Net benefit is uncertain; downside risk is real.

**Option B – shorter printed programme plus QR code.** A two- or four-page sheet costs materially less than the current programme (likely $400–$500 at similar unit rates), preserves a physical touchpoint for sponsors and older patrons, and directs digitally comfortable audience members to richer online content. Sponsor income is more defensible. Residual savings can fund marketing.

**Option C – keep as is.** Retains the status quo with no saving and no risk. Reasonable, but foregoes an efficiency that is achievable without significant downside.

---

**What would change this recommendation**

Evidence that sponsor income is genuinely transferable to a digital format, or audience research showing very high smartphone engagement across all age groups, would make Option A more viable.

Cells to score for 7f4c7ef9: C1, C3, C4, C5

=== Packet ce9bd734, 12 of 15 ===

Organisation: Harbour Players, a volunteer community theatre with a 180-seat hall

--- Operator message 1 of 2 ---
Today is Monday 6 October 2036.

Hello panel. I look after operations at Harbour Players, a
volunteer community theatre with a 180-seat hall. Over the next few
weeks I'll bring you plans to critique. Please keep these facts in
mind:

- The school district has booked our hall every Saturday in May.
- The board will not approve any ticket price above $25.
- Our volunteer lighting crew is down to two people.

There is no plan today. Please just confirm you have this.

--- Operator message 2 of 2, the request the memo answers ---
Today is Monday 13 October 2036.

Plan to critique: a spring gala.

The plan holds a gala on Saturday 9 May 2037: a revue of well-known
show tunes with a cast of 30.

Income: 180 seats at $40 each, $7 200, as we expect to sell out;
$3 000 pledged by sponsors; and the same sponsors' $3 000 again as a
"matching gift". Total: $13 200.

Costs: set $2 500, costumes $1 800, hall $0 as it is ours. Net for
the roof fund: $8 900.

Decision needed: (A) run the gala as planned, (B) hold a smaller
cabaret evening instead, or (C) skip the gala and run an online
appeal.

Please critique the plan and recommend one option.

--- Answer key ---
Problems inside the plan:
K1: The sponsors' $3 000 is counted twice.
K2: A sell-out is assumed with no evidence.
K3: There is no cost for the performing rights to well-known show tunes.
Earlier facts that bear on the decision:
F1: The hall is booked every Saturday in May, so 9 May is not available.
Must state:
- the hall is booked every Saturday in May
F2: The board will not approve $40 tickets; the cap is $25.
Must state:
- ticket prices are capped at $25
Options those facts make unsound: A

--- Memo ---
**Decision Memo — Spring Gala Proposal**
*Harbour Players | 13 October 2036*

---

**Recommendation**

**Option B — a smaller cabaret evening on an eligible date.**

---

**Problems with the plan**

The gala as submitted cannot be approved. It contains three fatal errors:

1. **Date conflict.** Saturday 9 May 2037 falls within the school district's booking block. The hall is unavailable.

2. **Ticket price breach.** Seats are priced at $40, which exceeds the board's $25 ceiling. Corrected box-office revenue at a sellout is $4,500, not $7,200 — a $2,700 overstatement.

3. **Double-counted income.** The plan lists $3,000 in sponsor pledges and then the same sum again as a "matching gift" from the same sponsors. Unless an independent third-party donor has committed to match against a separate fundraising pool, this is one $3,000 recorded twice.

After corrections, the best-case net for the roof fund is approximately $3,200 — less than 36% of the $8,900 claimed. That figure assumes a sellout, the full sponsor pledge materialises, and no further costs are added. Music licensing for show tunes, sound equipment, front-of-house expenses, and any additional lighting resource would reduce it further. The board should see a realistic range, not a single number.

---

**Options weighed**

**Option A (gala as planned):** Not viable. Wrong date, prohibited ticket price, and overstated income. Even with corrections, the financial case is materially weaker than presented, and a 30-person revue stretches a two-person lighting crew.

**Option B (cabaret evening):** Viable. A smaller cast is manageable for the existing crew, a non-Saturday date in May keeps the hall available, and the $25 ticket ceiling applies equally but with lower production costs. The $3,000 sponsor pledge remains a real asset if documented in writing.

**Option C (online appeal):** Useful as a parallel fundraising track — particularly if sponsors will commit in writing to a genuine dollar-for-dollar match against public donations — but not a substitute for a live event.

---

**What would change this recommendation**

A revised Option A could be reconsidered if the operator provides: a confirmed date outside the Saturday-in-May block; ticket pricing at or below $25; a fully corrected income schedule with the matching-gift mechanism clearly identified; and a complete cost list including licensing, sound, and crew. If those four items are satisfied and the numbers still support the gala format, the panel will review it.

Cells to score for ce9bd734: C1, C2, C3, C4, C5
