import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/svelte";
import App from "./App.svelte";

// The §E tester identity-override carve-out (RFC 0048 amendment): in LOCAL
// (unauthenticated) mode the shell offers an "acting as" control — distinct from
// the /ui/context principal — so a tester can switch the effective user and
// watch per-user persistence follow the identity. Split out of App.test.js to
// keep each spec under the review-size cap; the boot/routing smoke stays there.
// The shell mounts the real channel_timeline panel, whose mount effects call the
// client, so the full api surface is stubbed (mirrors App.test.js).
vi.mock("./lib/api.js", () => ({
  ApiError: class ApiError extends Error {},
  loadBootstrap: vi.fn(),
  listAgents: vi.fn(() => Promise.resolve([])),
  sendChat: vi.fn(),
  getChatHistory: vi.fn(() => Promise.resolve({ messages: [] })),
  listSessions: vi.fn(() => Promise.resolve({ sessions: [] })),
  createSession: vi.fn(),
  listChannels: vi.fn(() => Promise.resolve({ channels: [] })),
  getChannelHistory: vi.fn(() => Promise.resolve({ messages: [] })),
  publishMessage: vi.fn(),
  getClosedInteractions: vi.fn(() => Promise.resolve({ interactions: [] })),
}));

import {
  loadBootstrap,
  listAgents,
  getChatHistory,
  getChannelHistory,
  sendChat,
} from "./lib/api.js";
import { selection } from "./lib/selection.svelte.js";

beforeEach(() => {
  window.location.hash = "";
  // The DM specs below give these four their own answers; every test starts
  // from a console with no persona and no history.
  listAgents.mockImplementation(() => Promise.resolve([]));
  getChatHistory.mockImplementation(() => Promise.resolve({ messages: [] }));
  getChannelHistory.mockImplementation(() => Promise.resolve({ messages: [] }));
  sendChat.mockReset();
  selection.dmAgent = "";
});

// One persona, and one DM per person who talks to it (`dm:ada:<user>`): the
// same stand-in for the DM lookup, the channel history and the chat turn.
function onePersonaWithDms(stored) {
  const dm = (user) => `dm:ada:${user}`;
  let n = 0;
  const row = (user, sender, content) => ({
    id: `m${++n}`,
    channel_id: dm(user),
    sender_id: sender,
    content,
    timestamp: new Date(Date.parse("2026-06-03T10:00:00Z") + n * 1000).toISOString(),
    mentions: [],
  });
  const rows = {};
  for (const [user, lines] of Object.entries(stored)) {
    rows[dm(user)] = lines.map(([sender, content]) => row(user, sender, content));
  }
  const newestFirst = (id) => ({ messages: [...(rows[id] ?? [])].reverse() });
  listAgents.mockImplementation(() =>
    Promise.resolve([{ id: "ada", name: "Ada", role: "Researcher", status: "healthy" }]),
  );
  getChatHistory.mockImplementation((agent, { userId }) =>
    Promise.resolve(newestFirst(dm(userId))),
  );
  getChannelHistory.mockImplementation((id) => Promise.resolve(newestFirst(id)));
  sendChat.mockImplementation((agent, { message, userId }) => {
    (rows[dm(userId)] ??= []).push(
      row(userId, userId, message),
      row(userId, agent, `reply to ${userId}`),
    );
    return Promise.resolve({ reply: `reply to ${userId}` });
  });
}

afterEach(() => {
  // Unmount between tests. Vitest runs without `globals: true`, so
  // @testing-library/svelte cannot auto-register its afterEach(cleanup) — left
  // implicit, each render() leaks into jsdom and queries resolve against a
  // prior test's DOM, making the suite order-dependent.
  cleanup();
  vi.clearAllMocks();
});

