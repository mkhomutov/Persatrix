import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  within,
  cleanup,
  fireEvent,
} from "@testing-library/svelte";
import ChannelTimeline from "./ChannelTimeline.svelte";

// Changing who the console acts as while a conversation is open (ISSUE-0226).
// The shell's "acting as" control (local testing only) changes the panel's
// `userId`. A DM belongs to one person and one persona, so an open DM is
// opened again for the new identity, and nothing of the old identity's
// conversation — its history, a reply still on its way, an error — reaches the
// new one's view. A group channel is the same channel whoever watches it.
// The backend client is replaced by a stand-in that keeps one DM per person.
vi.mock("../lib/api.js", () => ({
  ApiError: class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  },
  listAgents: vi.fn(),
  listChannels: vi.fn(),
  getChannelHistory: vi.fn(),
  getChatHistory: vi.fn(),
  sendChat: vi.fn(),
  publishMessage: vi.fn(),
  getClosedInteractions: vi.fn(() => Promise.resolve({ interactions: [] })),
  listSessions: vi.fn(() => Promise.resolve({ sessions: [] })),
  createSession: vi.fn(),
}));

import {
  listAgents,
  listChannels,
  getChannelHistory,
  getChatHistory,
  sendChat,
  listSessions,
  ApiError,
} from "../lib/api.js";
import { selection } from "../lib/selection.svelte.js";

const AGENTS = [{ id: "ada", name: "Ada", role: "Researcher", status: "healthy" }];
const BEA = { id: "bea", name: "Bea", role: "Writer", status: "healthy" };
const CHANNELS = [{ id: "general", name: "General", channel_type: "group" }];
const dmOf = (user) => `dm:ada:${user}`;

let clock;
function msg(channel, sender, content) {
  clock += 1000;
  return {
    id: `m${clock}`,
    channel_id: channel,
    sender_id: sender,
    content,
    timestamp: new Date(clock).toISOString(),
    mentions: [],
  };
}

// The stand-in's store: channel id → messages, oldest first.
let store;
const say = (channel, sender, content) =>
  (store[channel] ??= []).push(msg(channel, sender, content));
const newestFirst = (channel) => ({ messages: [...(store[channel] ?? [])].reverse() });

// A promise a test settles by hand, to hold one answer back.
function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function texts() {
  return screen
    .queryAllByRole("listitem")
    .map((li) => li.querySelector(".content").textContent);
}

async function openDm(view) {
  await screen.findByRole("option", { name: "General" });
  await fireEvent.click(screen.getByRole("option", { name: /^Ada\b/ }));
  await screen.findByRole("heading", { name: "Ada" });
  return view;
}

const box = () => screen.getByRole("textbox", { name: /message/i });
const type = (text) => fireEvent.input(box(), { target: { value: text } });
const send = () => fireEvent.click(screen.getByRole("button", { name: /^send/i }));

