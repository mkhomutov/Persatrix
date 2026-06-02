import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/svelte";
import App from "./App.svelte";

// Mock the backend client so the shell's boot wiring is exercised without a
// running orchestrator. This is the PR-3 smoke: the shell fetches config +
// context, renders only the enabled && available panels, hides the rest, and
// derives identity from the context principal — never a hard-coded user.
vi.mock("./lib/api.js", () => ({
  ApiError: class ApiError extends Error {},
  loadBootstrap: vi.fn(),
}));

import { loadBootstrap } from "./lib/api.js";

beforeEach(() => {
  window.location.hash = "";
});

afterEach(() => {
  // Unmount between tests. Vitest is configured without `globals: true`, so
  // @testing-library/svelte cannot auto-register its afterEach(cleanup) — left
  // implicit, each render() leaks into jsdom and `getByRole`/`getByTitle`
  // resolve against a prior test's DOM, making the suite order-dependent.
  cleanup();
  vi.clearAllMocks();
});

describe("App shell boot", () => {
  it("renders a tab only for enabled && available panels, hiding the rest", async () => {
    loadBootstrap.mockResolvedValue({
      config: {
        panels: {
          chat: { enabled: true, available: true },
          channel_timeline: { enabled: true, available: true },
          memory_strip: { enabled: true, available: false },
          cost: { enabled: false, available: false },
        },
      },
      context: { principal: "local", tenant: "local", authenticated: false },
    });

    render(App);

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /chat/i })).toBeTruthy();
    });
    expect(screen.getByRole("tab", { name: /channels/i })).toBeTruthy();
    expect(screen.queryByRole("tab", { name: /memory/i })).toBeNull();
    expect(screen.queryByRole("tab", { name: /cost/i })).toBeNull();
  });

  it("surfaces the context principal and never shows a free-text user field", async () => {
    loadBootstrap.mockResolvedValue({
      config: { panels: { chat: { enabled: true, available: true } } },
      context: { principal: "local", tenant: "local", authenticated: false },
    });

    const { container } = render(App);

    // The topbar surfaces the principal coming from /ui/context (titled so the
    // source is unambiguous and to disambiguate it from panels that also echo
    // the derived user id).
    await waitFor(() => {
      const principal = screen.getByTitle("Identity from /api/v1/ui/context");
      expect(principal.textContent.trim()).toBe("local");
    });
    // RFC §F rule 1: identity comes from /ui/context, so the shell offers no
    // user-id input the operator could type into.
    expect(container.querySelector('input[name="user_id"]')).toBeNull();
  });

  it("shows a boot-error state when the backend is unreachable", async () => {
    loadBootstrap.mockRejectedValue(new Error("boom"));

    render(App);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeTruthy();
    });
  });

  it("shows a boot-error state (no tabs) when context carries no principal", async () => {
    // RFC §F rule 1: identity comes only from the context principal. A reachable
    // backend that returns an empty principal is still unusable — the shell must
    // surface the identity error rather than render panels with a null user.
    loadBootstrap.mockResolvedValue({
      config: { panels: { chat: { enabled: true, available: true } } },
      context: { principal: "", tenant: "local", authenticated: false },
    });

    render(App);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeTruthy();
    });
    expect(screen.getByRole("alert").textContent).toMatch(/identity/i);
    expect(screen.queryByRole("tab")).toBeNull();
  });

  it("drives the tablist with roving tabindex + arrow keys (ARIA APG tabs)", async () => {
    // The APG tabs pattern is more than roles: exactly one tab is in the Tab
    // sequence (roving tabindex), and Left/Right/Home/End move focus *and*
    // selection between tabs (automatic activation — cheap here, panels are
    // local). Without this, the role=tab markup advertises a keyboard contract
    // the shell doesn't honour.
    loadBootstrap.mockResolvedValue({
      config: {
        panels: {
          chat: { enabled: true, available: true },
          channel_timeline: { enabled: true, available: true },
        },
      },
      context: { principal: "local", tenant: "local", authenticated: false },
    });

    render(App);

    const chatTab = await screen.findByRole("tab", { name: /chat/i });
    const channelsTab = screen.getByRole("tab", { name: /channels/i });

    // Roving tabindex: only the active (chat) tab is Tab-reachable.
    expect(chatTab.getAttribute("tabindex")).toBe("0");
    expect(channelsTab.getAttribute("tabindex")).toBe("-1");

    // ArrowRight: focus + selection move to the next tab.
    await fireEvent.keyDown(chatTab, { key: "ArrowRight" });
    expect(channelsTab.getAttribute("aria-selected")).toBe("true");
    expect(chatTab.getAttribute("aria-selected")).toBe("false");
    expect(channelsTab.getAttribute("tabindex")).toBe("0");
    expect(chatTab.getAttribute("tabindex")).toBe("-1");
    expect(document.activeElement).toBe(channelsTab);

    // ArrowRight wraps from the last tab back to the first.
    await fireEvent.keyDown(channelsTab, { key: "ArrowRight" });
    expect(chatTab.getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(chatTab);

    // End jumps to the last tab, Home back to the first.
    await fireEvent.keyDown(chatTab, { key: "End" });
    expect(document.activeElement).toBe(channelsTab);
    await fireEvent.keyDown(channelsTab, { key: "Home" });
    expect(document.activeElement).toBe(chatTab);
  });

  it("exposes the active content region as a labelled tabpanel", async () => {
    // The tabs advertise role=tab/aria-selected; the content region they drive
    // must be a matching role=tabpanel labelled by the active tab so the
    // tab/panel relationship is complete for assistive tech.
    loadBootstrap.mockResolvedValue({
      config: { panels: { chat: { enabled: true, available: true } } },
      context: { principal: "local", tenant: "local", authenticated: false },
    });

    render(App);

    const tabpanel = await screen.findByRole("tabpanel");
    const tab = screen.getByRole("tab", { name: /chat/i });
    expect(tab.getAttribute("aria-controls")).toBe(tabpanel.id);
    expect(tabpanel.getAttribute("aria-labelledby")).toBe(tab.id);
  });

  it("does not leave a dangling aria-controls on inactive tabs", async () => {
    // Only the active panel is mounted (lazy single-panel render — PRs 4-5'
    // panels poll, so mounting all tabpanels hidden would start background work
    // for tabs the operator isn't looking at). An inactive tab therefore has no
    // panel in the DOM to control; advertising aria-controls to a missing id is
    // a dangling ARIA reference, so the attribute is present only on the active
    // tab (whose panel exists).
    loadBootstrap.mockResolvedValue({
      config: {
        panels: {
          chat: { enabled: true, available: true },
          channel_timeline: { enabled: true, available: true },
        },
      },
      context: { principal: "local", tenant: "local", authenticated: false },
    });

    render(App);

    const channelsTab = await screen.findByRole("tab", { name: /channels/i });
    const chatTab = screen.getByRole("tab", { name: /chat/i });
    const tabpanel = screen.getByRole("tabpanel");
    // chat is the default-active tab: its panel is mounted and controlled.
    expect(chatTab.getAttribute("aria-controls")).toBe(tabpanel.id);
    // channels is inactive: no panel mounted, so no aria-controls to dangle.
    expect(channelsTab.getAttribute("aria-controls")).toBeNull();
  });

  it("renders a plain empty state (no tab scaffolding) when no panels are enabled", async () => {
    // A reachable backend with a valid principal but zero enabled && available
    // panels is a legitimate deployment. The shell must not render an empty
    // role=tablist (invalid ARIA: a tablist with no tabs) or a tabpanel labelled
    // by a tab that doesn't exist — just the empty-state copy.
    loadBootstrap.mockResolvedValue({
      config: { panels: { chat: { enabled: false, available: true } } },
      context: { principal: "local", tenant: "local", authenticated: false },
    });

    render(App);

    await waitFor(() => {
      expect(screen.getByText(/no panels are enabled/i)).toBeTruthy();
    });
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.queryByRole("tab")).toBeNull();
    expect(screen.queryByRole("tabpanel")).toBeNull();
  });

  it("replaces history for keyboard tab navigation so Back doesn't step through tabs", async () => {
    // Automatic activation moves selection on every arrow keypress. If each
    // keystroke pushed a hash history entry, arrowing across the tabs would bury
    // the previous page under one entry per tab — Back would walk the tabs
    // instead of leaving the console. Keyboard nav replaces; clicks still push.
    loadBootstrap.mockResolvedValue({
      config: {
        panels: {
          chat: { enabled: true, available: true },
          channel_timeline: { enabled: true, available: true },
        },
      },
      context: { principal: "local", tenant: "local", authenticated: false },
    });

    render(App);

    const chatTab = await screen.findByRole("tab", { name: /chat/i });
    const replaceSpy = vi.spyOn(window.history, "replaceState");

    await fireEvent.keyDown(chatTab, { key: "ArrowRight" });

    expect(replaceSpy).toHaveBeenCalled();
    expect(window.location.hash).toBe("#/channels");
    replaceSpy.mockRestore();
  });

  it("pushes a history entry when a tab is clicked (deep-linkable navigation)", async () => {
    loadBootstrap.mockResolvedValue({
      config: {
        panels: {
          chat: { enabled: true, available: true },
          channel_timeline: { enabled: true, available: true },
        },
      },
      context: { principal: "local", tenant: "local", authenticated: false },
    });

    render(App);

    const channelsTab = await screen.findByRole("tab", { name: /channels/i });
    const replaceSpy = vi.spyOn(window.history, "replaceState");

    await fireEvent.click(channelsTab);

    expect(window.location.hash).toBe("#/channels");
    expect(replaceSpy).not.toHaveBeenCalled();
    replaceSpy.mockRestore();
  });
});
