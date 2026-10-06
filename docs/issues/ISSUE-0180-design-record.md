# ISSUE-0180 — the measurements behind the fix (record)

**Companion to**: [ISSUE-0180](ISSUE-0180-topic-facts-reachable-only-by-their-exact-subject.md)
**Covers**: why the fix has the shape it has — how the issue's four options measured on the practice run, and what a paid probe of the extractor's prompt showed
**Fix**: described in the issue's Fix section

Split out of [ISSUE-0180](ISSUE-0180-topic-facts-reachable-only-by-their-exact-subject.md)
on 2026-10-06, when its Fix section took the issue past the 3 000-word
limit. Splitting rather than trimming follows the precedent of
[ISSUE-0124](ISSUE-0124-design-record.md): these two notes are what the
choice among the options rests on, and a resolved issue that has lost them
is a worse record than a longer one.

The first note is moved whole, as it stood in the issue's Notes. The
second was written with the fix and has only ever stood here. "The note
above" in the second is the first. "The 2026-10-05 note above" in the
first is the issue's own note of that date, which stays there.

---

## Notes

> 2026-10-06 — the four options were measured on the first practice run,
> with no model calls. Each of the 25 turns where an arm D adviser spoke
> in the three later meetings was replayed through the shipped recall and
> prompt code, against the store that adviser held when the meeting
> began, written again with ISSUE-0181's fix
> ([#1034](https://github.com/mkhomutov/Persatrix/pull/1034)). Only the
> rule under test was swapped. As a check, the shipped rule on the run's
> own stores brings back the same two facts the run recalled, at the same
> times. The count is of prompts that hold a briefing fact: the hall
> booking, the $25 cap or the lighting crew. As shipped, none of the 25
> does.
>
> 1. **Option 1 changes nothing here.** An adviser's store gained a
>    topic subject 49 times. Sixteen were at the first close, when the
>    store was empty, and these hold every briefing fact. Twelve named
>    something that only a record closed with them had just named; those
>    extractions run side by side and cannot see each other's subjects.
>    Fifteen named a new topic. Six could have reused a stored subject,
>    all at the last close, and renaming those six leaves every count as
>    it was. Ideal naming does little for the shipped rule either: with
>    everything about the theatre under "harbour players", 3 of 25
>    prompts hold the briefing, because a message must still say
>    "Harbour Players". Merged subjects also cost facts under the
>    current write rule. Of the 121 stored facts, 31 to 44 stop being
>    live, nearly all because records closed together replace each
>    other's facts on a shared subject and predicate (the 2026-10-05
>    note above).
> 2. **Option 2 helps, mostly through one word.** Seeding a subject when
>    the message shares any one of its content words puts a briefing
>    fact in 17 of 25 prompts and all three in 8. But the advisers filed
>    the briefing under "event plan" or "event planning", and every
>    opening message says "plan". With that word ignored, the $25 cap
>    falls from 15 prompts to 3 and the lighting crew from 15 to 4. The
>    recall questions' own words match no subject, and the chair's memo
>    turns get nothing. Asking for every word, or most, reaches 3 or 4
>    prompts. A made-up subject such as "plan review" would be seeded on
>    18 of 25 turns, where today it needs the whole phrase.
> 3. **Option 3 as written gives one fact.** Only the subject "harbour
>    players" appears whole in the room's description, and it holds the
>    lighting crew for two advisers: 9 of 25 prompts, none of them the
>    chair's. Seeding a subject when all its words are in the
>    description adds the hall booking to all 25, the chair's memo turns
>    included, but only because the description says "hall". With half
>    its words, up to five "harbour players …" subjects compete for
>    three seeds once the first plan meeting has added its own, and the
>    newest ones win, so the $25 cap reaches 6 prompts. The description
>    already reaches the agent on every channel turn, with the roster,
>    but after fact recall has run. A planted subject that is a phrase
>    of the description would be seeded on every turn in the room.
> 4. **Option 4 adds routes, not facts.** Five briefing rows sit under a
>    person predicate, `avoids`. With ISSUE-0181's fix, each of those
>    facts is also live under a topic predicate in the same store. Alone
>    it changes nothing. With option 2 the hall booking goes from 10
>    prompts to 17, and with option 3 as written the $25 cap goes from 0
>    to 9. The chair gains nothing. Reading every row fails
>    `test_topic_seed_reads_only_topic_rows`, the test that pins the
>    bound, and so do two of the three narrower forms tried. Reading
>    only preference and commitment predicates passes it. In a check
>    with one planted topic tuple naming a colleague, that form still
>    showed the colleague's stated dislike on 3 of 20 turns from other
>    senders once option 2 was in.
>
> No option, alone or paired with another, puts all three facts into the
> chair's memo prompt in the recall meeting within the cap of three
> seeds. One combination does, in all 25 prompts: one subject per
> organisation from the first close, option 3 as written, and
> ISSUE-0181's fix. A list of stored subjects cannot produce that
> naming, since it is chosen while the store is empty. Whether a line in
> the extractor prompt can is untested and needs model calls.
>
> What this does not show: the transcripts are held fixed, so it counts
> which facts reach a prompt, not what the advisers would then say. It
> is one run and four stores, and the rules were tried on the practice
> series only. In the unit and integration suites no test fails under
> option 2, and option 4 fails only the test named above. No golden
> trace moves under options 2 or 4, and option 3 cannot move one, since
> no room in a golden has a description. None of them covers these
> cases: the goldens hold one-word subjects.

> 2026-10-06 — the question the note above left open, asked with model
> calls at the maintainer's go-ahead: can a sentence in the extractor
> prompt get an organisation's facts filed under the organisation at the
> first close? The call log keeps no prompt text, so arm D's 45
> close-path prompts were rebuilt from each episode row's turns and the
> channel store's message bodies, and sent through the tree's own close
> path. The provider's token count matched the run's call log on all 45
> (41 675 input tokens). A pass holds the 45 calls again on the run's
> model and settings with only the extractor's text swapped, writes the
> tuples through the tree's write path, and replays the 25 turns as the
> note above did.
>
> | Extractor wording | Operator's briefing record files all three facts under "harbour players" | Prompts holding all three, with option 3 as written |
> |---|---|---|
> | Shipped | 0 of 4 | 1 of 25 |
> | One added sentence, the one this issue's fix ships | 28 of 28 | 25 of 25, in both full passes |
> | The topic sentence rewritten | 4 of 4 | 25 of 25 |
>
> The 28 are two full passes and five repeats of the first close. The
> sentence without option 3 reaches 3 or 4 of the 25 prompts, so both
> halves are needed. The chair's three memo turns hold all three facts
> wherever the 25 do.
>
> Write order did not matter: 30 shuffles of the records closed together
> left every count as it was. Only the operator's message names the
> theatre in the briefing, so no other record wrote its subject there (0
> of 48). Replacement between records cost 5 to 8 facts of about 100 and
> never a briefing fact, where the renaming bound above had 31 to 44 of
> 121: the extractor keeps a named plan under the plan's own name
> ("spring gala"), so the organisation's subject takes about a quarter of
> the topic tuples, not all of them.
>
> The facts budget is what the practice series could not show. Its
> largest facts section was 134 of 200 tokens, after two plan meetings; a
> scored series has four. With copies of the plan meetings written as
> later meetings of their own (no model calls), the practice mix doubled
> still fits. With three meetings that each file as the first plan
> meeting did, the briefing is cut for two or three of the four advisers,
> the chair included, because the section lists a subject's newest facts
> first. The fix reads such a subject from both ends for that reason.
> That keeps the briefing only because nothing was filed under the
> organisation before it: in all 28 extractions of the operator's
> briefing record with the added sentence, its three facts are the
> subject's three oldest rows.
>
> Two side effects. On the chair's memo-request record, whose text names
> no organisation, the added sentence made the extractor use the
> channel's own name (`advice-2`), three tuples a pass. The rewrite did
> not, in its one pass. And records write over each other more: written
> in close order, 8 and 7 rows of the two full passes were replaced by
> another record's, none with the shipped wording, and in 2 of the 15
> groups of records closed at one instant two records wrote one subject
> and predicate, none before (both under "spring gala", in the first
> plan meeting).
>
> Cost: 230 calls, 212 705 input and 44 905 output tokens, $1.31 at the
> experiment's prices. They were made with the experiment's key and are in
> no harness call log. What this does not show is what the note above
> lists: the transcripts are held fixed, it is one practice series and
> one organisation, and the scored materials were not opened.
