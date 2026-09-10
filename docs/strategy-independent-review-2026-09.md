# Independent strategy review — September 2026

> **Date**: 2026-09-10, at `main` = `f52e7ff6`, with v0.3.16 in progress.
> **What this is**: an outside-in look at the idea, the direction and the implementation strategy, ending in suggestions. Nothing here is ratified — acting on it would go through a [sequencing amendment](methodology/decisions.md#sequencing-amendments).
> **Evidence**: every number is in the [evidence file](strategy-independent-review-2026-09-evidence.md), with the command that produced it.

## The short answer

- **The idea is promising, but nobody owns it yet.** "Persona agents that remember people, talk to each other in shared channels, and act on their own" describes an experience, not a job someone needs done. After five months and 21 releases, no one outside the maintainer is known to use Persatrix, and no document names the first user.
- **The direction drifted from its own stated goal.** On 2026-06-04 the project decided in writing that attracting users came first. It then built the right demo — persona agents that discuss a topic, stop, and write a synthesis with no human present (v0.3.11) — and, as far as the repository and traffic show, never took it to anyone. Since 2026-07-25, five releases (counting v0.3.16, in progress) have gone to how memory moves between channels and people — mostly keeping people's memories apart, recording who said what, and checking who is listening: safety for multi-person deployments that do not exist yet.
- **The engineering is excellent and aimed at the wrong risk.** Tests, reviews, gates and cheap live runs make regressions rare. What ends projects like this is different: nobody wants the thing, or a simpler tool does the job as well. Nothing in the process measures that.
- **A real future exists, but not on the current path.** As a general-purpose "agent society engine" under a restrictive license, Persatrix faces funded, permissively licensed competitors with large communities in every layer. It can have a future if it narrows to what others do not do — **multi-agent discussions that finish, stay within a hard budget, and respect who is listening** — proves with one experiment that this beats the simple alternative, and makes it easy to try. Spend the next 8–10 weeks finding out, not building v0.4.0.

## How this review was done

I read the README, both founding specs, the ROADMAP, the sequencing log and every amendment, the v0.3.16 plan, the RFC and issue indexes and the methodology; measured code, tests, docs and the whole commit and pull-request history; sampled core code; read GitHub's traffic data; and checked the market as of September 2026. I wrote it without reading the three strategy reviews open at the time (#895, #896, #897). Limits: I did not run the stack, market figures are public reporting, and traffic data covers 14 days.

## What Persatrix is today

| Measure | Value |
|---|---|
| Age | 5 months — first commit 2026-04-08 |
| People | one maintainer — 872 of 891 commits; the rest are Dependabot and the initial commit |
| Releases | 21 tags (v0.1.0 to v0.3.15), one every seven days on average |
| Product code | about 124,000 lines — Go 45k, Python 56k, Rust 11k, TypeScript 12k |
| Tests | about 201,000 lines — 1.6 lines of test per line of code |
| Writing | about 1.4 million words of markdown, of which about 36,000 are user guides and the README |
| Outside use | 4 stars, 0 forks, 24 unique visitors in 14 days, no release with a downloadable file |
| License | BUSL-1.1 — free use only for non-commercial deployments serving fewer than 10 users |

A very large, very carefully built system that has not yet met its users.

## The idea

### What is strong

1. **Discussions that finish.** Frameworks commonly offer a turn order and a round limit. Persatrix goes further: each persona agent decides whether a reply is worth posting (a [salience bid](ai-glossary.md#salience-bid)), a [chair](ai-glossary.md#chair) writes the synthesis, members vote to end, and an autonomous channel cannot exist without a cost cap. A live v0.3.11 run debated a monorepo decision, closed itself after 99 seconds and left a synthesis, for $0.17. As workplace chat fills with agents, the chat vendors themselves now name the problem: agents deployed separately "work in silos, without shared context" ([Slack, 2026](strategy-independent-review-2026-09-evidence.md#the-market-in-september-2026)).
2. **Cost as a gate, not a report.** Every LLM call leases budget before it runs, per agent and per conversation, with a reserve for the closing summary. Gateways cap spend per API key; budgets that understand agents and conversations are rarer.
3. **Memory that asks "may I say this here?"** Memory is kept apart by run, channel, person and speaker, filtered by each channel's confidentiality, and v0.3.16 adds a check on who is present (in shadow mode). Most memory products store and retrieve; few ask whether a fact may be repeated in front of these people.
4. **Honest operations.** A $0 offline mode, any provider behind three model aliases, end-to-end traces, and live acceptance runs that cost cents.

### What is weak

1. **No named user, no named job.** The founding spec promises eight domains, from software teams to classrooms to game characters, "without code changes". The README sells three products: a persona you chat with, a team of agents in a channel, and a workflow runner. A newcomer cannot tell which problem Persatrix solves better than anything else. Platforms win by owning one first use, then widening.
2. **The core claim has never been tested.** The six eval sets pin recorded prompts and replies so that regressions show up. None compares a Persatrix discussion with a simpler route to the same result — one model arguing every side in a single call, or the same persona agents without memory. If one call writes an equally good synthesis for a tenth of the cost, the society machinery is not justified for that job. Today nobody knows.
3. **Every layer is crowded, and rivals are further along** ([details](strategy-independent-review-2026-09-evidence.md#the-market-in-september-2026)). Agent frameworks: LangGraph, CrewAI (built around role-playing agents), Microsoft Agent Framework, OpenAI Agents SDK (with sessions and hand-offs), Google ADK. Agent memory: Mem0, Letta, Zep and others, several funded. Simulated societies: Simile, reported at a $2 billion valuation, plus open-source research tools such as Concordia and OASIS. Shared channels: Slack calls itself an "agent-first workspace" and now puts coding agents in project channels beside people. Interoperability: MCP and A2A both sit in the Linux Foundation's Agentic AI Foundation — and Persatrix implements neither; its [MCP bridge](../agents/tools/mcp_bridge.py) is twelve lines of TODO comments. Persatrix cannot win on breadth here, only on depth in one place.

## The direction

### What the decision log shows

The [sequencing log](v0.3.x-sequencing.md) is candid, so the story reads straight off it:

1. **April — workflows, then persona agents.** The workflow engine (about 3,700 lines) has had no commits since 1 July; the product became persona agents, channels and memory. The README still sells the workflow runner.
2. **[2026-06-04](v0.3.x-sequencing.md#amendment-2026-06-04--re-sequence-the-v03x-tail-for-conversation-realism--usefulness-ahead-of-v040) — users first.** Attracting users became the explicit goal; confidentiality and identity work was deferred as invisible to a prospective user.
3. **June–July — the plan delivered.** v0.3.7 to v0.3.11 made group discussion realistic and useful, ending with the autonomous channel the [2026-06-28 amendment](v0.3.x-sequencing.md#amendment-2026-06-28--add-the-autonomous-agent-only-channel-as-the-v03x-realism-capstone) called the best adoption demo the project could ship.
4. **No adoption step followed.** No launch, packaged release, outreach or user conversation is recorded after April.
5. **[2026-07-25](v0.3.x-sequencing.md#amendment-2026-07-25--add-v0312-memory-that-travels-cross-channel-experience--accounts) — the axis changed** from conversation realism to memory realism, pulling forward the confidentiality work June had judged invisible to users. The [2026-08-02](v0.3.x-sequencing.md#amendment-2026-08-02--v0313--v0314-the-two-release-tail-to-v040) and [2026-08-19](v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train) amendments took their scope from the project's own gaps — deferred items, then a sweep of conversation topologies — not from anyone's request.
6. **v0.4.0 keeps moving.** In June it was due after v0.3.9; seven patch releases have been inserted ahead of it since.
7. **Output fell.** Merged pull requests dropped from 228 in June to 52 in August. In August and September, 28 of 94 were features or fixes; the rest were documentation, dependency and process work.

### Is this the right direction?

No — not because the recent work is wrong, but because of its order. Keeping one person's memory from another matters the day two real people share a persona agent. There are no such deployments yet, and the license requires a commercial agreement for most of the ones that would create them — any company team. Meanwhile the release built to attract users sits unused.

The project is in a loop: each release closes gaps the last one exposed, and closing them exposes more — the audience check exists because speaker attribution exists, which exists because per-person isolation exists ([ISSUE-0132](issues/ISSUE-0132-memory-egress-gate-blind-to-room-audience.md)). A system this rich always has another gap, so the loop does not end by itself. v0.4.0 as planned — roles, hierarchy, agents that "reason toward a decision" — would widen it: more surface, more gaps, still no user.

## The implementation strategy

### What to keep

- **The quality discipline.** Tests at 1.6 times the code, required CI, numbered review findings, and live runs on real providers. The execution reports show real bugs found and fixed before each tag.
- **Offline-first, provider-neutral design.** Cheap to demo, cheap to test, safe to try.
- **The open-core shape** in [RFC 0045](rfcs/0045-open-core-extraction-policy.md) — permissive building blocks, a self-hostable product, room for a hosted tier. The right shape; not yet used.

### What to change

1. **The process is sized for a large team and run by one person.** For every word written for users there are about 35 words of internal design, planning and test writing. The rules create their own work: 24 files sit exactly at their size cap, so v0.3.16 had to open with two mandatory file splits before its feature could start ([ISSUE-0143](issues/ISSUE-0143-debt-sweep-26-files-at-size-cap.md)); the ROADMAP's two header lines hold 665 words; and every red CI run on `main` among the last sixty failed the documentation job, not a code test.
2. **The economics are inverted.** The repository is built with AI coding assistants (it carries instructions for Claude Code and Copilot), which makes code and documents cheap. The scarce resources are the maintainer's attention and feedback from outside. A process that multiplies documents spends the scarce resource to save the cheap one.
3. **Four languages for one maintainer.** Go, Python, Rust and TypeScript. The quickstart needs Docker plus Go, Python and Rust, because no release ships an image or binary. The hop between the Go orchestrator and the Python agents keeps costing: much of v0.3.14 and v0.3.15 carried "who is speaking" across it ([ISSUE-0082](issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md), [ISSUE-0124](issues/ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md), [ISSUE-0130](issues/ISSUE-0130-catchup-replay-rederives-memory-under-default-principal.md)), and v0.3.16 carries "who is present".
4. **Built alone instead of joining the ecosystem.** Hand-written integrations are pinned to superseded dependency lines ([ISSUE-0144](issues/ISSUE-0144-anthropic-sdk-pinned-below-1x.md), [ISSUE-0145](issues/ISSUE-0145-proto-toolchain-pinned-to-protobuf-5x.md)). With no MCP client, persona agents cannot use existing MCP tool servers; with no A2A endpoint, an agent built elsewhere cannot join a channel. Each gap is a reason to try something else first.
5. **The license blocks the people most likely to try it.** Any commercial production use — a company running it internally, a startup building on it — needs a separate license, and even non-commercial deployments must serve fewer than 10 users. The planned permissive extractions ([RFC 0046](rfcs/0046-budget-lease-extraction.md), [RFC 0047](rfcs/0047-low-coupling-batch-extraction.md)) wait for v0.4.0 or later. With no users, permissive licensing is the cheapest adoption lever available.

## Does it have a real future?

Three honest futures, the most likely to reach users first:

1. **A focused tool** for multi-agent discussions that finish, stay within budget and can be replayed and audited — first for research teams studying how groups of agents behave (the license already allows research use), then for developers whose multi-agent chats pile on and never finish. What is unique here is exactly what these users need. **Recommended.**
2. **A source of building blocks.** The governance core and the budget lease, released permissively as small libraries that work inside the frameworks developers already use. Reaches people faster; harder to build a business on.
3. **A personal research project.** Legitimate, and already valuable as skill and portfolio — but then the process should shrink sharply.

The current path — a general-purpose engine in four languages, under a restrictive license, with a roadmap driven by its own gaps — does not lead to users.

## Recommendations

In order. Items 1–4 fill the next 8–10 weeks; the rest follow from what they show.

1. **Pause the v0.4.0 train; finish v0.3.16 small.** Render the audience verdict as planned, keep the two CI jobs if they fit in days, and cut the rest. Record the pause as a sequencing amendment; the same amendment would re-slot the items below that are parked for v0.4.0 or later.
2. **Test the core claim with one published experiment.** Pick one job — say, "critique this plan and recommend a decision". Run the same 20–30 prompts through four setups: one model call playing every role; persona agents without governance; with governance; with governance and memory across sessions. Score blind, with people and an LLM judge, and report quality per dollar. The eval harness, offline replay and cost ledger already exist. Decide beforehand: if governance does not clearly beat the single call, stop adding society features for that job.
3. **Name one first user and one job at the top of the README.** For example: "teams and researchers who need a multi-agent discussion that finishes, stays within budget, and can be replayed." Move the workflow runner out of the pitch.
4. **Make first contact take five minutes.** Publish images and CLI binaries with every release; a one-command offline demo that needs only Docker; one real transcript and synthesis committed as an example, so a visitor sees the result before installing anything. Then show it where the chosen users are, and talk to at least five of them.
5. **Join the ecosystem where it is cheapest.** An MCP client for tools; an A2A endpoint so an outside agent can join a channel ([RFC 0043](rfcs/0043-inbound-agent-interop-endpoint.md), pulled forward); consider one shared provider layer instead of five hand-written integrations.
6. **Release the unique pieces permissively now.** The governance core and the budget lease as an installable package that works with other frameworks — the RFC 0045 path, without waiting for v0.4.0. Keep BUSL for a hosted product if one is ever built; the no-retraction rule already protects that line.
7. **Put the process on a diet.** For a patch release: one short plan, no separate scope-lock file, no hand-flipped status rows. Make the 500-line cap a warning, not a gate. Keep the ROADMAP header to two short sentences. Keep CI, review findings and live runs — they catch real bugs.
8. **Review on a date, against outcomes.** By 2026-11-30: the experiment is published; at least 10 people outside the project have run the demo; at least five conversations with the chosen users are written up. If none of that has happened, choose openly between futures 1 and 3.

## What not to do

- Do not start organizations ([RFC 0012](rfcs/0012-protocols-organizations.md)) or the decision engine ([RFC 0028](rfcs/0028-agent-decision-policy-engine.md)) before the experiment reports.
- Do not add another language, store or kind of release document.
- Do not count progress in releases. Twenty-one releases and no users is the clearest signal in this review.

## What would change this assessment

- The experiment shows governed discussion clearly beats a single call on quality per dollar: the society machinery is then justified, and the result is the project's best argument.
- Users appear and ask for multi-person isolation or organizations: the recent direction was early, not wrong.
- The maintainer's goal is learning rather than adoption: future 3 is the right frame, and this review reduces to advice about process weight.

## Related documentation

- [Evidence for this review](strategy-independent-review-2026-09-evidence.md) — every number and the command behind it.
- [v0.3.x sequencing](v0.3.x-sequencing.md) — the decision log read above; [v0.3.16 plan](v0.3.16-plan.md) — the release in progress.
- [RFC 0045](rfcs/0045-open-core-extraction-policy.md) and [open-core reserved seams](open-core-reserved-seams.md) — the licensing structure.
- [Decisions](methodology/decisions.md) — how an amendment would ratify any of this.
