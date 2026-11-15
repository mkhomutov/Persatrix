import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/svelte";
import ChannelTimeline from "./ChannelTimeline.svelte";
import { selection } from "../lib/selection.svelte.js";

// What one console window costs while it shows a group channel (ISSUE-0224).
// Every data request counts toward one budget: 60 calls per 60 seconds by
// default, shared by every console window while sign-in is off. Watching the
// shipped `planning` channel, with its three personas, must stay inside it
// with room for a person's own clicks.
//
// This case counts every request the panel sends in one minute of watching,
// by endpoint. The backend is replaced at `fetch`, not at the client's
// functions, so a read added anywhere — through a new client function too —
// is counted. The panel is mounted as the shell mounts it by default, with
// the details rail (members and settings) beside the conversation.

const PERSONAS = ["ember-owl", "iron-fox", "nova-sparrow"];
const BUDGET = 60;
const CHANNEL = "group:planning";
const AT = `/api/v1/channels/${encodeURIComponent(CHANNEL)}`;

// The stand-in backend: what each path the panel reads answers. A path that
// is not here answers 404 and is counted under its own name.
const BACKEND = {
  "/api/v1/agents": PERSONAS.map((id) => ({
    id,
    name: id,
    role: "Persona",
    status: "healthy",
  })),
  "/api/v1/channels": {
    channels: [
      {
        id: CHANNEL,
        name: "planning",
        channel_type: "group",
        members: PERSONAS.map((id) => ({ id, respond: "auto" })),
      },
    ],
  },
  [`${AT}/messages`]: {
    messages: [
      {
        id: "m1",
        channel_id: CHANNEL,
        sender_id: "ember-owl",
        content: "a first message",
        timestamp: "2026-06-02T10:00:00Z",
        mentions: [],
      },
    ],
  },
  [`${AT}/activity`]: { thinking: [] },
  [`${AT}/config`]: { revision: 1 },
  ...Object.fromEntries(
    PERSONAS.map((id) => [
      `/api/v1/agents/${id}/interactions/closed`,
      { interactions: [] },
    ]),
  ),
};

function endpoint(path) {
  if (path.endsWith("/messages")) return "channel history";
  if (path.endsWith("/activity")) return "channel activity";
  if (path.endsWith("/config")) return "channel config";
  if (path.endsWith("/interactions/closed")) return "closed interactions";
  return path.replace("/api/v1/", "");
}

// Every request sent since the last reset, as "endpoint" (a GET) or
// "METHOD endpoint".
let sent;
const count = () =>
  sent.reduce((by, name) => ({ ...by, [name]: (by[name] ?? 0) + 1 }), {});

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  sent = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url, init) => {
      const { pathname } = new URL(url, "http://console.test");
      const method = init?.method ?? "GET";
      sent.push(method === "GET" ? endpoint(pathname) : `${method} ${endpoint(pathname)}`);
      const body = BACKEND[pathname];
      return Promise.resolve(
        body === undefined
          ? { ok: false, status: 404, json: () => Promise.resolve({ error: "not found", code: "NOT_FOUND" }) }
          : { ok: true, status: 200, json: () => Promise.resolve(body) },
      );
    }),
  );
  selection.dmAgent = "";
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("Channels panel — requests in one minute of watching a group channel", () => {
  it("makes 46 requests, 14 inside the default budget of 60", async () => {
    render(ChannelTimeline, {
      props: { userId: "local", canCreate: true, canConfigEdit: true },
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.getByText("a first message")).toBeTruthy();
    // Opening the channel: the two lists, then six for the channel itself —
    // its first page, its activity, its settings for the details rail, and
    // one summary read per persona. A switch to another group channel costs
    // those six again.
    expect(count()).toEqual({
      agents: 1,
      channels: 1,
      "channel history": 1,
      "channel activity": 1,
      "channel config": 1,
      "closed interactions": 3,
    });
    sent = [];

    await vi.advanceTimersByTimeAsync(60000);

    // The feed: a history read and an activity read every three seconds.
    // The summary: each of the three personas twice. The details rail reads
    // nothing while the channel stays open.
    expect(count()).toEqual({
      "channel history": 20,
      "channel activity": 20,
      "closed interactions": 6,
    });
    expect(sent).toHaveLength(46);
    expect(BUDGET - sent.length).toBe(14);
  });
});
