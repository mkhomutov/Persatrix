import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/svelte";
import App from "./App.svelte";

// The §E tester identity-override carve-out (RFC 0048 amendment): in LOCAL
// (unauthenticated) mode the shell offers an "acting as" control — distinct from
// the /ui/context principal — so a tester can switch the effective user and
// watch per-user persistence follow the identity. Split out of App.test.js to
// keep each spec under the review-size cap; the boot/routing smoke stays there.
// The shell mounts the real channel_timeline panel, whose mount effects call the
// client, so the full api surface is stubbed (mirrors App.test.js).
vi.mock("./lib/api.js", () => ({
  ApiError: class ApiError extends Error {},
  loadBootstrap: vi.fn(),
  listAgents: vi.fn(() => Promise.resolve([])),
  sendChat: vi.fn(),
  getChatHistory: vi.fn(() => Promise.resolve({ messages: [] })),
  listSessions: vi.fn(() => Promise.resolve({ sessions: [] })),
  createSession: vi.fn(),
  listChannels: vi.fn(() => Promise.resolve({ channels: [] })),
  getChannelHistory: vi.fn(() => Promise.resolve({ messages: [] })),
  publishMessage: vi.fn(),
}));

import { loadBootstrap } from "./lib/api.js";

beforeEach(() => {
  window.location.hash = "";
});

afterEach(() => {
  // Unmount between tests. Vitest runs without `globals: true`, so
  // @testing-library/svelte cannot auto-register its afterEach(cleanup) — left
  // implicit, each render() leaks into jsdom and queries resolve against a
  // prior test's DOM, making the suite order-dependent.
  cleanup();
  vi.clearAllMocks();
});

describe("App shell — §E tester identity override", () => {
  it("surfaces the real principal verbatim and offers the §E testing override in local mode", async () => {
    loadBootstrap.mockResolvedValue({
      config: { panels: { channel_timeline: { enabled: true, available: true } } },
      context: { principal: "local", tenant: "local", authenticated: false },
    });

    const { container } = render(App);

    // The topbar surfaces the principal coming from /ui/context (titled so the
    // source is unambiguous), shown verbatim and never replaced by the override.
    await waitFor(() => {
      const principal = screen.getByTitle("Identity from /api/v1/ui/context");
      expect(principal.textContent.trim()).toBe("local");
    });
    // RFC §F rule 1: identity comes from /ui/context, so there is still NO
    // user_id field — the panels never prompt for the identity itself.
    expect(container.querySelector('input[name="user_id"]')).toBeNull();
    // §E carve-out: in LOCAL (unauthenticated) mode the shell offers a clearly
    // distinct "acting as" testing override, defaulting to the principal, so a
    // tester can demonstrate per-user persistence. It is a separate control, not
    // the identity source.
    const override = container.querySelector('input[name="acting_as"]');
    expect(override).not.toBeNull();
    expect(override.value).toBe("local");
    // The placeholder echoes the principal so that, once a tester clears the
    // box, the field visibly communicates "empty ⇒ acting as the principal"
    // rather than reading as no identity at all.
    expect(override.getAttribute("placeholder")).toBe("local");
  });

  it("hides the §E identity override once the principal is authenticated", async () => {
    // The carve-out is local-only: once /ui/context reports an authenticated
    // principal (RFC 0039), the override disappears so a real identity can never
    // be masked from the browser.
    loadBootstrap.mockResolvedValue({
      config: { panels: { channel_timeline: { enabled: true, available: true } } },
      context: { principal: "alice@example.com", tenant: "t", authenticated: true },
    });

    const { container } = render(App);

    await waitFor(() => {
      expect(
        screen.getByTitle("Identity from /api/v1/ui/context").textContent.trim(),
      ).toBe("alice@example.com");
    });
    expect(container.querySelector('input[name="acting_as"]')).toBeNull();
  });

  it("commits the override on change (not per-keystroke) so the effective identity follows it", async () => {
    // Editing "acting as" changes the effective user threaded to the panels —
    // the mechanism that lets a tester switch users and watch the persona's
    // memory follow the identity (§E). The commit is deferred to `change`
    // (blur/Enter), not every keystroke: persistence is keyed on (user, agent)
    // and the panels reseed history on an identity change, so committing
    // per-keystroke would blank+refetch the transcript for each intermediate
    // value ("b", "bo", "bob"). The contract here is the observable side of
    // that: typing alone must NOT move the effective identity; the change does.
    loadBootstrap.mockResolvedValue({
      config: { panels: { channel_timeline: { enabled: true, available: true } } },
      context: { principal: "local", tenant: "local", authenticated: false },
    });

    const { container } = render(App);

    const override = await waitFor(() => {
      const el = container.querySelector('input[name="acting_as"]');
      expect(el).not.toBeNull();
      return el;
    });

    const effectiveIds = () =>
      [...container.querySelectorAll("code")].map((c) => c.textContent.trim());

    // The conversation panel echoes the effective identity it acts as; it starts
    // at the principal.
    await waitFor(() => expect(effectiveIds()).toContain("local"));

    // Typing into the box edits the draft but must not yet move the effective
    // identity — no premature reseed.
    await fireEvent.input(override, { target: { value: "bob" } });
    expect(effectiveIds()).not.toContain("bob");
    expect(effectiveIds()).toContain("local");

    // Committing (blur/Enter ⇒ `change`) threads the new identity to the panels.
    await fireEvent.change(override, { target: { value: "bob" } });
    await waitFor(() => expect(effectiveIds()).toContain("bob"));

    // Clearing the override and committing falls back to the real principal —
    // the empty box means "act as the principal", never "no identity".
    await fireEvent.input(override, { target: { value: "" } });
    await fireEvent.change(override, { target: { value: "" } });
    await waitFor(() => {
      expect(effectiveIds()).toContain("local");
      expect(effectiveIds()).not.toContain("bob");
    });
  });
});
