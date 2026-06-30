import { describe, it, expect, vi, afterEach } from "vitest";
import {
  render,
  cleanup,
  screen,
  fireEvent,
  waitFor,
} from "@testing-library/svelte";
import ChannelSettings from "./ChannelSettings.svelte";

// RFC 0052 PR 2: the nested `autonomous` block of the Channel-settings panel —
// split into its own per-concern test file (like the ChannelTimeline.*.test.js
// suites) so neither file busts the 500-line cap. The block renders in the
// AutonomousSettings child but shares the panel's one draft/patch/save path, so
// these drive the integration through ChannelSettings exactly as the reasoning
// tests in ChannelSettings.test.js do. They exercise the block's new control
// types: text (topic/goal), the multiline `agenda` list (a `[]string` on the
// wire), and the convener member picker.
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// A config body where the autonomous block is overridden on the channel; the rest
// inherit. Mirrors the wire shape the server returns (each sub-knob a {value,
// source} cell, agenda a JSON array).
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
    reasoning: {
      mode: { value: "off", source: "default" },
      model: { value: "fast", source: "default" },
      depth: { value: "shallow", source: "default" },
      revise: { value: 0, source: "default" },
    },
    autonomous: {
      enabled: { value: false, source: "default" },
      topic: { value: "", source: "default" },
      agenda: { value: [], source: "default" },
      convener: { value: "", source: "default" },
      goal: { value: "", source: "default" },
      max_rounds: { value: 12, source: "default" },
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
      members: [
        { id: "ada", respond: "always" },
        { id: "bob", respond: "never" }, // observer — excluded from the picker
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

// An autonomous block overridden on the channel — exercises the new control
// types (text topic/goal, the multiline agenda list, the convener picker).
const overriddenAutonomous = {
  enabled: { value: true, source: "channel" },
  topic: { value: "Monorepo?", source: "channel" },
  agenda: { value: ["Build cost", "Coupling risk"], source: "channel" },
  convener: { value: "ada", source: "channel" },
  goal: { value: "A recommendation", source: "channel" },
  max_rounds: { value: 20, source: "channel" },
};

describe("ChannelSettings — autonomous block", () => {
  it("renders the autonomous controls with their effective values", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(okJSON(configBody({ autonomous: overriddenAutonomous }))),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    // The enabled checkbox + text/number inputs read back their effective value.
    expect(
      (await screen.findByLabelText("Autonomous (human-free) mode")).checked,
    ).toBe(true);
    expect(screen.getByLabelText("Topic").value).toBe("Monorepo?");
    expect(screen.getByLabelText("Max rounds").value).toBe("20");

    // The agenda (a `[]string` on the wire) renders as one item per line.
    expect(screen.getByLabelText("Agenda (one item per line)").value).toBe(
      "Build cost\nCoupling risk",
    );

    // The convener picker resolves to its member and excludes the observer (bob,
    // respond: never) exactly as the chair picker does.
    const convener = screen.getByLabelText("Convener");
    expect(convener.value).toBe("ada");
    const options = [...convener.querySelectorAll("option")]
      .map((o) => o.value)
      .filter((v) => v !== "");
    expect(options).toEqual(["ada"]);
  });

  it("sends changed autonomous sub-knobs as a NESTED PATCH (agenda as a JSON array)", async () => {
    const fetchMock = vi.fn((path, init) =>
      Promise.resolve(
        okJSON(init?.method === "PATCH" ? configBody({ revision: 4 }) : configBody()),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onChanged = vi.fn(() => Promise.resolve());
    renderSettings({ onChanged });

    // All autonomous knobs start inherited; override enabled (bool) + agenda (list)
    // and confirm both nest under "autonomous", the agenda as a real string array
    // (split per line, trimmed) — not a flat key or a newline string.
    await fireEvent.click(
      await screen.findByLabelText(
        "Inherit fleet default for Autonomous (human-free) mode",
      ),
    );
    await fireEvent.click(screen.getByLabelText("Autonomous (human-free) mode")); // -> true
    await fireEvent.click(
      screen.getByLabelText("Inherit fleet default for Agenda (one item per line)"),
    );
    // A textarea's bind:value updates on the `input` event. Blank lines + padding
    // are dropped, mirroring the server's non-blank, trimmed agenda-item check.
    await fireEvent.input(screen.getByLabelText("Agenda (one item per line)"), {
      target: { value: "Build cost\n  Coupling risk \n\n" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    const patchCall = fetchMock.mock.calls.find((c) => c[1]?.method === "PATCH");
    expect(JSON.parse(patchCall[1].body)).toEqual({
      autonomous: { enabled: true, agenda: ["Build cost", "Coupling risk"] },
    });
  });

  it("sends a changed convener as a NESTED PATCH", async () => {
    const fetchMock = vi.fn((path, init) =>
      Promise.resolve(
        okJSON(init?.method === "PATCH" ? configBody({ revision: 4 }) : configBody()),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    // The convener starts inherited (a disabled picker). Override it and pick the
    // one floor-capable member (ada; bob is an observer), and confirm it nests
    // under "autonomous" — the convener (a string-valued select) was previously
    // only read back, never exercised through the patch path.
    await fireEvent.click(
      await screen.findByLabelText("Inherit fleet default for Convener"),
    );
    await fireEvent.change(screen.getByLabelText("Convener"), {
      target: { value: "ada" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() =>
      expect(fetchMock.mock.calls.some((c) => c[1]?.method === "PATCH")).toBe(true),
    );
    const patchCall = fetchMock.mock.calls.find((c) => c[1]?.method === "PATCH");
    expect(JSON.parse(patchCall[1].body)).toEqual({ autonomous: { convener: "ada" } });
  });

  it("sends an emptied free-text override explicitly (does not silently drop it)", async () => {
    // Regression: a `text` knob (topic) overridden to "" must ride as an explicit
    // empty-string override — parity with the CLI's `autonomous.topic=` — NOT be
    // skipped like an unpicked select. Skipping it would leave the patch empty, so
    // the next adopt would silently revert the operator's clear back to the old value.
    const fetchMock = vi.fn((path, init) =>
      Promise.resolve(
        okJSON(
          init?.method === "PATCH"
            ? configBody({ revision: 4 })
            : configBody({ autonomous: overriddenAutonomous }),
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    const topic = await screen.findByLabelText("Topic");
    expect(topic.value).toBe("Monorepo?"); // overridden on the channel
    await fireEvent.input(topic, { target: { value: "" } });
    await fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() =>
      expect(fetchMock.mock.calls.some((c) => c[1]?.method === "PATCH")).toBe(true),
    );
    const patchCall = fetchMock.mock.calls.find((c) => c[1]?.method === "PATCH");
    expect(JSON.parse(patchCall[1].body)).toEqual({ autonomous: { topic: "" } });
  });

  it("reverting an overridden autonomous sub-knob nests an explicit null", async () => {
    const fetchMock = vi.fn((path, init) =>
      Promise.resolve(
        okJSON(
          init?.method === "PATCH"
            ? configBody({ revision: 4 })
            : configBody({ autonomous: overriddenAutonomous }),
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    // autonomous.topic is overridden; reverting it nests an explicit null so the
    // server clears just that sub-knob (not the whole block).
    await fireEvent.click(
      await screen.findByLabelText("Inherit fleet default for Topic"),
    );
    await fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() =>
      expect(fetchMock.mock.calls.some((c) => c[1]?.method === "PATCH")).toBe(true),
    );
    const patchCall = fetchMock.mock.calls.find((c) => c[1]?.method === "PATCH");
    expect(JSON.parse(patchCall[1].body)).toEqual({ autonomous: { topic: null } });
  });
});

// RFC 0052 §B PR 3: the Convene action — the panel's first per-channel action
// button. It lives in the AutonomousSettings child but is driven here through
// ChannelSettings (the integration the operator actually uses), gated on the
// SAVED armed state and disabled while there are unsaved edits.
describe("ChannelSettings — convene action", () => {
  it("hides the Convene button when the channel is not armed", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(okJSON(configBody())));
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    // Wait for the panel to load (the topic field renders), then assert no button.
    await screen.findByLabelText("Topic");
    expect(screen.queryByRole("button", { name: /convene/i })).toBeNull();
  });

  it("shows Convene and posts to the convene endpoint when armed", async () => {
    const fetchMock = vi.fn((path, init) => {
      if (String(path).endsWith("/convene")) {
        return Promise.resolve({
          ok: true,
          status: 202,
          json: () =>
            Promise.resolve({
              channel_id: "group:planning",
              convener: "ada",
              status: "convening",
            }),
        });
      }
      return Promise.resolve(okJSON(configBody({ autonomous: overriddenAutonomous })));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    const button = await screen.findByRole("button", { name: /convene/i });
    await fireEvent.click(button);

    // The POST lands on the encoded {id}/convene route…
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          (c) =>
            String(c[0]).endsWith("/convene") && c[1]?.method === "POST",
        ),
      ).toBe(true),
    );
    const conveneCall = fetchMock.mock.calls.find((c) =>
      String(c[0]).endsWith("/convene"),
    );
    expect(conveneCall[0]).toBe("/api/v1/channels/group%3Aplanning/convene");
    // …and the convener from the ack surfaces in the success notice.
    await waitFor(() =>
      expect(screen.getByText(/ada is opening the discussion/i)).toBeTruthy(),
    );
  });

  it("disables Convene while there are unsaved edits", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(okJSON(configBody({ autonomous: overriddenAutonomous }))),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    const button = await screen.findByRole("button", { name: /convene/i });
    expect(button.disabled).toBe(false);

    // Editing a knob makes the panel dirty; convening reads the persisted block,
    // so the action disables and tells the operator to save first.
    await fireEvent.input(screen.getByLabelText("Topic"), {
      target: { value: "Monorepo? (revised)" },
    });
    await waitFor(() => expect(button.disabled).toBe(true));
    expect(screen.getByText(/save your changes before convening/i)).toBeTruthy();
  });

  it("latches disabled after a successful convene (no accidental second opener)", async () => {
    let conveneCalls = 0;
    const fetchMock = vi.fn((path, init) => {
      if (String(path).endsWith("/convene") && init?.method === "POST") {
        conveneCalls += 1;
        return Promise.resolve({
          ok: true,
          status: 202,
          json: () =>
            Promise.resolve({ channel_id: "group:planning", convener: "ada", status: "convening" }),
        });
      }
      return Promise.resolve(okJSON(configBody({ autonomous: overriddenAutonomous })));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    const button = await screen.findByRole("button", { name: /convene/i });
    await fireEvent.click(button);
    await waitFor(() => expect(screen.getByText(/ada is opening the discussion/i)).toBeTruthy());

    // The button is now latched disabled (re-convening an idle channel is not
    // yet aggregate-bounded; a second POST before the first opener commits would
    // dispatch a second uncapped opener).
    await waitFor(() => expect(button.disabled).toBe(true));
    expect(button.textContent).toMatch(/convened/i);
    // A second click does nothing — still exactly one convene POST.
    await fireEvent.click(button);
    expect(conveneCalls).toBe(1);
  });

  it("surfaces the server's wording when convene fails", async () => {
    const fetchMock = vi.fn((path) => {
      if (String(path).endsWith("/convene")) {
        return Promise.resolve({
          ok: false,
          status: 409,
          json: () =>
            Promise.resolve({
              error: "channel is not autonomous-enabled",
              code: "CONFLICT",
            }),
        });
      }
      return Promise.resolve(okJSON(configBody({ autonomous: overriddenAutonomous })));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderSettings();

    await fireEvent.click(await screen.findByRole("button", { name: /convene/i }));
    await waitFor(() =>
      expect(screen.getByText(/not autonomous-enabled/i)).toBeTruthy(),
    );
  });
});
