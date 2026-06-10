import { describe, it, expect, vi, afterEach } from "vitest";
import { createPresence } from "./presence.svelte.js";
import { GRACE_MS, SLOW_AFTER_MS, STALE_AFTER_MS } from "./presence.js";

// Controller-level specs for createPresence (lib/presence.svelte.js) — the
// timer/source-fusion behaviour that the pure mergeThinking spec
// (presence.test.js) and the wiring specs (ChannelTimeline.presence.test.js)
// sit either side of. These pin the semantics of the two state machines the
// controller layers over the merge:
//
//   • the idle "Waiting for you" flash means A TURN ACTUALLY HANDED BACK — a
//     reply emptied the set, or the server cleared a turn it had confirmed.
//     A wrong optimistic guess fading (the server never confirmed it) is
//     degradation and must stay silent, exactly like hardClear.
//   • the "taking a while…" softening describes the CURRENT turn — it restarts
//     whenever the displayed set gains an id (a new turn joining), and must NOT
//     restart on a mere reconfirmation of the same set (every 3s poll tick), or
//     a long turn would never soften.
//   • the optimistic grace bound is real: a never-confirmed guess fades at
//     graceMs even when nothing else (no /activity success, no fresh message)
//     triggers a recompute.

afterEach(() => {
  vi.useRealTimers();
});

describe("createPresence — idle flash semantics", () => {
  it("flashes 'Waiting for you' when the server clears a turn it had confirmed", () => {
    const p = createPresence();
    p.set(["ember-owl"]);
    expect(p.thinking).toEqual(["ember-owl"]);
    p.set([]);
    expect(p.thinking).toEqual([]);
    expect(p.idle).toBe(true);
  });

  it("does NOT flash when a never-confirmed optimistic guess fades", () => {
    // The console @-addressed a persona the orchestrator never dispatched
    // (e.g. a respond:never member). The server set stays empty throughout, so
    // when the grace lapses there is no turn handing back — flashing would
    // tell the operator a reply landed that never existed.
    vi.useFakeTimers();
    let clock = 0;
    const p = createPresence({ now: () => clock });
    p.add(["never-dispatched"], { graceMs: GRACE_MS });
    expect(p.thinking).toEqual(["never-dispatched"]);
    clock = GRACE_MS + 1000;
    p.set([]); // every /activity read came back empty
    expect(p.thinking).toEqual([]);
    expect(p.idle).toBe(false);
  });

  it("defers a hand-back flash masked by a still-in-grace wrong guess", () => {
    // The server confirmed agent-a's turn AND the console guessed at a persona
    // the orchestrator never dispatched. When a's reply empties the server set
    // the display stays lit by the guess — flashing right then would
    // contradict the visible "thinking…" line (the bar renders one state at a
    // time). But the hand-back is real: it must land when the masking guess
    // fades, not be swallowed because a wrong guess happened to overlap it.
    vi.useFakeTimers();
    const p = createPresence({ now: () => Date.now() });
    p.set(["agent-a"]);
    p.add(["never-dispatched"], { graceMs: GRACE_MS });
    p.set([]); // a replied; the wrong guess still masks the display
    expect(p.thinking).toEqual(["never-dispatched"]);
    expect(p.idle).toBe(false);
    vi.advanceTimersByTime(GRACE_MS);
    expect(p.thinking).toEqual([]);
    expect(p.idle).toBe(true); // the deferred hand-back flashes with the empty bar
  });

  it("drops a deferred hand-back once the operator fires a new turn", () => {
    // Publishing again IS the operator taking their turn — the deferred flash
    // is moot (the same reason add() cancels a live idle flash), and must not
    // resurface when the new round eventually fades unanswered.
    vi.useFakeTimers();
    const p = createPresence({ now: () => Date.now() });
    p.set(["agent-a"]);
    p.add(["never-dispatched"], { graceMs: GRACE_MS });
    p.set([]); // hand-back deferred behind the masking guess
    p.add(["also-never-dispatched"], { graceMs: GRACE_MS });
    vi.advanceTimersByTime(GRACE_MS);
    expect(p.thinking).toEqual([]);
    expect(p.idle).toBe(false); // both guesses faded silently
  });
});

describe("createPresence — optimistic grace bound", () => {
  it("fades a never-confirmed guess at graceMs without any server read", () => {
    // The /activity poll is self-guarded: a persistent failure means set() is
    // never called, so the fade must be timer-driven or GRACE_MS is only a
    // bound while the poll succeeds (the wrong guess would otherwise stay lit,
    // soften at SLOW_AFTER_MS, and clear only at the 120s stale backstop).
    vi.useFakeTimers();
    const p = createPresence({ now: () => Date.now() });
    p.add(["never-dispatched"], { graceMs: GRACE_MS });
    expect(p.thinking).toEqual(["never-dispatched"]);
    vi.advanceTimersByTime(GRACE_MS);
    expect(p.thinking).toEqual([]);
    expect(p.idle).toBe(false); // a fade is silent, not a hand-back
  });

  it("keeps a sticky (DM) add lit past graceMs, until the stale backstop", () => {
    vi.useFakeTimers();
    const p = createPresence({ now: () => Date.now() });
    p.add(["ada"]); // no graceMs — the DM send lifecycle owns clearing
    vi.advanceTimersByTime(STALE_AFTER_MS - 1000);
    expect(p.thinking).toEqual(["ada"]);
    vi.advanceTimersByTime(1000);
    expect(p.thinking).toEqual([]); // dead-poll/dead-reply backstop
    expect(p.idle).toBe(false);
  });
});

describe("createPresence — slow softening is per turn", () => {
  it("restarts the countdown when the server set turns over to a new agent", () => {
    // A floor round / cascade hands from one speaker to the next without the
    // set ever emptying. The new speaker's turn is brand new — it must read
    // "thinking…", not inherit the previous speaker's elapsed softening.
    vi.useFakeTimers();
    const p = createPresence({ now: () => Date.now() });
    p.set(["agent-a"]);
    vi.advanceTimersByTime(SLOW_AFTER_MS);
    expect(p.slow).toBe(true);
    p.set(["agent-b"]); // a replied; same tick already shows b's turn
    expect(p.slow).toBe(false);
    vi.advanceTimersByTime(SLOW_AFTER_MS);
    expect(p.slow).toBe(true); // and the countdown genuinely restarted
  });

  it("does not restart the countdown on reconfirmation of the same set", () => {
    // Every 3s poll tick re-installs the same server set; if that re-armed the
    // countdown, a long single turn would never soften.
    vi.useFakeTimers();
    const p = createPresence({ now: () => Date.now() });
    p.set(["agent-a"]);
    vi.advanceTimersByTime(SLOW_AFTER_MS / 2);
    p.set(["agent-a"]); // poll tick reconfirms
    vi.advanceTimersByTime(SLOW_AFTER_MS / 2);
    expect(p.slow).toBe(true);
  });

  it("resets the softening when an optimistic add joins a slow turn", () => {
    // Tier 0 add() did exactly this (slow = false + re-arm): the just-mentioned
    // persona starts a fresh turn, and the one-line phrase is per-set, so the
    // set reads "thinking…" again rather than branding the newcomer slow at
    // t=0 of its turn.
    vi.useFakeTimers();
    const p = createPresence({ now: () => Date.now() });
    p.add(["agent-a"], { graceMs: GRACE_MS });
    p.set(["agent-a"]);
    vi.advanceTimersByTime(SLOW_AFTER_MS);
    expect(p.slow).toBe(true);
    p.add(["agent-b"], { graceMs: GRACE_MS });
    expect(p.slow).toBe(false);
  });
});
