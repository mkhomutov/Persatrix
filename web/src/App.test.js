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
// The shell mounts the real Chat / ChannelTimeline panels for the enabled tabs,
// whose mount effects call listAgents() / listChannels(); stub the panels' API
// surface so the boot wiring under test doesn't reach a real backend or throw on
// an undefined export. The list calls resolve empty — these tests assert the
// shell's tab/identity wiring, not the panels' contents. (A deep-link / hashchange
// to #/channels mounts ChannelTimeline, so its calls must be stubbed too.)
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

  it("surfaces the orchestrator build version from config.build.version", async () => {
    // The topbar shows which orchestrator build the operator is driving (RFC 0048
    // amendment §D), read from /ui/config's build.version. Titled so the chip's
    // source is unambiguous.
    loadBootstrap.mockResolvedValue({
      config: {
        build: { version: "0.3.6" },
        panels: { chat: { enabled: true, available: true } },
      },
      context: { principal: "local", tenant: "local", authenticated: false },
    });

    render(App);

    await waitFor(() => {
      const chip = screen.getByTitle("Orchestrator build");
      expect(chip.textContent.trim()).toBe("v0.3.6");
    });
  });

  it("omits the version chip when the config carries no build version", async () => {
    // build.version is optional; a payload without it shows no chip rather than a
    // bare "v" placeholder.
    loadBootstrap.mockResolvedValue({
      config: { panels: { chat: { enabled: true, available: true } } },
      context: { principal: "local", tenant: "local", authenticated: false },
    });

    render(App);

    await screen.findByRole("tab", { name: /chat/i });
    expect(screen.queryByTitle("Orchestrator build")).toBeNull();
  });

  it("shows a boot-error state when the backend is unreachable", async () => {
    loadBootstrap.mockRejectedValue(new Error("boom"));

    render(App);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeTruthy();
    });
  });

  it("keeps the boot-error inside the main landmark (no orphaned content)", async () => {
    // The empty-state branch already wraps its copy in <main>; the error state
    // must too, so no shell content sits outside a landmark region (the
    // orphaned-content a11y gap). The error stays a role=alert so it is still
    // announced — wrapping it in <main> doesn't change that.
    loadBootstrap.mockRejectedValue(new Error("boom"));

    render(App);

    const alert = await screen.findByRole("alert");
    expect(alert.closest("main")).not.toBeNull();
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

  it("selects the deep-linked panel on initial load (hash routing)", async () => {
    // A reload / shared link lands directly on a panel via the hash. The first
    // rendered tab must be the one the hash names, not the default-first tab —
    // this is the whole point of the D1 hash-mode routing decision.
    window.location.hash = "#/channels";
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
    expect(channelsTab.getAttribute("aria-selected")).toBe("true");
    const tabpanel = screen.getByRole("tabpanel");
    expect(tabpanel.getAttribute("aria-labelledby")).toBe(channelsTab.id);
  });

  it("rewrites a stale/unavailable deep-link hash to the resolved fallback route", async () => {
    // Deep-linking to a panel that isn't available in this deployment (#/memory
    // — known name, but available:false) falls back to the first rendered panel.
    // The URL must be canonicalised to the panel actually shown (via replace, not
    // push) so the address bar doesn't dangle a route that resolves to a
    // different tab.
    window.location.hash = "#/memory";
    const replaceSpy = vi.spyOn(window.history, "replaceState");
    loadBootstrap.mockResolvedValue({
      config: {
        panels: {
          chat: { enabled: true, available: true },
          memory_strip: { enabled: true, available: false },
        },
      },
      context: { principal: "local", tenant: "local", authenticated: false },
    });

    render(App);

    await screen.findByRole("tab", { name: /chat/i });
    await waitFor(() => expect(window.location.hash).toBe("#/chat"));
    expect(replaceSpy).toHaveBeenCalledWith(null, "", "#/chat");
    replaceSpy.mockRestore();
  });

  it("does not append a hash on a clean load with no deep link", async () => {
    // The canonicalisation above must fire only for a non-empty stale hash. A
    // bare /ui/ visit (no hash) is left untouched — we don't want to push
    // #/chat onto every clean load.
    loadBootstrap.mockResolvedValue({
      config: { panels: { chat: { enabled: true, available: true } } },
      context: { principal: "local", tenant: "local", authenticated: false },
    });

    render(App);

    await screen.findByRole("tab", { name: /chat/i });
    expect(window.location.hash).toBe("");
  });

  it("switches the active tab when the location hash changes (back/forward nav)", async () => {
    // The shell registers a hashchange listener so browser Back/Forward (and any
    // external hash navigation) moves the active tab. Without it, the URL and the
    // rendered panel would drift apart after a history navigation.
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
    expect(chatTab.getAttribute("aria-selected")).toBe("true");

    window.location.hash = "#/channels";
    await fireEvent(window, new HashChangeEvent("hashchange"));

    const channelsTab = screen.getByRole("tab", { name: /channels/i });
    await waitFor(() =>
      expect(channelsTab.getAttribute("aria-selected")).toBe("true"),
    );
    expect(chatTab.getAttribute("aria-selected")).toBe("false");
  });

  it("canonicalises a stale hash reached after load, not just on initial boot", async () => {
    // Initial-load canonicalisation rewrites a stale/unavailable deep-link hash
    // to the fallback route. The same correction must apply when a stale hash is
    // reached *after* boot (a manual address-bar edit, or any external hash
    // navigation): the listener falls the active tab back to the first panel, so
    // leaving the URL pointing at #/memory would dangle a route that resolves to
    // a different tab — exactly the drift canonicalisation exists to prevent.
    loadBootstrap.mockResolvedValue({
      config: {
        panels: {
          chat: { enabled: true, available: true },
          memory_strip: { enabled: true, available: false },
        },
      },
      context: { principal: "local", tenant: "local", authenticated: false },
    });

    render(App);

    const chatTab = await screen.findByRole("tab", { name: /chat/i });
    expect(chatTab.getAttribute("aria-selected")).toBe("true");

    // Navigate to a known-but-unavailable panel after boot.
    window.location.hash = "#/memory";
    await fireEvent(window, new HashChangeEvent("hashchange"));

    // The active tab falls back to chat AND the URL is rewritten to match it.
    await waitFor(() => expect(window.location.hash).toBe("#/chat"));
    expect(chatTab.getAttribute("aria-selected")).toBe("true");
  });
});