describe("App shell — §E tester identity override", () => {
  it("surfaces the real principal verbatim and offers the §E testing override in local mode", async () => {
    loadBootstrap.mockResolvedValue({
      config: { panels: { channel_timeline: { enabled: true, available: true } } },
      context: { principal: "local", tenant: "local", authenticated: false },
    });

    const { container } = render(App);

    // The topbar surfaces the principal coming from /ui/context (titled so the
    // source is unambiguous), shown verbatim and never replaced by the override.
    await waitFor(() => {
      const principal = screen.getByTitle("Identity from /api/v1/ui/context");
      expect(principal.textContent.trim()).toBe("local");
    });
    // RFC §F rule 1: identity comes from /ui/context, so there is still NO
    // user_id field — the panels never prompt for the identity itself.
    expect(container.querySelector('input[name="user_id"]')).toBeNull();
    // §E carve-out: in LOCAL (unauthenticated) mode the shell offers a clearly
    // distinct "acting as" testing override, defaulting to the principal, so a
    // tester can demonstrate per-user persistence. It is a separate control, not
    // the identity source.
    const override = container.querySelector('input[name="acting_as"]');
    expect(override).not.toBeNull();
    expect(override.value).toBe("local");
    // The placeholder echoes the principal so that, once a tester clears the
    // box, the field visibly communicates "empty ⇒ acting as the principal"
    // rather than reading as no identity at all.
    expect(override.getAttribute("placeholder")).toBe("local");
  });

  it("hides the §E identity override once the principal is authenticated", async () => {
    // The carve-out is local-only: once /ui/context reports an authenticated
    // principal (RFC 0039), the override disappears so a real identity can never
    // be masked from the browser.
    loadBootstrap.mockResolvedValue({
      config: { panels: { channel_timeline: { enabled: true, available: true } } },
      context: { principal: "alice@example.com", tenant: "t", authenticated: true },
    });

    const { container } = render(App);

    await waitFor(() => {
      expect(
        screen.getByTitle("Identity from /api/v1/ui/context").textContent.trim(),
      ).toBe("alice@example.com");
    });
    expect(container.querySelector('input[name="acting_as"]')).toBeNull();
  });

  it("commits the override on change (not per-keystroke) so the effective identity follows it", async () => {
    // Editing "acting as" changes the effective user threaded to the panels —
    // the mechanism that lets a tester switch users and watch the persona's
    // memory follow the identity (§E). The commit is deferred to `change`
    // (blur/Enter), not every keystroke: a DM is keyed on (user, persona), and
    // the conversation panel opens an open DM again for the new identity (the
    // DM specs below), so committing per-keystroke would blank and refetch the
    // transcript for each intermediate value ("b", "bo", "bob"). The contract
    // here is the observable side of that: typing alone must NOT move the
    // effective identity; the change does.
    loadBootstrap.mockResolvedValue({
      config: { panels: { channel_timeline: { enabled: true, available: true } } },
      context: { principal: "local", tenant: "local", authenticated: false },
    });

    const { container } = render(App);

    const override = await waitFor(() => {
      const el = container.querySelector('input[name="acting_as"]');
      expect(el).not.toBeNull();
      return el;
    });

    const effectiveIds = () =>
      [...container.querySelectorAll("code")].map((c) => c.textContent.trim());

    // The conversation panel echoes the effective identity it acts as; it starts
    // at the principal.
    await waitFor(() => expect(effectiveIds()).toContain("local"));

    // Typing into the box edits the draft but must not yet move the effective
    // identity — no premature reseed.
    await fireEvent.input(override, { target: { value: "bob" } });
    expect(effectiveIds()).not.toContain("bob");
    expect(effectiveIds()).toContain("local");

    // Committing (blur/Enter ⇒ `change`) threads the new identity to the panels.
    await fireEvent.change(override, { target: { value: "bob" } });
    await waitFor(() => expect(effectiveIds()).toContain("bob"));

    // Clearing the override and committing falls back to the real principal —
    // the empty box means "act as the principal", never "no identity".
    await fireEvent.input(override, { target: { value: "" } });
    await fireEvent.change(override, { target: { value: "" } });
    await waitFor(() => {
      expect(effectiveIds()).toContain("local");
      expect(effectiveIds()).not.toContain("bob");
    });
  });

  it("opens an open DM again as the new identity, and sends as it (ISSUE-0226)", async () => {
    // The three steps of the issue, through the shell's own control: open a DM
    // as one identity, change "acting as", send. The DM on screen and the DM
    // the message goes to must be the same one: the new identity's.
    onePersonaWithDms({ local: [["local", "hello from local"], ["ada", "hi local"]] });
    loadBootstrap.mockResolvedValue({
      config: { panels: { channel_timeline: { enabled: true, available: true } } },
      context: { principal: "local", tenant: "local", authenticated: false },
    });

    const { container } = render(App);
    await fireEvent.click(await screen.findByRole("option", { name: /^Ada\b/ }));
    await screen.findByText("hello from local");

    const override = container.querySelector('input[name="acting_as"]');
    await fireEvent.input(override, { target: { value: "bob" } });
    // Typing alone opens nothing again (the commit is on `change`).
    expect(getChatHistory).toHaveBeenCalledTimes(1);
    await fireEvent.change(override, { target: { value: "bob" } });

    // local's conversation is gone from the screen, and bob's (none yet) is
    // what was looked up.
    await waitFor(() =>
      expect(getChatHistory).toHaveBeenLastCalledWith("ada", { userId: "bob" }),
    );
    expect(screen.queryByText("hello from local")).toBeNull();
    expect(screen.queryByText("hi local")).toBeNull();

    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "who am I?" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /^send/i }));

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1));
    expect(sendChat.mock.calls[0][1]).toMatchObject({ userId: "bob", message: "who am I?" });
    // The reply shows, in bob's DM, and nothing of local's with it.
    expect(await screen.findByText("reply to bob")).toBeTruthy();
    expect(getChannelHistory).toHaveBeenLastCalledWith("dm:ada:bob", expect.anything());
    expect(screen.queryByText("hello from local")).toBeNull();

    // Back to the principal: local's conversation again, bob's gone.
    await fireEvent.input(override, { target: { value: "" } });
    await fireEvent.change(override, { target: { value: "" } });
    expect(await screen.findByText("hello from local")).toBeTruthy();
    expect(screen.queryByText("reply to bob")).toBeNull();
  });

  it("opens a signed-in person's DM once, as that person", async () => {
    // Signed in, the identity is the verified principal and cannot change from
    // the browser: there is no override to trigger a second lookup.
    onePersonaWithDms({ "alice@example.com": [["ada", "hi alice"]] });
    loadBootstrap.mockResolvedValue({
      config: { panels: { channel_timeline: { enabled: true, available: true } } },
      context: { principal: "alice@example.com", tenant: "t", authenticated: true },
    });

    const { container } = render(App);
    await fireEvent.click(await screen.findByRole("option", { name: /^Ada\b/ }));

    expect(await screen.findByText("hi alice")).toBeTruthy();
    expect(container.querySelector('input[name="acting_as"]')).toBeNull();
    expect(getChatHistory).toHaveBeenCalledTimes(1);
    expect(getChatHistory).toHaveBeenCalledWith("ada", { userId: "alice@example.com" });
  });
});
