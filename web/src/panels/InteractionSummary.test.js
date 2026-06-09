import { describe, it, expect, vi, afterEach } from "vitest";
import { render, cleanup, screen } from "@testing-library/svelte";
import InteractionSummary from "./InteractionSummary.svelte";

// InteractionSummary renders the v0.3.8 interaction-summary surface in the
// conversation view: when the read API reports a closed interaction for the
// active scope, it shows the synthesised RFC 0020 summary + the close trigger.
// It is additive — no closed interaction means no affordance, so an open
// conversation's live feed is untouched. The failure sentinel renders as an
// honest "unavailable" state, never a blank (SS3).
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function jsonResponse(body, ok = true, status = 200) {
  return { ok, status, json: () => Promise.resolve(body) };
}

function record(overrides = {}) {
  return {
    interaction_id: "int-1",
    scope: "group:planning",
    started_at: 1717500000,
    closed_at: 1717500600,
    turn_count: 6,
    close_reason: "structural",
    summary: "The group agreed to ship the cache layer first.",
    participants: ["ember-owl", "iron-fox"],
    ...overrides,
  };
}

// stubFetch returns the given envelope for every agent the surface queries.
function stubFetch(envelope) {
  const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(envelope)));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderSummary(props = {}) {
  return render(InteractionSummary, {
    props: {
      scope: "group:planning",
      agentIds: ["ember-owl"],
      ...props,
    },
  });
}

describe("InteractionSummary", () => {
  it("renders the summary and the close trigger when an interaction has closed", async () => {
    stubFetch({ interactions: [record()] });

    renderSummary();

    const region = await screen.findByRole("status", {
      name: /interaction summary/i,
    });
    expect(region.textContent).toContain(
      "The group agreed to ship the cache layer first.",
    );
    // The close trigger is surfaced (structural close -> "ended"/"concluded").
    expect(region.textContent).toMatch(/ended|concluded/i);
  });

  it("renders the failure sentinel as an explicit unavailable state, not a blank", async () => {
    stubFetch({
      interactions: [record({ summary: "[interaction summary unavailable]" })],
    });

    renderSummary();

    const region = await screen.findByRole("status", {
      name: /interaction summary/i,
    });
    expect(region.textContent).toMatch(/unavailable/i);
    // The raw sentinel string is not shown as if it were the summary body.
    expect(region.textContent).not.toContain(
      "[interaction summary unavailable]",
    );
  });

  it("shows no affordance when there is no closed interaction (open conversation)", async () => {
    stubFetch({ interactions: [] });

    const { container } = renderSummary();

    // Give the in-flight fetch a chance to resolve before asserting absence.
    await Promise.resolve();
    await Promise.resolve();
    expect(container.querySelector(".interaction-summary")).toBeNull();
  });

  it("does not query the read API when there is no scope or no candidate agents", async () => {
    const fetchMock = stubFetch({ interactions: [record()] });

    renderSummary({ agentIds: [] });
    await Promise.resolve();
    expect(fetchMock).not.toHaveBeenCalled();

    renderSummary({ scope: "" });
    await Promise.resolve();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows the newest interaction across the channel's participating agents", async () => {
    // Two agents, each its own closed-interaction row; the newer close wins.
    const fetchMock = vi.fn((url) =>
      Promise.resolve(
        jsonResponse(
          url.includes("iron-fox")
            ? {
                interactions: [
                  record({
                    interaction_id: "int-late",
                    closed_at: 1717599999,
                    summary: "Final consensus reached.",
                  }),
                ],
              }
            : {
                interactions: [
                  record({
                    interaction_id: "int-early",
                    closed_at: 1717500600,
                    summary: "An earlier, stale summary.",
                  }),
                ],
              },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    renderSummary({ agentIds: ["ember-owl", "iron-fox"] });

    const region = await screen.findByRole("status", {
      name: /interaction summary/i,
    });
    expect(region.textContent).toContain("Final consensus reached.");
    expect(region.textContent).not.toContain("An earlier, stale summary.");
  });

  it("clears a prior summary when the conversation switches, even if the new channel's reads fail", async () => {
    // Switching channels must never leave the previous conversation's summary on
    // screen. Channel A has a closed interaction; channel B's reads all fail —
    // the affordance must vanish, not "hold" A's summary in B's view.
    const fetchMock = vi.fn((url) =>
      url.includes("scope=group%3Aplanning")
        ? Promise.resolve(jsonResponse({ interactions: [record()] }))
        : Promise.reject(new Error("agent down")),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { rerender } = renderSummary();
    const region = await screen.findByRole("status", {
      name: /interaction summary/i,
    });
    expect(region.textContent).toContain(
      "The group agreed to ship the cache layer first.",
    );

    await rerender({
      scope: "group:other",
      agentIds: ["iron-fox"],
    });

    await vi.waitFor(() => {
      expect(
        screen.queryByRole("status", { name: /interaction summary/i }),
      ).toBeNull();
    });
  });

  it("does not flash the previous summary while the new channel's fetch is pending", async () => {
    // The clear must happen up front on a switch, not only once the new fetch
    // resolves — otherwise A's summary flashes under B's feed for the duration
    // of the request. B's fetch is left pending for the whole test.
    const fetchMock = vi.fn((url) =>
      url.includes("scope=group%3Aplanning")
        ? Promise.resolve(jsonResponse({ interactions: [record()] }))
        : new Promise(() => {}),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { rerender } = renderSummary();
    await screen.findByRole("status", { name: /interaction summary/i });

    await rerender({
      scope: "group:other",
      agentIds: ["iron-fox"],
    });

    await vi.waitFor(() => {
      expect(
        screen.queryByRole("status", { name: /interaction summary/i }),
      ).toBeNull();
    });
  });

  it("holds the summary on a poll when the holding agent fails but a peer responds empty", async () => {
    // Multi-agent: iron-fox holds the latest summary, ember-owl has none. A poll
    // where iron-fox transiently fails (but ember-owl still answers "none") must
    // NOT flap the affordance away — a partial failure of the holder is not
    // evidence the interaction is gone.
    vi.useFakeTimers();
    let holderFails = false;
    const fetchMock = vi.fn((url) => {
      if (url.includes("iron-fox")) {
        return holderFails
          ? Promise.reject(new Error("holder down"))
          : Promise.resolve(
              jsonResponse({ interactions: [record({ summary: "Held summary." })] }),
            );
      }
      return Promise.resolve(jsonResponse({ interactions: [] }));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSummary({ agentIds: ["ember-owl", "iron-fox"] });
    await vi.waitFor(() => {
      expect(
        screen.getByRole("status", { name: /interaction summary/i }).textContent,
      ).toContain("Held summary.");
    });

    holderFails = true;
    await vi.advanceTimersByTimeAsync(5000);

    expect(
      screen.getByRole("status", { name: /interaction summary/i }).textContent,
    ).toContain("Held summary.");
  });

  it("drops the summary on a poll when every agent successfully reports none", async () => {
    // The counterpart to the hold: a complete poll (no failures) that returns no
    // closed interaction is authoritative — the affordance is cleared, not held.
    vi.useFakeTimers();
    let closed = true;
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        jsonResponse({ interactions: closed ? [record()] : [] }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    renderSummary();
    await vi.waitFor(() => {
      expect(
        screen.queryByRole("status", { name: /interaction summary/i }),
      ).not.toBeNull();
    });

    closed = false;
    await vi.advanceTimersByTimeAsync(5000);

    expect(
      screen.queryByRole("status", { name: /interaction summary/i }),
    ).toBeNull();
  });
});
