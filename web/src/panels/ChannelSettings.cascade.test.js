import { describe, it, expect, vi, afterEach } from "vitest";
import {
  render,
  cleanup,
  screen,
  fireEvent,
  waitFor,
} from "@testing-library/svelte";
import ChannelSettings from "./ChannelSettings.svelte";

// ISSUE-0114 (v0.3.13): the per-channel `max_cascade_depth` knob — the
// productive-discussion length control — on the Channel-settings panel. Split
// into its own per-concern test file (the ChannelSettings.autonomous.test.js
// precedent) so the main suite stays under the 500-line cap. The knob is an
// ordinary flat int row driven off lib/channelKnobs.js; these tests pin its
// render (effective value + provenance) and the sparse PATCH round-trip so a
// registry edit that drops or mistypes the row goes red here.
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// The wire shape the server returns; max_cascade_depth inherits the fleet cap
// unless a test overrides the cell. Mirrors the main suite's configBody.
function configBody(overrides = {}) {
  return {
    revision: 3,
    floor_control: { value: true, source: "default" },
    salience_max_channel_members: { value: 8, source: "default" },
    max_cascade_depth: { value: 5, source: "default" },
    max_replies_per_participant_per_interaction: { value: 4, source: "default" },
    end_vote_threshold: { value: 2, source: "default" },
    end_vote_window: { value: 600, source: "default" },
    escalation_chair_id: { value: "", source: "default" },
    interaction_idle_timeout_seconds: { value: 900, source: "default" },
    interaction_budget_tokens: { value: 0, source: "default" },
    reasoning: {
      mode: { value: "off", source: "default" },
      model: { value: "fast", source: "default" },
      depth: { value: "shallow", source: "default" },
      revise: { value: 0, source: "default" },
    },
    ...overrides,
  };
}

function okJSON(body) {
  return { ok: true, status: 200, json: () => Promise.resolve(body) };
}

function renderSettings(props = {}) {
  return render(ChannelSettings, {
    props: {
      channelId: "group:planning",
      members: [{ id: "ada", respond: "always" }],
      agentsById: { ada: { id: "ada", name: "Ada" } },
      onChanged: vi.fn(() => Promise.resolve()),
      ...props,
    },
  });
}

describe("ChannelSettings max_cascade_depth (ISSUE-0114)", () => {
  it("renders the inherited fleet cap with default provenance", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(okJSON(configBody()))));
    renderSettings();

    // Router-held like every flat knob: an inherited cap reads back the fleet
    // value (never null) and is flagged inherited so the honest "5" is not
    // mistaken for a channel override.
    const cap = await screen.findByLabelText("Max cascade depth");
    expect(cap.value).toBe("5");
    expect(
      screen.getByLabelText("Inherit fleet default for Max cascade depth")
        .checked,
    ).toBe(true);
  });

  it("renders a channel override with its provenance badge", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          okJSON(
            configBody({
              max_cascade_depth: { value: 3, source: "channel" },
            }),
          ),
        ),
      ),
    );
    renderSettings();

    const cap = await screen.findByLabelText("Max cascade depth");
    expect(cap.value).toBe("3");
    expect(
      screen.getByLabelText("Inherit fleet default for Max cascade depth")
        .checked,
    ).toBe(false);
  });

  it("sends only max_cascade_depth in a sparse PATCH when overridden", async () => {
    const fetchMock = vi.fn((path, init) =>
      Promise.resolve(
        okJSON(
          init?.method === "PATCH"
            ? configBody({
                revision: 4,
                max_cascade_depth: { value: 3, source: "channel" },
              })
            : configBody(),
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    const cap = await screen.findByLabelText("Max cascade depth");
    // Un-inherit the knob, set the channel's own cap, save.
    await fireEvent.click(
      screen.getByLabelText("Inherit fleet default for Max cascade depth"),
    );
    await fireEvent.input(cap, { target: { value: "3" } });
    await fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => {
      const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
      expect(patch).toBeTruthy();
      const [, init] = patch;
      expect(JSON.parse(init.body)).toEqual({ max_cascade_depth: 3 });
      expect(init.headers["If-Match"]).toBe("3");
    });
  });
});
