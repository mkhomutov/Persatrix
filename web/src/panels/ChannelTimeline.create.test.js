import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/svelte";
import ChannelTimeline from "./ChannelTimeline.svelte";

// Console channel creation (RFC 0048 channel-creation amendment §A–§D): the
// Channels panel surfaces the already-exposed POST /api/v1/channels behind a
// collapsed "New channel" form, gated on the server-reported create capability
// (create.enabled && create.available) threaded in as the `canCreate` prop. The
// backend client is mocked so the form's wiring is exercised without a running
// orchestrator. Split from ChannelTimeline.test.js to keep each spec under the
// review-size cap, mirroring the crosspanel split.
vi.mock("../lib/api.js", () => ({
  ApiError: class ApiError extends Error {
    constructor(message, status, options) {
      super(message, options);
      this.name = "ApiError";
      this.status = status;
      this.code = options?.code;
    }
  },
  listAgents: vi.fn(),
  listChannels: vi.fn(),
  getChannelHistory: vi.fn(),
  publishMessage: vi.fn(),
  createChannel: vi.fn(),
}));

import {
  listAgents,
  listChannels,
  getChannelHistory,
  createChannel,
  ApiError,
} from "../lib/api.js";
import { nav } from "../lib/nav.svelte.js";

const CHANNELS = [{ id: "general", name: "General", channel_type: "group" }];

// The members multi-select is populated from the agent list the panel already
// loads for sender decoration (amendment §C: ids come from the server, never
// free-typed).
const AGENTS = [
  { id: "ada", name: "Ada", role: "Researcher", status: "healthy" },
  { id: "bob", name: "Bob", role: "Writer", status: "healthy" },
];

function historyOf(...messages) {
  return { messages };
}

// Open the collapsed form: click "New channel" and wait for the name field.
async function openForm() {
  await fireEvent.click(screen.getByRole("button", { name: /new channel/i }));
  return screen.findByRole("textbox", { name: /channel name/i });
}

