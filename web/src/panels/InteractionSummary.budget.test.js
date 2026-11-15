import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, cleanup, screen } from "@testing-library/svelte";
import InteractionSummary from "./InteractionSummary.svelte";

// What the interaction summary costs (ISSUE-0224). The read is per persona, and
// every read counts toward the console's request budget: 60 calls per 60
// seconds by default, one bucket for every console window while sign-in is
// off. The summary changes only when an interaction closes, so each persona is
// asked once every ten seconds per persona asked: six requests a minute on
// average, whatever the size of the channel. It is an average: a round's
// reads go out together, ten at once every 100 seconds for ten personas.
// These cases pin that count, so a change that adds a read to a refresh, or
// shortens the wait, fails here.
vi.mock("../lib/api.js", () => ({
  getClosedInteractions: vi.fn(),
}));

import { getClosedInteractions } from "../lib/api.js";

const MINUTE = 60000;

function record(overrides = {}) {
  return {
    interaction_id: "int-1",
    scope: "group:planning",
    started_at: 1717500000,
    closed_at: 1717500600,
    turn_count: 6,
    close_reason: "structural",
    summary: "The group agreed to ship the cache layer first.",
    participants: ["ember-owl"],
    ...overrides,
  };
}

const personas = (n) => Array.from({ length: n }, (_, i) => `persona-${i + 1}`);

// Mount the summary for a channel with `n` personas and let the read made on
// opening settle; returns after clearing the call count.
async function watch(n, props = {}) {
  const view = render(InteractionSummary, {
    props: { scope: "group:planning", agentIds: personas(n), ...props },
  });
  await vi.advanceTimersByTimeAsync(0);
  expect(getClosedInteractions).toHaveBeenCalledTimes(n); // one per persona, at once
  getClosedInteractions.mockClear();
  return view;
}

let hidden;

