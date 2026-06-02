import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
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
  listChannels: vi.fn(),
  getChannelHistory: vi.fn(),
  publishMessage: vi.fn(),
}));

import {
  listChannels,
  getChannelHistory,
  publishMessage,
  ApiError,
} from "../lib/api.js";

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
  listChannels.mockResolvedValue({ channels: CHANNELS });
  getChannelHistory.mockResolvedValue(
    historyOf(msg("m2", "second"), msg("m1", "first")),
  );
  publishMessage.mockResolvedValue(
    msg("m3", "from me", "local", "2026-06-02T10:00:03Z"),
  );
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

  it("renders the selected channel's history newest-first", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });

    // The default channel's history loads; the two messages render with the
    // newest (m2/"second") before the older (m1/"first").
    const items = await screen.findAllByRole("listitem");
    expect(items[0].textContent).toMatch(/second/);
    expect(items[1].textContent).toMatch(/first/);
  });

  it("shows an empty state when no channels exist", async () => {
    listChannels.mockResolvedValue({ channels: [] });

    render(ChannelTimeline, { props: { userId: "local" } });

    expect(await screen.findByText(/no channels/i)).toBeTruthy();
  });

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
      // m3 prepended ahead of the existing two; still newest-first; no dup.
      expect(items.length).toBe(3);
      expect(items[0].textContent).toMatch(/third/);
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
});
