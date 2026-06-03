import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/svelte";
import ChannelTimeline from "./ChannelTimeline.svelte";

// RFC 0048 amendment §F — the channel-timeline panel's first-contact and
// cross-panel surfaces: the no-channels empty state is an on-ramp (guidance +
// Refresh + docs link) rather than a dead end, and a hand-off intent recorded
// by the chat panel (nav.targetChannel) opens the freshly-mounted timeline on
// that DM and is consumed one-shot — even when the requested channel is absent,
// so a stale intent can't leak onto a later mount. Split out of
// ChannelTimeline.test.js to keep each spec under the review-size cap; the core
// panel/polling behaviour stays there.
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
}));

import {
  listAgents,
  listChannels,
  getChannelHistory,
  publishMessage,
} from "../lib/api.js";
import { nav } from "../lib/nav.svelte.js";

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
  listAgents.mockResolvedValue([]);
  listChannels.mockResolvedValue({ channels: CHANNELS });
  getChannelHistory.mockResolvedValue(
    historyOf(msg("m2", "second"), msg("m1", "first")),
  );
  publishMessage.mockResolvedValue(
    msg("m3", "from me", "local", "2026-06-02T10:00:03Z"),
  );
  // Reset the shared cross-panel nav intent (§F) so tests don't leak it.
  nav.targetChannel = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("Channel timeline panel — onboarding + cross-panel (§F)", () => {
  it("makes the no-channels empty state an on-ramp, not a dead end (§F)", async () => {
    listChannels.mockResolvedValue({ channels: [] });
    render(ChannelTimeline, { props: { userId: "local" } });

    await screen.findByText(/no channels/i);
    expect(screen.getByRole("button", { name: /refresh/i })).toBeTruthy();
    expect(screen.getByRole("link", { name: /quick-start/i })).toBeTruthy();
  });

  it("opens on the channel handed off from the chat panel (§F)", async () => {
    // The chat panel records a target DM; the freshly-mounted timeline selects
    // it (if present) and consumes the one-shot intent.
    nav.targetChannel = "ops";
    getChannelHistory.mockResolvedValue(
      historyOf(msg("o1", "ops message", "alice")),
    );

    render(ChannelTimeline, { props: { userId: "local" } });

    // History loaded for the handed-off channel, and the intent is cleared.
    await waitFor(() =>
      expect(getChannelHistory).toHaveBeenCalledWith("ops", expect.anything()),
    );
    expect(nav.targetChannel).toBe("");
    const picker = screen.getByRole("combobox", { name: /channel/i });
    expect(picker.value).toBe("ops");
  });

  it("consumes a stale hand-off intent even when its channel is absent (§F)", async () => {
    // The hand-off intent is one-shot, scoped to the mount it triggered. If the
    // requested DM isn't in the list (a race where it hasn't surfaced yet), the
    // intent must still be consumed on this load — otherwise it leaks and would
    // surface an unexpected jump on a later, unrelated mount/Refresh. The load
    // falls back to the first channel.
    nav.targetChannel = "missing-dm";

    render(ChannelTimeline, { props: { userId: "local" } });

    await waitFor(() =>
      expect(getChannelHistory).toHaveBeenCalledWith(
        "general",
        expect.anything(),
      ),
    );
    expect(nav.targetChannel).toBe("");
    const picker = screen.getByRole("combobox", { name: /channel/i });
    expect(picker.value).toBe("general");
  });
});
