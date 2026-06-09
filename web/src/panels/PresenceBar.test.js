import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/svelte";
import PresenceBar from "./PresenceBar.svelte";

// PresenceBar is the live status line above the composer (RFC 0048 console,
// Tier 0). It is purely presentational — its owner (ChannelTimeline) decides who
// is thinking and when the turn flips back to the operator; this spec pins the
// rendering of each state and the optional cancel affordance.
afterEach(cleanup);

const AGENTS = {
  "ember-owl": { id: "ember-owl", name: "Ember Owl", role: "Strategist" },
  "crimson-fox": { id: "crimson-fox", name: "Crimson Fox" },
};

function renderBar(props = {}) {
  return render(PresenceBar, {
    props: { thinking: [], agentsById: AGENTS, slow: false, idle: false, ...props },
  });
}

describe("PresenceBar", () => {
  it("renders nothing when nobody is working and the bar is not idle-flashing", () => {
    const { container } = renderBar();
    expect(container.querySelector(".presence")).toBeNull();
  });

  it("names a single thinking persona", () => {
    renderBar({ thinking: ["ember-owl"] });
    expect(screen.getByText("Ember Owl is thinking…")).toBeTruthy();
  });

  it("aggregates two thinking personas", () => {
    renderBar({ thinking: ["ember-owl", "crimson-fox"] });
    expect(
      screen.getByText("Ember Owl and Crimson Fox are thinking…"),
    ).toBeTruthy();
  });

  it("softens to 'taking a while' when slow", () => {
    renderBar({ thinking: ["ember-owl"], slow: true });
    expect(screen.getByText("Ember Owl is taking a while…")).toBeTruthy();
  });

  it("shows the brief idle hint when the turn flips back to the operator", () => {
    renderBar({ idle: true });
    expect(screen.getByText(/waiting for you/i)).toBeTruthy();
  });

  it("prefers the thinking state over an idle flag", () => {
    // A new turn started before the idle flash faded: the active state wins.
    renderBar({ thinking: ["ember-owl"], idle: true });
    expect(screen.getByText("Ember Owl is thinking…")).toBeTruthy();
    expect(screen.queryByText(/waiting for you/i)).toBeNull();
  });

  it("exposes the status to assistive tech as a polite live region", () => {
    renderBar({ thinking: ["ember-owl"] });
    const status = screen.getByRole("status");
    expect(status.getAttribute("aria-live")).toBe("polite");
  });

  it("renders a Cancel control only when onCancel is provided", () => {
    const onCancel = vi.fn();
    const { rerender } = renderBar({ thinking: ["ember-owl"] });
    expect(screen.queryByRole("button", { name: /cancel/i })).toBeNull();

    rerender({ thinking: ["ember-owl"], agentsById: AGENTS, onCancel });
    const button = screen.getByRole("button", { name: /cancel/i });
    fireEvent.click(button);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("does not render a Cancel control in the idle state", () => {
    renderBar({ idle: true, onCancel: vi.fn() });
    expect(screen.queryByRole("button", { name: /cancel/i })).toBeNull();
  });
});
