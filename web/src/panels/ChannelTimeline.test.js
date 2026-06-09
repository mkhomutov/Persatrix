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

// The channel-timeline panel renders over today's channel API (RFC 0048 PR 5):
// it lists channels, shows a channel's history newest-first, keeps it live by
// polling (visibility-pause + error-backoff + head-poll de-dupe), and offers an
// optional human publish. The backend client is mocked so the panel's wiring is
// exercised without a running orchestrator or real timers.
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
  getChatHistory: vi.fn(),
  sendChat: vi.fn(),
  publishMessage: vi.fn(),
  getClosedInteractions: vi.fn(() => Promise.resolve({ interactions: [] })),
}));

import {
  listAgents,
  listChannels,
  getChannelHistory,
  getChatHistory,
  publishMessage,
  ApiError,
} from "../lib/api.js";
import { selection } from "../lib/selection.svelte.js";

const CHANNELS = [
  { id: "general", name: "General", channel_type: "group" },
  { id: "ops", name: "Ops", channel_type: "group" },
];

function msg(id, content, sender = "alice", ts = "2026-06-02T10:00:00Z") {
  return {
    id,
    channel_id: "general",
    sender_id: sender,
    content,
    timestamp: ts,
    mentions: [],
  };
}

// The history endpoint returns newest-first (sqlite_messages.go ORDER BY
// timestamp DESC), so fixtures mirror that ordering.
function historyOf(...messages) {
  return { messages };
}

