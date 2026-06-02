import { describe, it, expect } from "vitest";
import { KNOWN_PANELS, selectPanels, deriveUserId } from "./bootstrap.js";

// RFC 0048 §C feature-toggle contract + §F identity rule. These are the
// load-bearing decisions the SPA shell makes when it boots off
// /api/v1/ui/config and /api/v1/ui/context, isolated here as pure functions so
// the wiring is testable without a browser or a running orchestrator.

describe("selectPanels", () => {
  it("renders a panel only when it is both enabled and available", () => {
    const config = {
      panels: {
        chat: { enabled: true, available: true },
        channel_timeline: { enabled: true, available: true },
      },
    };
    expect(selectPanels(config).map((p) => p.name)).toEqual([
      "chat",
      "channel_timeline",
    ]);
  });

  it("excludes a panel the operator disabled (enabled:false)", () => {
    const config = {
      panels: {
        chat: { enabled: true, available: true },
        channel_timeline: { enabled: false, available: true },
      },
    };
    expect(selectPanels(config).map((p) => p.name)).toEqual(["chat"]);
  });

  it("excludes a panel whose subsystem is not wired (available:false)", () => {
    // The channel_timeline toggle is on, but the orchestrator was started
    // without channels — available is runtime-derived false, so the slot hides.
    const config = {
      panels: {
        chat: { enabled: true, available: true },
        channel_timeline: { enabled: true, available: false },
      },
    };
    expect(selectPanels(config).map((p) => p.name)).toEqual(["chat"]);
  });

  it("ignores panels the client does not recognise (forward-compat)", () => {
    // An older binary serving a newer bundle — or vice versa — must degrade
    // gracefully: the client renders only panels it knows how to draw.
    const config = {
      panels: {
        chat: { enabled: true, available: true },
        some_future_panel: { enabled: true, available: true },
      },
    };
    expect(selectPanels(config).map((p) => p.name)).toEqual(["chat"]);
  });

  it("renders panels in the client's known order, not the payload's", () => {
    const config = {
      panels: {
        channel_timeline: { enabled: true, available: true },
        chat: { enabled: true, available: true },
      },
    };
    expect(selectPanels(config).map((p) => p.name)).toEqual([
      "chat",
      "channel_timeline",
    ]);
  });

  it("ships the deferred panels (memory_strip, cost) dark even if enabled", () => {
    // Slice-1 ships these toggles off; even a deployment that flips memory_strip
    // on cannot make it available (no backing endpoint until Slice 2), so a
    // known-but-unavailable panel still does not render.
    const config = {
      panels: {
        chat: { enabled: true, available: true },
        memory_strip: { enabled: true, available: false },
        cost: { enabled: false, available: false },
      },
    };
    expect(selectPanels(config).map((p) => p.name)).toEqual(["chat"]);
  });

  it("returns an empty list for a missing or empty panels payload", () => {
    expect(selectPanels({})).toEqual([]);
    expect(selectPanels({ panels: {} })).toEqual([]);
    expect(selectPanels(null)).toEqual([]);
  });

  it("carries a human-facing title and hash route for each rendered panel", () => {
    const config = { panels: { chat: { enabled: true, available: true } } };
    const [panel] = selectPanels(config);
    expect(panel.title).toBeTruthy();
    expect(panel.route).toMatch(/^#\//);
  });
});

describe("KNOWN_PANELS", () => {
  it("covers exactly the four RFC 0048 §C panel names", () => {
    expect(KNOWN_PANELS.map((p) => p.name)).toEqual([
      "chat",
      "channel_timeline",
      "memory_strip",
      "cost",
    ]);
  });
});

describe("deriveUserId", () => {
  it("derives the user id from the context principal (the single identity source)", () => {
    expect(deriveUserId({ principal: "local", authenticated: false })).toBe(
      "local",
    );
  });

  it("returns null when no principal is present rather than inventing one", () => {
    // RFC §F rule 1: the console never hard-codes or prompts for a user. A
    // missing principal is an error state the shell surfaces, not a default.
    expect(deriveUserId({})).toBeNull();
    expect(deriveUserId(null)).toBeNull();
    expect(deriveUserId({ principal: "" })).toBeNull();
  });
});
