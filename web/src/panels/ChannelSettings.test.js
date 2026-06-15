import { describe, it, expect, vi, afterEach } from "vitest";
import {
  render,
  cleanup,
  screen,
  fireEvent,
  waitFor,
} from "@testing-library/svelte";
import ChannelSettings from "./ChannelSettings.svelte";

// ChannelSettings is the group-channel governance surface (RFC 0050 Phase 2 PR
// 2): on channel select it reads GET /api/v1/channels/{id}/config and renders
// the eight knobs, each with its effective value, a provenance badge from
// `source` (overridden-here vs inherited default), and an inherit/override
// control. Save collects ONLY the changed knobs into a sparse PATCH carrying the
// last-read revision in If-Match. A 409 reloads (never blind-overwrites). Unlike
// ChannelMembers, this component fetches its own config (it is not in the
// channel-list row), so the tests stub global fetch and assert on the wire.
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// A representative config body: revision + the eight {value, source} knobs.
// floor_control and escalation_chair_id are overridden on the channel; the rest
// inherit the fleet default. interaction_budget_tokens is the inherited-null
// case (RFC 0050 Phase 1 Open item 4 — not router-held, reads back value:null).
function configBody(overrides = {}) {
  return {
    revision: 3,
    floor_control: { value: true, source: "channel" },
    salience_max_channel_members: { value: 8, source: "default" },
    max_replies_per_participant_per_interaction: { value: 4, source: "default" },
    end_vote_threshold: { value: 2, source: "default" },
    end_vote_window: { value: 600, source: "default" },
    escalation_chair_id: { value: "ada", source: "channel" },
    interaction_idle_timeout_seconds: { value: 900, source: "default" },
    interaction_budget_tokens: { value: null, source: "default" },
    ...overrides,
  };
}

function okJSON(body) {
  return { ok: true, status: 200, json: () => Promise.resolve(body) };
}

function errJSON(status, body) {
  return { ok: false, status, json: () => Promise.resolve(body) };
}

function renderSettings(props = {}) {
  return render(ChannelSettings, {
    props: {
      channelId: "group:planning",
      members: [
        { id: "ada", respond: "always" },
        { id: "bob", respond: "never" }, // observer — excluded from chair picker
      ],
      agentsById: {
        ada: { id: "ada", name: "Ada" },
        bob: { id: "bob", name: "Bob" },
      },
      onChanged: vi.fn(() => Promise.resolve()),
      ...props,
    },
  });
}

describe("ChannelSettings", () => {
  it("loads the config on mount and renders each knob with its provenance", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(okJSON(configBody())));
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    // The floor-control override reads back true and is flagged as overridden.
    const floor = await screen.findByLabelText("Floor control");
    expect(floor.checked).toBe(true);
    // Two knobs are overridden (floor_control, escalation_chair_id); the rest
    // inherit. The provenance vocabulary is the user-facing rendering of `source`.
    expect(screen.getAllByText("Overridden on this channel").length).toBe(2);
    expect(screen.getAllByText("Inherited default").length).toBe(6);

    // It fetched the encoded config route, not anything else.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/channels/group%3Aplanning/config",
    );
  });

  it("renders an inherited interaction_budget_tokens as empty, never 0", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(okJSON(configBody())));
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    const budget = await screen.findByLabelText("Interaction budget (tokens)");
    // value:null inherited must render blank — coercing to "0" would lie about
    // the inherited state and would re-emit 0 on the next save.
    expect(budget.value).toBe("");
  });

  it("sends only the changed knob in a sparse PATCH carrying the revision in If-Match", async () => {
    const fetchMock = vi.fn((path, init) =>
      Promise.resolve(
        okJSON(init?.method === "PATCH" ? configBody({ revision: 4 }) : configBody()),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onChanged = vi.fn(() => Promise.resolve());
    renderSettings({ onChanged });

    const floor = await screen.findByLabelText("Floor control");
    await fireEvent.click(floor); // true -> false (still an override)
    await fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    const patchCall = fetchMock.mock.calls.find((c) => c[1]?.method === "PATCH");
    expect(patchCall[0]).toBe("/api/v1/channels/group%3Aplanning/config");
    expect(patchCall[1].headers["If-Match"]).toBe("3");
    // ONLY the touched knob — not the seven untouched ones.
    expect(JSON.parse(patchCall[1].body)).toEqual({ floor_control: false });
  });

  it("reverting a knob to inherit sends an explicit null (not an absent key)", async () => {
    const fetchMock = vi.fn((path, init) =>
      Promise.resolve(
        okJSON(init?.method === "PATCH" ? configBody({ revision: 4 }) : configBody()),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    // escalation_chair_id is overridden ("ada"); reverting it inherits the fleet
    // default, which the sparse contract expresses as an explicit null.
    const revert = await screen.findByLabelText(
      "Inherit fleet default for Escalation chair",
    );
    await fireEvent.click(revert);
    await fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() =>
      expect(fetchMock.mock.calls.some((c) => c[1]?.method === "PATCH")).toBe(true),
    );
    const patchCall = fetchMock.mock.calls.find((c) => c[1]?.method === "PATCH");
    const body = JSON.parse(patchCall[1].body);
    expect(body).toEqual({ escalation_chair_id: null });
    expect("escalation_chair_id" in body).toBe(true); // present, not dropped
  });

  it("on a 409 conflict it reloads the config and warns, never blind-overwriting", async () => {
    let gets = 0;
    const fetchMock = vi.fn((path, init) => {
      if (init?.method === "PATCH") {
        return Promise.resolve(
          errJSON(409, { error: "config revision conflict", code: "CONFLICT" }),
        );
      }
      gets += 1;
      return Promise.resolve(okJSON(configBody({ revision: gets === 1 ? 3 : 7 })));
    });
    vi.stubGlobal("fetch", fetchMock);
    const onChanged = vi.fn(() => Promise.resolve());
    renderSettings({ onChanged });

    const floor = await screen.findByLabelText("Floor control");
    await fireEvent.click(floor);
    await fireEvent.click(screen.getByRole("button", { name: /save/i }));

    // The conflict is surfaced, the config is re-read (initial GET + reload GET),
    // and onChanged is NOT called — a 409 must not look like a successful save.
    const alert = await screen.findByRole("alert");
    expect(alert.textContent.toLowerCase()).toMatch(/changed|reload/);
    expect(gets).toBe(2);
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("surfaces a load failure (e.g. 403 toggle off) without crashing", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        errJSON(403, {
          error: "channel config editing is disabled",
          code: "FORBIDDEN",
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent.toLowerCase()).toMatch(/disabled/);
    // No knob controls rendered when the config never loaded.
    expect(screen.queryByLabelText("Floor control")).toBeNull();
  });
});