beforeEach(() => {
  clock = Date.parse("2026-06-03T10:00:00Z");
  store = {};
  listAgents.mockResolvedValue(AGENTS);
  listChannels.mockResolvedValue({ channels: CHANNELS });
  getChannelHistory.mockImplementation((id) => Promise.resolve(newestFirst(id)));
  // A held answer a test queued and the panel never asked for must not reach
  // the next test: start these two from nothing.
  getChatHistory.mockReset();
  sendChat.mockReset();
  listSessions.mockReset();
  listSessions.mockImplementation(() => Promise.resolve({ sessions: [] }));
  // The DM is looked up for one person and one persona, without creating it.
  getChatHistory.mockImplementation((agent, { userId }) =>
    Promise.resolve(newestFirst(dmOf(userId))),
  );
  // A turn stores the person's message and the persona's reply in their DM.
  sendChat.mockImplementation((agent, { message, userId }) => {
    say(dmOf(userId), userId, message);
    say(dmOf(userId), agent, `reply to ${userId}`);
    return Promise.resolve({ reply: `reply to ${userId}` });
  });
  selection.dmAgent = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Channels panel — the identity changes while a DM is open", () => {
  it("shows nothing of the old identity's DM while the new one's opens, then the new one's", async () => {
    say(dmOf("local"), "local", "hello from local");
    say(dmOf("local"), "ada", "hi local");
    say(dmOf("bob"), "bob", "hello from bob");
    const view = await openDm(render(ChannelTimeline, { props: { userId: "local" } }));
    await waitFor(() => expect(texts()).toEqual(["hello from local", "hi local"]));

    // Hold the new identity's lookup back, to look at the moment in between.
    const lookup = deferred();
    getChatHistory.mockImplementationOnce(() => lookup.promise);
    await view.rerender({ userId: "bob" });

    expect(getChatHistory).toHaveBeenLastCalledWith("ada", { userId: "bob" });
    expect(texts()).toEqual([]);
    expect(screen.queryByText(/hello from local|hi local/)).toBeNull();
    expect(screen.getByText("Opening conversation…")).toBeTruthy();

    lookup.resolve(newestFirst(dmOf("bob")));
    await waitFor(() => expect(texts()).toEqual(["hello from bob"]));
    expect(screen.queryByText("Opening conversation…")).toBeNull();
    expect(getChannelHistory).toHaveBeenLastCalledWith(dmOf("bob"), expect.anything());
  });

  it("sends as the new identity into the new identity's DM, and shows the reply there", async () => {
    // The issue's three steps: open a DM, change who you act as, send.
    say(dmOf("local"), "local", "hello from local");
    say(dmOf("local"), "ada", "hi local");
    const view = await openDm(render(ChannelTimeline, { props: { userId: "local" } }));
    await waitFor(() => expect(texts()).toHaveLength(2));

    await view.rerender({ userId: "bob" });
    await waitFor(() => expect(getChatHistory).toHaveBeenLastCalledWith("ada", { userId: "bob" }));
    await type("who am I?");
    await send();

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1));
    expect(sendChat.mock.calls[0][1]).toMatchObject({ userId: "bob", message: "who am I?" });
    // The turn and its reply are bob's, read from bob's DM; local's are not here.
    await waitFor(() => expect(texts()).toEqual(["who am I?", "reply to bob"]));
    expect(getChannelHistory).toHaveBeenLastCalledWith(dmOf("bob"), expect.anything());
  });

  it("opens the first identity's DM again when the identity is changed back", async () => {
    say(dmOf("local"), "local", "hello from local");
    say(dmOf("bob"), "bob", "hello from bob");
    const view = await openDm(render(ChannelTimeline, { props: { userId: "local" } }));
    await waitFor(() => expect(texts()).toEqual(["hello from local"]));

    await view.rerender({ userId: "bob" });
    await waitFor(() => expect(texts()).toEqual(["hello from bob"]));
    await view.rerender({ userId: "local" });
    await waitFor(() => expect(texts()).toEqual(["hello from local"]));

    expect(getChatHistory.mock.calls.map(([, opts]) => opts.userId)).toEqual([
      "local",
      "bob",
      "local",
    ]);
  });

  it("drops a lookup answered for the old identity after the change", async () => {
    say(dmOf("local"), "local", "hello from local");
    say(dmOf("bob"), "bob", "hello from bob");
    // The first lookup (as local) is still out when the identity changes.
    const first = deferred();
    getChatHistory.mockImplementationOnce(() => first.promise);
    const view = await openDm(render(ChannelTimeline, { props: { userId: "local" } }));

    await view.rerender({ userId: "bob" });
    await waitFor(() => expect(texts()).toEqual(["hello from bob"]));

    first.resolve(newestFirst(dmOf("local")));
    await first.promise;
    await new Promise((r) => setTimeout(r, 0));

    expect(texts()).toEqual(["hello from bob"]);
    expect(getChannelHistory).not.toHaveBeenCalledWith(dmOf("local"), expect.anything());
  });

  it("drops a failed lookup of the old identity that answers after the change", async () => {
    say(dmOf("bob"), "bob", "hello from bob");
    const first = deferred();
    getChatHistory.mockImplementationOnce(() => first.promise);
    const view = await openDm(render(ChannelTimeline, { props: { userId: "local" } }));

    await view.rerender({ userId: "bob" });
    await waitFor(() => expect(texts()).toEqual(["hello from bob"]));

    first.reject(new ApiError("backend down", 503));
    await first.promise.catch(() => {});
    await new Promise((r) => setTimeout(r, 0));

    // The failure was the old identity's: it says nothing under the new one
    // and does not close the new one's conversation.
    expect(screen.queryByText(/Could not open the conversation/)).toBeNull();
    expect(texts()).toEqual(["hello from bob"]);
  });

  it("leaves the session and epoch overrides as they are", async () => {
    // They are the tester's choice of scope, like the draft: not the old
    // identity's property. (Without a session registry the session is a
    // free-text box, which a test can fill like the epoch.)
    listSessions.mockImplementation(() => Promise.reject(new ApiError("not wired", 503)));
    const view = await openDm(render(ChannelTimeline, { props: { userId: "local" } }));
    const epoch = () => screen.getByRole("textbox", { name: /epoch/i });
    const session = () => screen.getByRole("textbox", { name: /session id/i });
    await screen.findByRole("textbox", { name: /session id/i });
    await fireEvent.input(epoch(), { target: { value: "ep-1" } });
    await fireEvent.input(session(), { target: { value: "sess-1" } });

    await view.rerender({ userId: "bob" });
    await waitFor(() => expect(getChatHistory).toHaveBeenLastCalledWith("ada", { userId: "bob" }));

    expect(epoch().value).toBe("ep-1");
    expect(session().value).toBe("sess-1");
    await type("scoped");
    await send();
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1));
    expect(sendChat.mock.calls[0][1]).toMatchObject({
      userId: "bob",
      epochId: "ep-1",
      sessionId: "sess-1",
    });
  });

  it("keeps the draft parked for another persona", async () => {
    listAgents.mockResolvedValue([...AGENTS, BEA]);
    const view = await openDm(render(ChannelTimeline, { props: { userId: "local" } }));
    await type("for Ada, later");
    await fireEvent.click(screen.getByRole("option", { name: /^Bea\b/ }));
    await screen.findByRole("heading", { name: "Bea" });

    await view.rerender({ userId: "bob" });
    await waitFor(() => expect(getChatHistory).toHaveBeenLastCalledWith("bea", { userId: "bob" }));

    await fireEvent.click(screen.getByRole("option", { name: /^Ada\b/ }));
    await screen.findByRole("heading", { name: "Ada" });
    expect(box().value).toBe("for Ada, later");
  });

  it("keeps an unsent draft in the box, and sends it as the new identity", async () => {
    // The draft is what the person at the keyboard typed and has not said; the
    // identity they act as does not own it.
    const view = await openDm(render(ChannelTimeline, { props: { userId: "local" } }));
    await type("half a thought");

    await view.rerender({ userId: "bob" });
    await waitFor(() => expect(getChatHistory).toHaveBeenLastCalledWith("ada", { userId: "bob" }));

    expect(box().value).toBe("half a thought");
    await send();
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1));
    expect(sendChat.mock.calls[0][1]).toMatchObject({ userId: "bob", message: "half a thought" });
  });
});

