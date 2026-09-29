import { describe, it, expect, vi, afterEach } from "vitest";
import { render, cleanup, screen, fireEvent, within } from "@testing-library/svelte";
import ChannelMembers from "./ChannelMembers.svelte";

// A large room's roster gets a filter box, so finding one member among dozens
// is typing, not scrolling. A small roster doesn't need one and shows none.
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function member(id) {
  return { id, respond: "when_mentioned", joined_at: "2026-06-01T10:00:00Z", salience_gated: false };
}

function renderMembers(ids) {
  const agents = ids.map((id) => ({ id, name: id[0].toUpperCase() + id.slice(1), role: `${id} role` }));
  return render(ChannelMembers, {
    props: {
      channelId: "group:big",
      members: ids.map(member),
      agents,
      agentsById: Object.fromEntries(agents.map((a) => [a.id, a])),
      userId: "local",
      onChanged: vi.fn(() => Promise.resolve()),
    },
  });
}

const MANY = ["ada", "bob", "cleo", "dan", "eve", "finn", "gus", "hal"];

describe("ChannelMembers filter", () => {
  it("offers no filter for a small roster", () => {
    renderMembers(["ada", "bob"]);
    expect(screen.queryByRole("searchbox", { name: /filter members/i })).toBeNull();
  });

  it("narrows a large roster by name or role", async () => {
    renderMembers(MANY);
    const list = screen.getByRole("list", { name: /members/i });
    expect(within(list).getAllByRole("listitem")).toHaveLength(8);

    await fireEvent.input(screen.getByRole("searchbox", { name: /filter members/i }), {
      target: { value: "cle" },
    });

    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    expect(within(list).getByText("Cleo")).toBeTruthy();

    await fireEvent.input(screen.getByRole("searchbox", { name: /filter members/i }), {
      target: { value: "hal role" },
    });
    expect(within(list).getByText("Hal")).toBeTruthy();
  });

  it("says so when nothing matches", async () => {
    renderMembers(MANY);
    await fireEvent.input(screen.getByRole("searchbox", { name: /filter members/i }), {
      target: { value: "zzz" },
    });
    expect(screen.getByText(/no members match/i)).toBeTruthy();
  });
});