beforeEach(() => {
  listAgents.mockResolvedValue(AGENTS);
  listChannels.mockResolvedValue({ channels: CHANNELS });
  getChannelHistory.mockResolvedValue(historyOf());
  createChannel.mockResolvedValue({
    id: "group:standup",
    name: "standup",
    channel_type: "group",
  });
  nav.targetChannel = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("Channel creation affordance", () => {
  it("hides the New channel affordance when the create capability is off", async () => {
    render(ChannelTimeline, { props: { userId: "local", canCreate: false } });
    await screen.findByRole("option", { name: "General" });

    expect(screen.queryByRole("button", { name: /new channel/i })).toBeNull();
  });

  it("offers the New channel affordance collapsed when the capability is on", async () => {
    render(ChannelTimeline, { props: { userId: "local", canCreate: true } });
    await screen.findByRole("option", { name: "General" });

    // The trigger is present; the form (its name field) is collapsed until opened
    // so it never crowds the newcomer's first-contact view (amendment §B).
    expect(screen.getByRole("button", { name: /new channel/i })).toBeTruthy();
    expect(
      screen.queryByRole("textbox", { name: /channel name/i }),
    ).toBeNull();
  });

  it("previews the canonical group: id read-only as the name is typed", async () => {
    render(ChannelTimeline, { props: { userId: "local", canCreate: true } });
    await screen.findByRole("option", { name: "General" });

    const nameInput = await openForm();
    await fireEvent.input(nameInput, { target: { value: "standup" } });

    // The server derives group:<name>; the client must NOT prepend it (that would
    // POST group:group:standup). The preview shows what will actually be created.
    expect(screen.getByText("group:standup")).toBeTruthy();
  });

  it("keeps create disabled until a name AND at least one member are chosen", async () => {
    render(ChannelTimeline, { props: { userId: "local", canCreate: true } });
    await screen.findByRole("option", { name: "General" });
    await openForm();

    const submit = screen.getByRole("button", { name: /create channel/i });
    expect(submit.disabled).toBe(true);

    await fireEvent.input(
      screen.getByRole("textbox", { name: /channel name/i }),
      { target: { value: "standup" } },
    );
    // Name alone is not enough — the endpoint rejects an empty members array.
    expect(submit.disabled).toBe(true);

    await fireEvent.click(await screen.findByRole("checkbox", { name: /ada/i }));
    expect(submit.disabled).toBe(false);
  });

  it("creates the channel with members and defaults the respond policy to when_mentioned", async () => {
    render(ChannelTimeline, { props: { userId: "local", canCreate: true } });
    await screen.findByRole("option", { name: "General" });
    await openForm();

    await fireEvent.input(
      screen.getByRole("textbox", { name: /channel name/i }),
      { target: { value: "standup" } },
    );
    await fireEvent.click(await screen.findByRole("checkbox", { name: /ada/i }));
    await fireEvent.click(
      screen.getByRole("button", { name: /create channel/i }),
    );

    await waitFor(() => {
      expect(createChannel).toHaveBeenCalledTimes(1);
    });
    const payload = createChannel.mock.calls[0][0];
    expect(payload.name).toBe("standup");
    // The client never prepends group: (server derives it).
    expect(payload.name).not.toMatch(/^group:/);
    // The selected persona, plus the acting user appended so they can post.
    expect(payload.members).toEqual([
      { id: "ada", respond: "when_mentioned" },
      { id: "local", respond: "never" },
    ]);
  });

  it("lists only persona agents as members (task agents are excluded)", async () => {
    // Only persona agents hold a conversation; a task agent (agents.yaml
    // type:"task") runs workflow steps and is never a discussion participant, so
    // it must not be selectable as a channel member.
    listAgents.mockResolvedValue([
      ...AGENTS,
      { id: "runner", name: "Runner", type: "task", status: "healthy" },
    ]);

    render(ChannelTimeline, { props: { userId: "local", canCreate: true } });
    await screen.findByRole("option", { name: "General" });
    await openForm();

    expect(await screen.findByRole("checkbox", { name: /ada/i })).toBeTruthy();
    expect(screen.queryByRole("checkbox", { name: /runner/i })).toBeNull();
  });

  it("adds the acting user as a member so they can post in the channel", async () => {
    // The store rejects a publish from a non-member (ErrNotMember), so a channel
    // made of only personas would leave the operator unable to post into it. The
    // create call adds the acting user with respond:"never" — present to publish,
    // never dispatched a turn like an agent.
    render(ChannelTimeline, { props: { userId: "local", canCreate: true } });
    await screen.findByRole("option", { name: "General" });
    await openForm();

    await fireEvent.input(
      screen.getByRole("textbox", { name: /channel name/i }),
      { target: { value: "standup" } },
    );
    await fireEvent.click(await screen.findByRole("checkbox", { name: /ada/i }));
    await fireEvent.click(
      screen.getByRole("button", { name: /create channel/i }),
    );

    await waitFor(() => expect(createChannel).toHaveBeenCalledTimes(1));
    const payload = createChannel.mock.calls[0][0];
    expect(payload.members).toEqual(
      expect.arrayContaining([{ id: "local", respond: "never" }]),
    );
    // The user is added for posting, never shown as a selectable member.
    expect(screen.queryByRole("checkbox", { name: /local/i })).toBeNull();
  });

  it("passes a per-member respond policy and an optional description", async () => {
    render(ChannelTimeline, { props: { userId: "local", canCreate: true } });
    await screen.findByRole("option", { name: "General" });
    await openForm();

    await fireEvent.input(
      screen.getByRole("textbox", { name: /channel name/i }),
      { target: { value: "standup" } },
    );
    await fireEvent.input(
      screen.getByRole("textbox", { name: /description/i }),
      { target: { value: "daily sync" } },
    );
    await fireEvent.click(await screen.findByRole("checkbox", { name: /ada/i }));
    await fireEvent.change(
      screen.getByRole("combobox", { name: /respond policy for ada/i }),
      { target: { value: "always" } },
    );
    await fireEvent.click(
      screen.getByRole("button", { name: /create channel/i }),
    );

    await waitFor(() => expect(createChannel).toHaveBeenCalledTimes(1));
    const payload = createChannel.mock.calls[0][0];
    expect(payload.description).toBe("daily sync");
    expect(payload.members).toEqual([
      { id: "ada", respond: "always" },
      { id: "local", respond: "never" },
    ]);
  });

  it("reloads the channel list and selects the newly-created channel", async () => {
    const NEW = { id: "group:standup", name: "standup", channel_type: "group" };
    listChannels
      .mockResolvedValueOnce({ channels: CHANNELS })
      .mockResolvedValue({ channels: [...CHANNELS, NEW] });

    render(ChannelTimeline, { props: { userId: "local", canCreate: true } });
    await screen.findByRole("option", { name: "General" });
    await openForm();

    await fireEvent.input(
      screen.getByRole("textbox", { name: /channel name/i }),
      { target: { value: "standup" } },
    );
    await fireEvent.click(await screen.findByRole("checkbox", { name: /ada/i }));
    await fireEvent.click(
      screen.getByRole("button", { name: /create channel/i }),
    );

    // On 201 the panel reuses loadChannels() (a second listChannels call) and
    // lands the operator in the channel they just made (nav.targetChannel).
    await waitFor(() =>
      expect(screen.getByRole("option", { name: "standup" })).toBeTruthy(),
    );
    expect(listChannels.mock.calls.length).toBeGreaterThanOrEqual(2);
    const picker = screen.getByRole("combobox", { name: /channel/i });
    expect(picker.value).toBe("group:standup");
    // The form collapses after a successful create.
    expect(
      screen.queryByRole("textbox", { name: /channel name/i }),
    ).toBeNull();
  });

  it("surfaces the server conflict envelope (409) and keeps the form open to retry", async () => {
    createChannel.mockRejectedValue(
      new ApiError("channel group:standup already exists", 409, {
        code: "CONFLICT",
      }),
    );

    render(ChannelTimeline, { props: { userId: "local", canCreate: true } });
    await screen.findByRole("option", { name: "General" });
    await openForm();

    await fireEvent.input(
      screen.getByRole("textbox", { name: /channel name/i }),
      { target: { value: "standup" } },
    );
    await fireEvent.click(await screen.findByRole("checkbox", { name: /ada/i }));
    await fireEvent.click(
      screen.getByRole("button", { name: /create channel/i }),
    );

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/already exists/i);
    // The form is still open so the operator can pick a different name.
    expect(
      screen.getByRole("textbox", { name: /channel name/i }),
    ).toBeTruthy();
  });
});