describe("Channels panel — a reply still on its way for the old identity", () => {
  // Send as local and hold the answer; returns the handle to settle it.
  async function sendAsLocalAndHold(view) {
    const turn = deferred();
    sendChat.mockImplementationOnce(() => turn.promise);
    await type("for local");
    await send();
    await screen.findByText(/ada is thinking/i);
    return turn;
  }

  it("frees the new identity's view at once and leaves the request to finish", async () => {
    say(dmOf("bob"), "bob", "hello from bob");
    const view = await openDm(render(ChannelTimeline, { props: { userId: "local" } }));
    const turn = await sendAsLocalAndHold(view);

    await view.rerender({ userId: "bob" });
    await waitFor(() => expect(texts()).toEqual(["hello from bob"]));

    // Nothing of local's turn shows under bob: no "thinking" line, no Cancel,
    // a Send button that is not busy, a box that can be typed in.
    expect(screen.queryByText(/ada is thinking/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /cancel/i })).toBeNull();
    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
    expect(box().disabled).toBe(false);
    // The request itself was not cancelled: local's conversation gets its reply.
    expect(sendChat.mock.calls[0][1].signal.aborted).toBe(false);
    // The box is left as it was, as after a Cancel.
    expect(box().value).toBe("for local");

    await type("typed as bob");
    turn.resolve({ reply: "reply to local" });
    await turn.promise;
    await new Promise((r) => setTimeout(r, 0));

    // The late answer changes nothing here: not the box, not the rows.
    expect(box().value).toBe("typed as bob");
    expect(texts()).toEqual(["hello from bob"]);
    expect(screen.queryByText(/waiting for you/i)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows no error under the new identity when that request fails", async () => {
    const view = await openDm(render(ChannelTimeline, { props: { userId: "local" } }));
    const turn = await sendAsLocalAndHold(view);

    await view.rerender({ userId: "bob" });
    await waitFor(() => expect(getChatHistory).toHaveBeenLastCalledWith("ada", { userId: "bob" }));
    turn.reject(new ApiError("agent did not respond in time", 504));
    await turn.promise.catch(() => {});
    await new Promise((r) => setTimeout(r, 0));

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("does not let the old identity's answer end the new identity's own turn", async () => {
    const view = await openDm(render(ChannelTimeline, { props: { userId: "local" } }));
    const localTurn = await sendAsLocalAndHold(view);

    await view.rerender({ userId: "bob" });
    await waitFor(() => expect(getChatHistory).toHaveBeenLastCalledWith("ada", { userId: "bob" }));
    const bobTurn = deferred();
    sendChat.mockImplementationOnce(() => bobTurn.promise);
    await type("for bob");
    await send();
    await screen.findByText(/ada is thinking/i);

    localTurn.resolve({ reply: "reply to local" });
    await localTurn.promise;
    await new Promise((r) => setTimeout(r, 0));

    // Bob's turn is still out: its "thinking" line, its Cancel, its busy Send.
    expect(screen.getByText(/ada is thinking/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /cancel/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /sending/i })).toBeTruthy();
    expect(box().value).toBe("for bob");

    say(dmOf("bob"), "bob", "for bob");
    say(dmOf("bob"), "ada", "reply to bob");
    bobTurn.resolve({ reply: "reply to bob" });
    await waitFor(() => expect(texts()).toEqual(["for bob", "reply to bob"]));
    expect(screen.queryByText(/ada is thinking/i)).toBeNull();
    expect(box().value).toBe("");
  });

  it("keeps a draft parked meanwhile when the old identity's answer arrives", async () => {
    listAgents.mockResolvedValue([...AGENTS, BEA]);
    const view = await openDm(render(ChannelTimeline, { props: { userId: "local" } }));
    const turn = await sendAsLocalAndHold(view);

    await view.rerender({ userId: "bob" });
    await waitFor(() => expect(getChatHistory).toHaveBeenLastCalledWith("ada", { userId: "bob" }));
    // As bob: a new draft for Ada, parked by opening another persona's DM.
    await type("bob's half thought");
    await fireEvent.click(screen.getByRole("option", { name: /^Bea\b/ }));
    await screen.findByRole("heading", { name: "Bea" });

    turn.resolve({ reply: "reply to local" });
    await turn.promise;
    await new Promise((r) => setTimeout(r, 0));

    // A turn that ends normally drops the draft parked for its persona,
    // because that draft is the message it sent. This one was cut loose: the
    // parked draft is bob's, and stays.
    await fireEvent.click(screen.getByRole("option", { name: /^Ada\b/ }));
    await screen.findByRole("heading", { name: "Ada" });
    expect(box().value).toBe("bob's half thought");
  });

  it("takes an error of the old identity's send off the screen", async () => {
    sendChat.mockImplementationOnce(() =>
      Promise.reject(new ApiError("agent did not respond in time", 504)),
    );
    const view = await openDm(render(ChannelTimeline, { props: { userId: "local" } }));
    await type("for local");
    await send();
    expect((await screen.findByRole("alert")).textContent).toMatch(/did not respond/);

    await view.rerender({ userId: "bob" });

    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });
});

describe("Channels panel — the identity changes while a group channel is watched", () => {
  it("leaves the channel's history as it is and only renames whose messages are 'You'", async () => {
    say("general", "local", "posted as local");
    say("general", "ada", "an answer");
    const view = render(ChannelTimeline, { props: { userId: "local" } });
    const timeline = await screen.findByRole("list", { name: /channel messages/i });
    await waitFor(() => expect(texts()).toEqual(["posted as local", "an answer"]));
    expect(within(timeline).getByText("You")).toBeTruthy();
    const reads = getChannelHistory.mock.calls.length;

    await view.rerender({ userId: "bob" });

    // The same rows, not loaded again; the post by `local` is no longer "You".
    expect(texts()).toEqual(["posted as local", "an answer"]);
    expect(getChannelHistory.mock.calls.length).toBe(reads);
    expect(within(timeline).queryByText("You")).toBeNull();
    expect(within(timeline).getByText("local")).toBeTruthy();
    // No DM is open, so none is looked up.
    expect(getChatHistory).not.toHaveBeenCalled();
  });
});
