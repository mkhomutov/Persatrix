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
      agentsById: {},
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
});
