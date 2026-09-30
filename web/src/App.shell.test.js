import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  cleanup,
  fireEvent,
  waitFor,
} from "@testing-library/svelte";
import App from "./App.svelte";

// Shell conveniences in the topbar: a colour-theme switch that pins light or
// dark over the OS preference (and remembers it per browser), and a
// keyboard-shortcut reference. Neither depends on the backend beyond boot.
vi.mock("./lib/api.js", () => ({
  ApiError: class ApiError extends Error {},
  loadBootstrap: vi.fn(),
  listAgents: vi.fn(() => Promise.resolve([])),
  listChannels: vi.fn(() => Promise.resolve({ channels: [] })),
  getChannelHistory: vi.fn(() => Promise.resolve({ messages: [] })),
  getChatHistory: vi.fn(() => Promise.resolve({ messages: [] })),
}));

import { loadBootstrap } from "./lib/api.js";

beforeEach(() => {
  window.location.hash = "";
  localStorage.clear();
  delete document.documentElement.dataset.theme;
  loadBootstrap.mockResolvedValue({
    config: { panels: { channel_timeline: { enabled: true, available: true } } },
    context: { principal: "local", authenticated: false },
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});

describe("App shell — theme and shortcuts", () => {
  it("cycles the colour theme system → light → dark → system", async () => {
    render(App);
    const theme = await screen.findByRole("button", { name: /theme: system/i });

    await fireEvent.click(theme);
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(screen.getByRole("button", { name: /theme: light/i })).toBeTruthy();

    await fireEvent.click(theme);
    expect(document.documentElement.dataset.theme).toBe("dark");

    await fireEvent.click(theme);
    expect(document.documentElement.dataset.theme).toBeUndefined();
    expect(screen.getByRole("button", { name: /theme: system/i })).toBeTruthy();
  });

  it("restores a remembered theme on load", async () => {
    localStorage.setItem("persatrix.console.theme", JSON.stringify("dark"));
    render(App);
    await screen.findByRole("button", { name: /theme: dark/i });
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("lists the keyboard shortcuts", async () => {
    const { container } = render(App);
    const help = await waitFor(() => {
      const el = container.querySelector("details.shortcuts");
      expect(el).not.toBeNull();
      return el;
    });
    expect(help.querySelector("summary").getAttribute("aria-label")).toMatch(
      /keyboard shortcuts/i,
    );
    expect(help.textContent).toMatch(/jump to a conversation/i);
  });

  it("closes the shortcut list on Escape and keeps that Escape to itself", async () => {
    const { container } = render(App);
    const help = await waitFor(() => {
      const el = container.querySelector("details.shortcuts");
      expect(el).not.toBeNull();
      return el;
    });
    help.open = true;
    await fireEvent(help, new Event("toggle"));

    // Handled here, so the sidebar drawer's own Escape leaves the drawer open.
    const notPrevented = await fireEvent.keyDown(window, { key: "Escape" });
    expect(notPrevented).toBe(false);
    expect(help.open).toBe(false);
  });

  it("shows the theme switch even when the backend is unreachable", async () => {
    loadBootstrap.mockRejectedValue(new Error("offline"));
    render(App);
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByRole("button", { name: /theme:/i })).toBeTruthy();
  });
});
