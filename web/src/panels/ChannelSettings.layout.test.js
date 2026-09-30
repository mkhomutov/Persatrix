import { describe, it, expect, vi, afterEach } from "vitest";
import {
  render,
  cleanup,
  screen,
  fireEvent,
  waitFor,
} from "@testing-library/svelte";
import ChannelSettings from "./ChannelSettings.svelte";

// Reading and editing nineteen knobs has to be scannable: the knobs sit under
// section headings by what they govern, each says in one line what it does,
// and the form keeps count of unsaved edits with a way to throw them away.
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function configBody() {
  return {
    revision: 1,
    floor_control: { value: true, source: "channel" },
    salience_max_channel_members: { value: 8, source: "default" },
    max_cascade_depth: { value: 5, source: "default" },
    max_replies_per_participant_per_interaction: { value: 4, source: "default" },
    end_vote_threshold: { value: 2, source: "default" },
    end_vote_window: { value: 3, source: "default" },
    escalation_chair_id: { value: null, source: "default" },
    interaction_idle_timeout_seconds: { value: 600, source: "default" },
    interaction_budget_tokens: { value: 0, source: "default" },
    reasoning: {
      mode: { value: "off", source: "default" },
      model: { value: "fast", source: "default" },
      depth: { value: "shallow", source: "default" },
      revise: { value: 0, source: "default" },
    },
  };
}

function renderSettings(extra = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(configBody()) }),
    ),
  );
  return render(ChannelSettings, {
    props: {
      channelId: "group:planning",
      members: [{ id: "ada", respond: "always" }],
      agentsById: { ada: { id: "ada", name: "Ada" } },
      onChanged: vi.fn(() => Promise.resolve()),
      ...extra,
    },
  });
}

describe("ChannelSettings layout", () => {
  it("groups the knobs under headings by what they govern", async () => {
    renderSettings();
    await screen.findByLabelText("Floor control");

    const headings = screen.getAllByRole("heading").map((h) => h.textContent.trim());
    expect(headings).toEqual(
      expect.arrayContaining(["Turn-taking", "Closing an interaction", "Reasoning"]),
    );
  });

  it("describes each knob in one line", async () => {
    renderSettings();
    await screen.findByLabelText("Floor control");

    // The end-vote window counts consecutive turns (channels.EndVoteWindow).
    expect(screen.getByText(/within this many consecutive turns/i)).toBeTruthy();
    expect(screen.getByText(/one at a time/i)).toBeTruthy();
  });

  it("labels the end-vote window in turns, not seconds", async () => {
    renderSettings();
    expect(await screen.findByLabelText("End-vote window (turns)")).toBeTruthy();
    expect(screen.queryByLabelText(/end-vote window \(seconds\)/i)).toBeNull();
  });

  it("counts unsaved edits and discards them back to the loaded config", async () => {
    renderSettings();
    await screen.findByLabelText("Floor control");
    expect(screen.queryByText(/unsaved change/i)).toBeNull();

    const inherit = screen.getByLabelText("Inherit fleet default for Max cascade depth");
    await fireEvent.click(inherit);
    expect(await screen.findByText("1 unsaved change")).toBeTruthy();

    await fireEvent.click(screen.getByLabelText("Inherit fleet default for Floor control"));
    expect(await screen.findByText("2 unsaved changes")).toBeTruthy();

    await fireEvent.click(screen.getByRole("button", { name: /discard/i }));
    await waitFor(() => expect(screen.queryByText(/unsaved change/i)).toBeNull());
    expect(
      screen.getByLabelText("Inherit fleet default for Max cascade depth").checked,
    ).toBe(true);
    expect(
      screen.getByLabelText("Inherit fleet default for Floor control").checked,
    ).toBe(false);
    expect(screen.getByRole("button", { name: /save settings/i }).disabled).toBe(true);
  });

  it("says an override left without a value will not be saved, and offers Discard", async () => {
    renderSettings();
    await screen.findByLabelText("Floor control");

    // The chair has no inherited value, so overriding it leaves a blank pick:
    // nothing to send, but the row now reads "Overridden on this channel".
    await fireEvent.click(screen.getByLabelText("Inherit fleet default for Escalation chair"));

    expect(await screen.findByText(/1 override without a value/i)).toBeTruthy();
    expect(screen.queryByText(/up to date/i)).toBeNull();
    expect(screen.getByRole("button", { name: /save settings/i }).disabled).toBe(true);

    await fireEvent.click(screen.getByRole("button", { name: /discard/i }));
    await waitFor(() => expect(screen.getByText(/up to date/i)).toBeTruthy());
    expect(
      screen.getByLabelText("Inherit fleet default for Escalation chair").checked,
    ).toBe(true);
  });
});

describe("ChannelSettings while the Details panel is folded", () => {
  it("loads a channel's settings only once the panel is shown", async () => {
    const { rerender } = renderSettings({ active: false });
    await Promise.resolve();
    expect(fetch).not.toHaveBeenCalled();

    rerender({ active: true });
    await screen.findByLabelText("Floor control");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("keeps unsaved edits when folded and shown again on the same channel", async () => {
    const { rerender } = renderSettings();
    await screen.findByLabelText("Floor control");
    await fireEvent.click(screen.getByLabelText("Inherit fleet default for Max cascade depth"));
    expect(await screen.findByText("1 unsaved change")).toBeTruthy();

    rerender({ active: false });
    rerender({ active: true });

    expect(screen.getByText("1 unsaved change")).toBeTruthy();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("loads the channel picked while folded once the panel is shown", async () => {
    const { rerender } = renderSettings();
    await screen.findByLabelText("Floor control");

    rerender({ active: false, channelId: "group:ops" });
    expect(fetch).toHaveBeenCalledTimes(1);

    rerender({ active: true });
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(fetch.mock.calls[1][0]).toBe("/api/v1/channels/group%3Aops/config");
  });
});