beforeEach(() => {
  // Sender-name decoration is best-effort; default it to a resolved empty list
  // so the timeline renders raw ids unless a test opts into agent fixtures.
  listAgents.mockResolvedValue([]);
  listChannels.mockResolvedValue({ channels: CHANNELS });
  getChannelHistory.mockResolvedValue(
    historyOf(msg("m2", "second"), msg("m1", "first")),
  );
  // DM resolution defaults to an empty conversation; DM-specific behaviour lives
  // in ChannelTimeline.dm.test.js.
  getChatHistory.mockResolvedValue(historyOf());
  publishMessage.mockResolvedValue(
    msg("m3", "from me", "local", "2026-06-02T10:00:03Z"),
  );
  // Reset the rehomed sticky DM selection (amendment §B) so a DM opened by one
  // test doesn't auto-resume in the next (module-level $state outlives a mount).
  selection.dmAgent = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("Channel timeline panel", () => {
  it("populates the channel picker from GET /api/v1/channels on mount", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "General" })).toBeTruthy();
    });
    expect(screen.getByRole("option", { name: "Ops" })).toBeTruthy();
    expect(listChannels).toHaveBeenCalledOnce();
  });

  it("renders the channel's history conversational (oldest-top, newest-bottom)", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });

    // The wire is newest-first (m2/"second" before m1/"first"); the panel
    // renders it conversational — oldest at the top, newest at the bottom
    // (RFC 0048 amendment §D).
    const items = await screen.findAllByRole("listitem");
    expect(items[0].textContent).toMatch(/first/);
    expect(items[1].textContent).toMatch(/second/);
  });

  it("maps sender ids to names and marks the operator's own posts as 'You'", async () => {
    // The agent list decorates senders (RFC 0048 §A/§D): an agent id resolves to
    // "name — role", the operator's own id reads as "You", and an unknown id
    // falls back to its raw value.
    listAgents.mockResolvedValue([
      { id: "ada", name: "Ada", role: "Researcher", status: "healthy" },
    ]);
    getChannelHistory.mockResolvedValue(
      historyOf(
        msg("m3", "from the human", "local", "2026-06-02T10:00:02Z"),
        msg("m2", "from ada", "ada", "2026-06-02T10:00:01Z"),
        msg("m1", "from a stranger", "ghost", "2026-06-02T10:00:00Z"),
      ),
    );

    render(ChannelTimeline, { props: { userId: "local" } });

    // Scope to the timeline list: the persona picker also renders "Ada —
    // Researcher" as an option (the DM entry point), so assert on the message
    // rows specifically — which also waits for the history load to settle.
    const timeline = await screen.findByRole("list", {
      name: /channel messages/i,
    });
    const rows = within(timeline);
    expect(rows.getByText("Ada — Researcher")).toBeTruthy();
    expect(rows.getByText("You")).toBeTruthy();
    expect(rows.getByText("ghost")).toBeTruthy();
  });

  it("renders a human-readable timestamp but keeps the raw value machine-readable", async () => {
    // The wire timestamp is RFC-3339 UTC (e.g. 2026-06-02T10:00:00Z) — readable
    // by a machine, not by an operator scanning the timeline. The visible text
    // is formatted for a human, while the <time> element keeps the raw value in
    // its `datetime` attribute so it stays machine-parseable.
    getChannelHistory.mockResolvedValue(
      historyOf(msg("only", "hello", "alice", "2026-06-02T10:00:00Z")),
    );

    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByText(/hello/);

    const timeEl = document.querySelector("time.ts");
    expect(timeEl.getAttribute("datetime")).toBe("2026-06-02T10:00:00Z");
    // Formatted for display: not the raw ISO string, and without its `T`
    // date/time separator. The exact wording is locale/zone dependent, so the
    // assertion is on what it must NOT be rather than an exact string.
    expect(timeEl.textContent).not.toBe("2026-06-02T10:00:00Z");
    expect(timeEl.textContent).not.toMatch(/\dT\d/);
  });

  it("shows the merged onboarding state when neither personas nor channels exist", async () => {
    // With the consolidated panel a fresh stack is a dead end only when BOTH
    // entry points are empty (amendment §D); listAgents already defaults to [].
    listChannels.mockResolvedValue({ channels: [] });

    render(ChannelTimeline, { props: { userId: "local" } });

    expect(await screen.findByText(/no personas or channels/i)).toBeTruthy();
  });

  // The merged onboarding on-ramp (§D) and in-panel create-selection (§C) live in
  // ChannelTimeline.crosspanel.test.js; the DM entry point lives in
  // ChannelTimeline.dm.test.js — split out to keep each spec under the review cap.

  it("shows a no-messages state for an empty channel", async () => {
    getChannelHistory.mockResolvedValue(historyOf());

    render(ChannelTimeline, { props: { userId: "local" } });

    await screen.findByRole("option", { name: "General" });
    expect(await screen.findByText(/no messages/i)).toBeTruthy();
  });

  it("surfaces a channel-load failure and retries", async () => {
    listChannels.mockRejectedValueOnce(new ApiError("backend down", 503));

    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("alert");

    await fireEvent.click(screen.getByRole("button", { name: /retry/i }));

    await screen.findByRole("option", { name: "General" });
    expect(listChannels).toHaveBeenCalledTimes(2);
  });

  it("surfaces a history-load failure and retries it", async () => {
    // The channel list loads, but the selected channel's initial history fetch
    // fails. The poll loop only arms on a successful load, so without a Retry
    // the error is a dead end — re-selecting the same channel fires no onchange,
    // leaving a single-channel console stuck until reload. The Retry re-runs the
    // load for the still-selected channel (mockRejectedValueOnce falls back to
    // the beforeEach success on the second call).
    getChannelHistory.mockRejectedValueOnce(new ApiError("history down", 503));

    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });
    await screen.findByRole("alert");

    await fireEvent.click(screen.getByRole("button", { name: /retry/i }));

    expect(await screen.findByText(/second/)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("acts as the context principal for publish and offers no free-text user field", async () => {
    const { container } = render(ChannelTimeline, {
      props: { userId: "local" },
    });
    await screen.findByRole("option", { name: "General" });

    // RFC §F rule 1: the publish sender is the /ui/context principal threaded in
    // as a prop; the panel must never expose a sender/user textbox to type into.
    expect(screen.queryByRole("textbox", { name: /sender|user/i })).toBeNull();
    expect(container.querySelector('input[name="sender_id"]')).toBeNull();
    expect(container.querySelector('input[name="user_id"]')).toBeNull();
  });

  it("disables publish until a message is entered", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });

    const publishButton = screen.getByRole("button", { name: /post/i });
    expect(publishButton.disabled).toBe(true);

    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "hello channel" },
    });
    expect(publishButton.disabled).toBe(false);
  });

  it("publishes a message as the context principal and echoes it immediately", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });

    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "from me" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /post/i }));

    await waitFor(() => {
      expect(publishMessage).toHaveBeenCalledWith("general", {
        senderId: "local",
        content: "from me",
      });
    });
    // The stored message the publish returns appears immediately, without
    // waiting for the next poll tick.
    expect(await screen.findByText(/from me/)).toBeTruthy();
  });

  it("surfaces a publish error envelope without crashing the panel", async () => {
    publishMessage.mockRejectedValue(
      new ApiError("content is required", 400, { code: "BAD_REQUEST" }),
    );

    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });
    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "x" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /post/i }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/content is required/i);
    // The composer is still usable.
    expect(screen.getByRole("textbox", { name: /message/i })).toBeTruthy();
  });

  it("polls for new messages and appends them, de-duping by id", async () => {
    vi.useFakeTimers();
    render(ChannelTimeline, { props: { userId: "local" } });

    // Let the initial history load settle (m2, m1).
    await vi.waitFor(() => expect(getChannelHistory).toHaveBeenCalled());
    const initialCalls = getChannelHistory.mock.calls.length;

    // The next poll returns the head with a brand-new message (m3) plus the
    // already-seen ones — the panel must append only m3 and not duplicate m2/m1.
    getChannelHistory.mockResolvedValue(
      historyOf(
        msg("m3", "third", "alice", "2026-06-02T10:00:03Z"),
        msg("m2", "second"),
        msg("m1", "first"),
      ),
    );

    await vi.advanceTimersByTimeAsync(3000);

    expect(getChannelHistory.mock.calls.length).toBeGreaterThan(initialCalls);
    await vi.waitFor(() => {
      const items = screen.getAllByRole("listitem");
      // m3 is the newest, so in conversational (oldest-top) render it lands at
      // the BOTTOM; no duplicate of the already-seen m2/m1.
      expect(items.length).toBe(3);
      expect(items[2].textContent).toMatch(/third/);
      expect(items[0].textContent).toMatch(/first/);
    });
    // The head poll passes a limit (does not re-fetch unbounded history).
    const lastCall = getChannelHistory.mock.calls.at(-1);
    expect(lastCall[1]).toMatchObject({ limit: expect.any(Number) });
  });

  it("pauses polling while the tab is backgrounded and resumes when visible", async () => {
    vi.useFakeTimers();
    const hiddenSpy = vi.spyOn(document, "hidden", "get").mockReturnValue(false);

    render(ChannelTimeline, { props: { userId: "local" } });
    await vi.waitFor(() => expect(getChannelHistory).toHaveBeenCalled());
    const callsBeforeHide = getChannelHistory.mock.calls.length;

    // Background the tab: no poll should fire across multiple intervals.
    hiddenSpy.mockReturnValue(true);
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(9000);
    expect(getChannelHistory.mock.calls.length).toBe(callsBeforeHide);

    // Foreground again: polling resumes.
    hiddenSpy.mockReturnValue(false);
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.waitFor(() =>
      expect(getChannelHistory.mock.calls.length).toBeGreaterThan(
        callsBeforeHide,
      ),
    );
  });

  it("backs off exponentially after a poll error", async () => {
    vi.useFakeTimers();
    render(ChannelTimeline, { props: { userId: "local" } });
    await vi.waitFor(() => expect(getChannelHistory).toHaveBeenCalled());

    // Every poll now fails. After the first failure the next attempt must be
    // scheduled further out than the base interval (exponential backoff), so an
    // idle/erroring tab doesn't hammer the unauthenticated localhost surface.
    getChannelHistory.mockRejectedValue(new ApiError("boom", 503));

    // First poll at the base 3s interval -> fails, schedules a backed-off retry.
    await vi.advanceTimersByTimeAsync(3000);
    const afterFirstFailure = getChannelHistory.mock.calls.length;

    // A second base interval is NOT enough to trigger the next attempt: the
    // backoff pushed it past 3s. Nothing new fires yet.
    await vi.advanceTimersByTimeAsync(3000);
    expect(getChannelHistory.mock.calls.length).toBe(afterFirstFailure);

    // Advancing further crosses the backed-off delay and the retry fires.
    await vi.advanceTimersByTimeAsync(6000);
    expect(getChannelHistory.mock.calls.length).toBeGreaterThan(
      afterFirstFailure,
    );
  });

  it("reloads history and restarts polling when the channel is switched", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });
    // Initial history for the default channel.
    await screen.findByText(/second/);

    getChannelHistory.mockResolvedValue(
      historyOf(msg("o1", "ops message", "carol")),
    );
    await fireEvent.change(screen.getByRole("combobox", { name: /channel/i }), {
      target: { value: "ops" },
    });

    // The new channel's history replaces the old, and the fetch targets it.
    expect(await screen.findByText(/ops message/)).toBeTruthy();
    await waitFor(() => {
      const lastCall = getChannelHistory.mock.calls.at(-1);
      expect(lastCall[0]).toBe("ops");
    });
    expect(screen.queryByText(/second/)).toBeNull();
  });

  it("drops a publish echo when the operator switches channel mid-flight", async () => {
    // The channel <select> is not disabled while a publish is in flight, so the
    // operator can switch channels before the POST resolves. The publish targets
    // the channel it was issued against; its echo must not leak into — nor seed a
    // seen-id against — the channel now selected. This mirrors the loadToken
    // guard poll()/loadHistory() already apply to their own resolutions.
    let resolvePublish;
    publishMessage.mockReturnValue(
      new Promise((resolve) => {
        resolvePublish = resolve;
      }),
    );

    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByText(/second/); // general's history loaded

    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "to general" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /post/i }));

    // Switch to ops before the publish (issued against general) resolves.
    getChannelHistory.mockResolvedValue(
      historyOf(msg("o1", "ops message", "carol")),
    );
    await fireEvent.change(screen.getByRole("combobox", { name: /channel/i }), {
      target: { value: "ops" },
    });
    await screen.findByText(/ops message/);

    // The general-targeted publish now resolves with its stored message.
    resolvePublish(msg("g9", "to general", "local", "2026-06-02T10:00:09Z"));
    // Wait for the in-flight state to settle ("Posting…" → "Post"), which means
    // the publish resolution (and its echo, were it buggy) has been processed.
    await screen.findByRole("button", { name: "Post" });

    // The general message must not appear in ops's timeline.
    expect(screen.queryByText(/to general/)).toBeNull();
  });

  it("does not orphan the pending poll tick on a repeat visible event", async () => {
    // poll() must clear any armed tick when it runs, not merely null the handle:
    // the visibility handler calls poll() directly, and a repeat 'visible' event
    // (tab already visible, a tick already armed) would otherwise leave that tick
    // orphaned — firing one extra poll against the unauthenticated localhost
    // surface the visibility-pause / backoff is meant to protect.
    vi.useFakeTimers();
    render(ChannelTimeline, { props: { userId: "local" } });
    await vi.waitFor(() => expect(getChannelHistory).toHaveBeenCalled());
    const afterLoad = getChannelHistory.mock.calls.length;

    // A repeat 'visible' event fires an immediate catch-up poll and arms the
    // next tick (document.hidden defaults false in jsdom).
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.waitFor(() =>
      expect(getChannelHistory.mock.calls.length).toBe(afterLoad + 1),
    );
    const afterVisible = getChannelHistory.mock.calls.length;

    // One base interval later exactly one tick should fire — the freshly-armed
    // one. The previously-armed tick must have been cleared, not orphaned (which
    // would fire a second poll in the same window).
    await vi.advanceTimersByTimeAsync(3000);
    expect(getChannelHistory.mock.calls.length).toBe(afterVisible + 1);
  });

  it("does not launch a concurrent poll when a visible event arrives mid-poll", async () => {
    // The visibility handler calls poll() directly on resume, but a 'visible'
    // event can also arrive while a timer-fired tick is still awaiting its
    // fetch. Without an in-flight guard that would launch a second concurrent
    // request against the unauthenticated localhost surface. The in-flight tick
    // reschedules the loop when it settles, so the redundant fetch buys nothing.
    vi.useFakeTimers();
    render(ChannelTimeline, { props: { userId: "local" } });
    await vi.waitFor(() => expect(getChannelHistory).toHaveBeenCalled());
    const afterLoad = getChannelHistory.mock.calls.length;

    // Make the next poll hang so it stays in flight.
    let resolvePoll;
    getChannelHistory.mockReturnValueOnce(
      new Promise((resolve) => {
        resolvePoll = resolve;
      }),
    );

    // The base interval fires the (now-hanging) poll.
    await vi.advanceTimersByTimeAsync(3000);
    const duringPoll = getChannelHistory.mock.calls.length;
    expect(duringPoll).toBe(afterLoad + 1);

    // A visible event arrives while that poll is still in flight: it must not
    // start a second concurrent fetch (the handler runs synchronously up to the
    // fetch, so the call count would jump immediately if it did).
    document.dispatchEvent(new Event("visibilitychange"));
    expect(getChannelHistory.mock.calls.length).toBe(duringPoll);

    // Letting the in-flight poll settle, the loop resumes from a single timer.
    resolvePoll(historyOf(msg("m2", "second"), msg("m1", "first")));
    await vi.advanceTimersByTimeAsync(3000);
    expect(getChannelHistory.mock.calls.length).toBe(duringPoll + 1);
  });
});