beforeEach(() => {
  vi.useFakeTimers();
  hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  getClosedInteractions.mockReset();
  getClosedInteractions.mockImplementation(() => Promise.resolve({ interactions: [] }));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("InteractionSummary — requests per minute", () => {
  it("asks three personas six times in a minute, in two rounds", async () => {
    // The shipped `planning` and `roundtable` channels have three personas.
    await watch(3);

    await vi.advanceTimersByTimeAsync(29999);
    expect(getClosedInteractions).toHaveBeenCalledTimes(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(getClosedInteractions).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(30000);

    expect(getClosedInteractions).toHaveBeenCalledTimes(6);
    // Each round asks every persona once, for this channel's newest record.
    expect(getClosedInteractions.mock.calls.map(([id]) => id)).toEqual([
      ...personas(3),
      ...personas(3),
    ]);
    expect(getClosedInteractions).toHaveBeenLastCalledWith("persona-3", {
      scope: "group:planning",
      limit: 1,
      minTurns: 2,
    });
  });

  it.each([1, 2, 3, 10])(
    "makes thirty requests in five minutes for a channel of %i",
    async (n) => {
      await watch(n);

      await vi.advanceTimersByTimeAsync(5 * MINUTE);

      expect(getClosedInteractions).toHaveBeenCalledTimes(30);
    },
  );

  it("asks a DM's one persona every ten seconds", async () => {
    await watch(1, { scope: "dm:ada:local" });

    await vi.advanceTimersByTimeAsync(9999);
    expect(getClosedInteractions).toHaveBeenCalledTimes(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(getClosedInteractions).toHaveBeenCalledTimes(1);
  });

  it("shows an interaction that closed within one round of its summary being stored", async () => {
    await watch(3);
    expect(screen.queryByRole("status", { name: /interaction summary/i })).toBeNull();

    // A persona stores its summary just after a round: it shows at the next.
    await vi.advanceTimersByTimeAsync(30000);
    getClosedInteractions.mockImplementation((id) =>
      Promise.resolve({ interactions: id === "persona-2" ? [record()] : [] }),
    );
    await vi.advanceTimersByTimeAsync(29999);
    expect(screen.queryByRole("status", { name: /interaction summary/i })).toBeNull();
    await vi.advanceTimersByTimeAsync(1);

    expect(
      screen.getByRole("status", { name: /interaction summary/i }).textContent,
    ).toContain("The group agreed to ship the cache layer first.");
  });

  it("asks nobody, and wakes six times a minute, for a conversation without personas", async () => {
    render(InteractionSummary, { props: { scope: "group:planning", agentIds: [] } });
    await vi.advanceTimersByTimeAsync(0);
    const armed = vi.spyOn(globalThis, "setTimeout");

    // A wait of zero between rounds would spin here; a minute passes quietly.
    await vi.advanceTimersByTimeAsync(MINUTE);

    expect(getClosedInteractions).not.toHaveBeenCalled();
    expect(armed).toHaveBeenCalledTimes(6);
    expect(vi.getTimerCount()).toBe(1);
  });

  it("follows the channel's size when its members change", async () => {
    const view = await watch(3);

    await view.rerender({ agentIds: personas(1) });
    await vi.advanceTimersByTimeAsync(0);
    expect(getClosedInteractions).toHaveBeenCalledTimes(1); // the read on the change
    getClosedInteractions.mockClear();

    // The rounds are timed afresh from the change: the one persona is asked
    // every ten seconds, and the round timed for three does not come.
    await vi.advanceTimersByTimeAsync(9999);
    expect(getClosedInteractions).toHaveBeenCalledTimes(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(getClosedInteractions).toHaveBeenCalledTimes(1);
    getClosedInteractions.mockClear();
    await vi.advanceTimersByTimeAsync(MINUTE);
    expect(getClosedInteractions).toHaveBeenCalledTimes(6);
  });
});

describe("InteractionSummary — another conversation is opened", () => {
  // The wait belongs to the conversation it was timed for. Opening another
  // one reads it at once and times its rounds from that moment.
  const refused = () =>
    Promise.reject(Object.assign(new Error("rate limit exceeded"), { status: 429 }));

  it("times a smaller channel's first round from the switch, not from the larger channel left", async () => {
    const view = await watch(10); // a round every 100 seconds
    await vi.advanceTimersByTimeAsync(1000);

    await view.rerender({ scope: "group:roundtable", agentIds: personas(3) });
    await vi.advanceTimersByTimeAsync(0);
    expect(getClosedInteractions).toHaveBeenCalledTimes(3); // read on opening
    getClosedInteractions.mockClear();

    await vi.advanceTimersByTimeAsync(29999);
    expect(getClosedInteractions).toHaveBeenCalledTimes(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(getClosedInteractions).toHaveBeenCalledTimes(3);
  });

  it("does not ask a large channel a second time within its round after a DM", async () => {
    const view = await watch(1, { scope: "dm:ada:local" }); // a round every 10 seconds
    await vi.advanceTimersByTimeAsync(1000);

    await view.rerender({ scope: "group:assembly", agentIds: personas(10) });
    await vi.advanceTimersByTimeAsync(0);
    expect(getClosedInteractions).toHaveBeenCalledTimes(10); // read on opening
    getClosedInteractions.mockClear();

    // The DM's round, nine seconds away when it was left, does not come.
    await vi.advanceTimersByTimeAsync(99999);
    expect(getClosedInteractions).toHaveBeenCalledTimes(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(getClosedInteractions).toHaveBeenCalledTimes(10);
  });

  it("keeps to one round at a time when a round is still out as another conversation is opened", async () => {
    const view = await watch(3);
    // A round goes out and is not answered yet.
    const held = [];
    getClosedInteractions.mockImplementation(() => new Promise((resolve) => held.push(resolve)));
    await vi.advanceTimersByTimeAsync(30000);
    expect(held).toHaveLength(3);

    getClosedInteractions.mockImplementation(() => Promise.resolve({ interactions: [] }));
    await view.rerender({ scope: "group:roundtable", agentIds: personas(3) });
    await vi.advanceTimersByTimeAsync(0);
    held.forEach((answer) => answer({ interactions: [] })); // the old round answers now
    await vi.advanceTimersByTimeAsync(0);
    getClosedInteractions.mockClear();

    // The conversation left does not go on asking beside the new one.
    await vi.advanceTimersByTimeAsync(MINUTE);
    expect(getClosedInteractions).toHaveBeenCalledTimes(6);
  });

  it("reads a conversation again one round after a refused read on opening, whatever wait the one left had reached", async () => {
    const view = await watch(3);
    getClosedInteractions.mockImplementation(refused);
    // Four refused rounds: the next one is five minutes away.
    await vi.advanceTimersByTimeAsync(30000 + 60000 + 120000 + 240000);
    getClosedInteractions.mockClear();

    await view.rerender({ scope: "group:roundtable", agentIds: personas(3) });
    await vi.advanceTimersByTimeAsync(0);
    expect(getClosedInteractions).toHaveBeenCalledTimes(3); // read on opening, refused
    getClosedInteractions.mockImplementation(() => Promise.resolve({ interactions: [record()] }));
    getClosedInteractions.mockClear();

    await vi.advanceTimersByTimeAsync(29999);
    expect(getClosedInteractions).toHaveBeenCalledTimes(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(getClosedInteractions).toHaveBeenCalledTimes(3);
    expect(screen.getByRole("status", { name: /interaction summary/i })).toBeTruthy();
  });
});

describe("InteractionSummary — a hidden tab", () => {
  it("reads nothing while hidden, and reads once within five seconds of being shown", async () => {
    await watch(3);

    hidden.mockReturnValue(true);
    await vi.advanceTimersByTimeAsync(10 * MINUTE);
    expect(getClosedInteractions).toHaveBeenCalledTimes(0);

    hidden.mockReturnValue(false);
    await vi.advanceTimersByTimeAsync(4999);
    await vi.advanceTimersByTimeAsync(1);
    expect(getClosedInteractions).toHaveBeenCalledTimes(3);

    // And then at the ordinary pace again, not every five seconds.
    getClosedInteractions.mockClear();
    await vi.advanceTimersByTimeAsync(29999);
    expect(getClosedInteractions).toHaveBeenCalledTimes(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(getClosedInteractions).toHaveBeenCalledTimes(3);
  });

  it("does not read more often when the tab is hidden and shown again and again", async () => {
    await watch(3);

    // Away and back every four seconds for a minute, with the event the
    // browser fires each time: a read on that event would show here.
    for (let t = 0; t < MINUTE; t += 4000) {
      hidden.mockReturnValue((t / 4000) % 2 === 0);
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(4000);
    }

    expect(getClosedInteractions.mock.calls.length).toBeLessThanOrEqual(6);
  });
});

describe("InteractionSummary — a refused read", () => {
  const refused = () =>
    Promise.reject(Object.assign(new Error("rate limit exceeded"), { status: 429 }));

  it("doubles its wait after each refused round, up to five minutes, and returns to its pace after a served one", async () => {
    await watch(1);
    getClosedInteractions.mockImplementation(refused);
    const rounds = () => getClosedInteractions.mock.calls.length;

    // Rounds at 10 s, then 20, 40, 80, 160 and 300 s after the one before.
    const waits = [10, 20, 40, 80, 160, 300, 300];
    for (let i = 0; i < waits.length; i++) {
      await vi.advanceTimersByTimeAsync(waits[i] * 1000 - 1);
      expect(rounds()).toBe(i);
      await vi.advanceTimersByTimeAsync(1);
      expect(rounds()).toBe(i + 1);
    }

    getClosedInteractions.mockImplementation(() => Promise.resolve({ interactions: [] }));
    await vi.advanceTimersByTimeAsync(300000); // the round already timed, served
    getClosedInteractions.mockClear();
    await vi.advanceTimersByTimeAsync(MINUTE);
    expect(getClosedInteractions).toHaveBeenCalledTimes(6);
  });

  it("stretches its wait when one read of three is refused", async () => {
    // The usual shape of a full budget: the round's reads go out together
    // and the limiter serves some of them.
    await watch(3);
    getClosedInteractions.mockImplementation((id) =>
      id === "persona-2" ? refused() : Promise.resolve({ interactions: [] }),
    );
    await vi.advanceTimersByTimeAsync(30000); // the round with one refusal
    getClosedInteractions.mockClear();

    await vi.advanceTimersByTimeAsync(59999);
    expect(getClosedInteractions).toHaveBeenCalledTimes(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(getClosedInteractions).toHaveBeenCalledTimes(3);
  });

  it("keeps its pace after a read that failed for another reason", async () => {
    // One flaky persona is no sign of a full budget: the others still answer,
    // and the round comes at its ordinary time.
    await watch(3);
    getClosedInteractions.mockImplementation((id) =>
      id === "persona-2"
        ? Promise.reject(Object.assign(new Error("agent not found"), { status: 404 }))
        : Promise.resolve({ interactions: [] }),
    );

    await vi.advanceTimersByTimeAsync(MINUTE);

    expect(getClosedInteractions).toHaveBeenCalledTimes(6);
  });

  it("does not wait less than a larger channel's pace when the channel grew while refused", async () => {
    // Watching one persona, the wait is ten seconds, and one refused round
    // stretches it to twenty. The channel then has three, and every read is
    // still refused: the waits double from thirty seconds, not from twenty.
    const view = await watch(1);
    getClosedInteractions.mockImplementation(refused);
    await vi.advanceTimersByTimeAsync(10000); // a refused round: 20 s
    await view.rerender({ agentIds: personas(3) });
    await vi.advanceTimersByTimeAsync(0); // the read on the change, refused: 60 s
    getClosedInteractions.mockClear();

    // The first round comes one ordinary wait after the change, refused
    // again: 120 s.
    await vi.advanceTimersByTimeAsync(29999);
    expect(getClosedInteractions).toHaveBeenCalledTimes(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(getClosedInteractions).toHaveBeenCalledTimes(3);
    getClosedInteractions.mockClear();

    await vi.advanceTimersByTimeAsync(119999);
    expect(getClosedInteractions).toHaveBeenCalledTimes(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(getClosedInteractions).toHaveBeenCalledTimes(3);
  });

  it("drops a stretched wait when the personas leave, and is back at its pace when they return", async () => {
    const view = await watch(1);
    getClosedInteractions.mockImplementation(refused);
    await vi.advanceTimersByTimeAsync(10000 + 20000 + 40000); // three refused rounds: 80 s next
    await view.rerender({ agentIds: [] });
    await vi.advanceTimersByTimeAsync(0);
    // Nobody to ask: it looks again at the ordinary wait, not the stretched one.
    const armed = vi.spyOn(globalThis, "setTimeout");
    await vi.advanceTimersByTimeAsync(MINUTE);
    expect(armed).toHaveBeenCalledTimes(6);
    expect(getClosedInteractions).toHaveBeenCalledTimes(3);

    getClosedInteractions.mockImplementation(() => Promise.resolve({ interactions: [] }));
    await view.rerender({ agentIds: personas(1) });
    await vi.advanceTimersByTimeAsync(0); // the read on the change
    getClosedInteractions.mockClear();

    await vi.advanceTimersByTimeAsync(10000);
    expect(getClosedInteractions).toHaveBeenCalledTimes(1);
  });

  it("never waits less than the channel's own pace after a refusal, however large the channel", async () => {
    // Forty personas are asked every 400 seconds, which is already more than
    // the five-minute cap on a stretched wait.
    await watch(40);
    getClosedInteractions.mockImplementation(refused);

    await vi.advanceTimersByTimeAsync(400000); // the refused round
    getClosedInteractions.mockClear();
    await vi.advanceTimersByTimeAsync(399999);
    expect(getClosedInteractions).toHaveBeenCalledTimes(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(getClosedInteractions).toHaveBeenCalledTimes(40);
  });

  it("starts the doubling from the channel's own pace", async () => {
    await watch(3);
    getClosedInteractions.mockImplementation(refused);

    await vi.advanceTimersByTimeAsync(30000); // the refused round
    getClosedInteractions.mockClear();
    await vi.advanceTimersByTimeAsync(59999);
    expect(getClosedInteractions).toHaveBeenCalledTimes(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(getClosedInteractions).toHaveBeenCalledTimes(3);
  });
});
