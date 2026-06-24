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
// inherit the fleet default. interaction_budget_tokens reads back its effective
// value like every other knob (the RFC 0050 interaction-budget amendment made it
// router-held; an inherited uncapped budget is 0, not the old Open-item-4 null).
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
    interaction_budget_tokens: { value: 0, source: "default" },
    // RFC 0051 PR 5: the nested reasoning block. Each sub-knob is a {value,
    // source} cell exactly like a flat knob; all inherit the fleet default here.
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
    // inherit — six flat + the four nested reasoning.* sub-knobs = ten. The
    // provenance vocabulary is the user-facing rendering of `source`.
    expect(screen.getAllByText("Overridden on this channel").length).toBe(2);
    expect(screen.getAllByText("Inherited default").length).toBe(10);

    // It fetched the encoded config route, not anything else.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/channels/group%3Aplanning/config",
    );
  });

  it("renders an inherited interaction_budget_tokens with its effective value (router-held since the amendment)", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(okJSON(configBody())));
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    // Post-amendment the budget is router-held and resolves like any other knob:
    // an inherited uncapped budget reads back 0 (not the old Open-item-4 null),
    // and renders its effective value, marked inherited.
    const budget = await screen.findByLabelText("Interaction budget (tokens)");
    expect(budget.value).toBe("0");
    // ...and the source:"default" read-back is reflected as inherited, so the
    // honest "0" isn't mistaken for a channel override (the comment's claim).
    expect(
      screen.getByLabelText("Inherit fleet default for Interaction budget (tokens)")
        .checked,
    ).toBe(true);
  });

  it("renders a genuinely null knob value as empty (never coerced to 0)", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        okJSON(
          configBody({
            interaction_budget_tokens: { value: null, source: "default" },
          }),
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    // The generic null→empty guard still holds for any knob that ever reports a
    // null effective value — it must render blank, not a lying "0".
    const budget = await screen.findByLabelText("Interaction budget (tokens)");
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

  it("on a 409 it preserves the operator's edit and re-saves against the refreshed revision", async () => {
    let gets = 0;
    let patches = 0;
    const fetchMock = vi.fn((path, init) => {
      if (init?.method === "PATCH") {
        patches += 1;
        // First save loses the race (409); the retry — now carrying the
        // refreshed revision — succeeds.
        if (patches === 1) {
          return Promise.resolve(
            errJSON(409, { error: "config revision conflict", code: "CONFLICT" }),
          );
        }
        return Promise.resolve(okJSON(configBody({ revision: 8 })));
      }
      gets += 1;
      // initial load -> revision 3; post-conflict reload -> revision 7
      return Promise.resolve(okJSON(configBody({ revision: gets === 1 ? 3 : 7 })));
    });
    vi.stubGlobal("fetch", fetchMock);
    const onChanged = vi.fn(() => Promise.resolve());
    renderSettings({ onChanged });

    const floor = await screen.findByLabelText("Floor control");
    await fireEvent.click(floor); // true -> false
    await fireEvent.click(screen.getByRole("button", { name: /save settings/i }));

    // Let the conflict fully settle: the notice appears once the reload finishes
    // and the in-flight save resolves (button back to its idle "Save settings"
    // label, not "Saving…").
    await screen.findByRole("alert");
    await waitFor(() => expect(gets).toBe(2));

    // After the conflict reload the edit must SURVIVE — discarding it would make
    // the "review your edits and save again" notice a lie and force a re-type.
    expect(screen.getByLabelText("Floor control").checked).toBe(false);
    const saveBtn = await screen.findByRole("button", { name: /save settings/i });
    expect(saveBtn.disabled).toBe(false); // still dirty, saveable again

    // The retry rides the REFRESHED revision (7), not the stale 3, and replays
    // the preserved edit verbatim.
    await fireEvent.click(saveBtn);
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    const patchCalls = fetchMock.mock.calls.filter((c) => c[1]?.method === "PATCH");
    expect(patchCalls[1][1].headers["If-Match"]).toBe("7");
    expect(JSON.parse(patchCalls[1][1].body)).toEqual({ floor_control: false });
  });

  it("overriding an inherited chair without picking a member emits no empty id", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        okJSON(
          configBody({ escalation_chair_id: { value: null, source: "default" } }),
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    const inherit = await screen.findByLabelText(
      "Inherit fleet default for Escalation chair",
    );
    expect(inherit.checked).toBe(true);
    await fireEvent.click(inherit); // flip to override, but pick nobody

    // A blank chair override has nothing concrete to send: the panel must not
    // emit escalation_chair_id:"" (a guaranteed 400), the same way a blank int
    // override is skipped. With nothing to save, the button stays disabled.
    expect(screen.getByRole("button", { name: /save/i }).disabled).toBe(true);
  });

  it("renders each knob label once — the control is labelled, not duplicated", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(okJSON(configBody())));
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    await screen.findByLabelText("Floor control");
    // The label shows once as the row heading; the control carries it as an
    // accessible name (aria-label), not a second visible copy of the text.
    expect(screen.getAllByText("Floor control").length).toBe(1);
  });

  it("clears the previous channel's form while the next channel's config loads", async () => {
    let resolveSecond;
    let calls = 0;
    const fetchMock = vi.fn(() => {
      calls += 1;
      if (calls === 1) return Promise.resolve(okJSON(configBody()));
      return new Promise((res) => (resolveSecond = res)); // second channel: pending
    });
    vi.stubGlobal("fetch", fetchMock);
    const { rerender } = renderSettings();
    await screen.findByLabelText("Floor control");

    await rerender({ channelId: "group:other" });
    // While the new channel loads, the prior channel's form must not linger —
    // otherwise an operator could edit/save against the wrong channel's state.
    await waitFor(() =>
      expect(screen.queryByLabelText("Floor control")).toBeNull(),
    );
    expect(screen.getByText(/loading settings/i)).toBeTruthy();

    resolveSecond(okJSON(configBody())); // let the pending load settle
  });

  it("does not adopt a save response after the channel switched mid-request", async () => {
    let resolvePatch;
    const fetchMock = vi.fn((path, init) => {
      if (init?.method === "PATCH") {
        return new Promise((res) => (resolvePatch = res)); // hold the save open
      }
      return Promise.resolve(okJSON(configBody()));
    });
    vi.stubGlobal("fetch", fetchMock);
    const onChanged = vi.fn(() => Promise.resolve());
    const { rerender } = renderSettings({ onChanged });

    const floor = await screen.findByLabelText("Floor control");
    await fireEvent.click(floor); // edit the old channel
    await fireEvent.click(screen.getByRole("button", { name: /save settings/i }));

    // Operator navigates to another channel before the save resolves; the new
    // channel loads its own (fresh) config while the old save is still in flight.
    await rerender({ channelId: "group:other" });
    await screen.findByLabelText("Floor control");

    // The stale save now resolves with a bumped revision. It must NOT be adopted
    // onto the channel we've since moved to: no success notice, and onChanged
    // (which would refresh siblings) is not called for a channel left behind.
    resolvePatch(okJSON(configBody({ revision: 99 })));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /save settings/i }),
      ).toBeTruthy(),
    );
    expect(screen.queryByText("Settings saved.")).toBeNull();
    expect(onChanged).not.toHaveBeenCalled();
  });

  // ─── RFC 0051 PR 5: the nested reasoning block ──────────────────────

  it("renders the reasoning mode select with its option set and effective value", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        okJSON(
          configBody({
            reasoning: {
              mode: { value: "bid", source: "channel" },
              model: { value: "fast", source: "default" },
              depth: { value: "shallow", source: "default" },
              revise: { value: 0, source: "default" },
            },
          }),
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    // The mode select reads back its effective value and offers off/bid/plan.
    const mode = await screen.findByLabelText("Reasoning mode");
    expect(mode.value).toBe("bid");
    const options = [...mode.querySelectorAll("option")].map((o) => o.value);
    expect(options).toEqual(["off", "bid", "plan"]);

    // depth offers only the v0.3.10-accepted `shallow` — not a dead `deep` entry.
    const depth = screen.getByLabelText("Reasoning depth");
    expect([...depth.querySelectorAll("option")].map((o) => o.value)).toEqual([
      "shallow",
    ]);
  });

  it("sends a changed reasoning sub-knob as a NESTED sparse PATCH", async () => {
    const fetchMock = vi.fn((path, init) =>
      Promise.resolve(
        okJSON(init?.method === "PATCH" ? configBody({ revision: 4 }) : configBody()),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onChanged = vi.fn(() => Promise.resolve());
    renderSettings({ onChanged });

    // Override reasoning.mode: inherit -> bid. The knob defaults to inherited, so
    // flip it to an override first, then pick the value.
    const inherit = await screen.findByLabelText(
      "Inherit fleet default for Reasoning mode",
    );
    await fireEvent.click(inherit); // -> override
    const mode = screen.getByLabelText("Reasoning mode");
    await fireEvent.change(mode, { target: { value: "bid" } });
    await fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    const patchCall = fetchMock.mock.calls.find((c) => c[1]?.method === "PATCH");
    // The body nests the sub-knob under "reasoning" — NOT a flat "reasoning.mode"
    // key (the server's nested merge would reject that).
    expect(JSON.parse(patchCall[1].body)).toEqual({ reasoning: { mode: "bid" } });
  });

  it("reverting an overridden reasoning sub-knob nests an explicit null", async () => {
    const fetchMock = vi.fn((path, init) =>
      Promise.resolve(
        okJSON(
          init?.method === "PATCH"
            ? configBody({ revision: 4 })
            : configBody({
                reasoning: {
                  mode: { value: "plan", source: "channel" },
                  model: { value: "fast", source: "default" },
                  depth: { value: "shallow", source: "default" },
                  revise: { value: 0, source: "default" },
                },
              }),
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    // reasoning.mode is overridden ("plan"); reverting it to inherit nests an
    // explicit null so the server clears just that sub-knob.
    const revert = await screen.findByLabelText(
      "Inherit fleet default for Reasoning mode",
    );
    await fireEvent.click(revert);
    await fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() =>
      expect(fetchMock.mock.calls.some((c) => c[1]?.method === "PATCH")).toBe(true),
    );
    const patchCall = fetchMock.mock.calls.find((c) => c[1]?.method === "PATCH");
    expect(JSON.parse(patchCall[1].body)).toEqual({ reasoning: { mode: null } });
  });
});
