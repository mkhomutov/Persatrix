import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/svelte";
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
});
