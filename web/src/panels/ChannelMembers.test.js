import { describe, it, expect, vi, afterEach } from "vitest";
import { render, cleanup, screen, fireEvent } from "@testing-library/svelte";
import ChannelMembers from "./ChannelMembers.svelte";

// ChannelMembers is the group-channel roster surface (RFC 0011 §C add/remove):
// it lists members with their disposition + the v0.3.8 salience signal and
// lets an operator add a persona (any disposition) or remove a member. It does
// not fetch the roster itself — after a mutation it calls onChanged() so the
// panel re-lists, keeping the server's normalized read-back as the source of
// truth.
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function noContent() {
  return { ok: true, status: 204, json: () => Promise.resolve(null) };
}

function renderMembers(props = {}) {
  return render(ChannelMembers, {
    props: {
      channelId: "group:planning",
      members: [
        {
          id: "ada",
          respond: "always",
          joined_at: "2026-06-01T10:00:00Z",
          salience_gated: true,
          threshold: 0.3,
        },
        {
          id: "local",
          respond: "never",
          joined_at: "2026-06-01T10:00:00Z",
          salience_gated: false,
        },
      ],
      agents: [
        { id: "ada", name: "Ada", type: "persona" },
        { id: "iron-fox", name: "Iron Fox", type: "persona" },
        { id: "builder", name: "Builder", type: "task" },
      ],
      agentsById: {
        ada: { id: "ada", name: "Ada" },
        "iron-fox": { id: "iron-fox", name: "Iron Fox" },
      },
      userId: "local",
      onChanged: vi.fn(() => Promise.resolve()),
      ...props,
    },
  });
}

describe("ChannelMembers", () => {
  it("lists members with disposition, the salience signal, and flags the acting user", () => {
    renderMembers();
    // Member rows carry the persisted respond token...
    expect(screen.getByText("Ada")).toBeTruthy();
    expect(screen.getByText("always")).toBeTruthy();
    // ...plus the salience note that confirms a normalized chair/participant
    // disposition took (respond reads back as the legacy token).
    expect(screen.getByText("salience-gated · threshold 0.3")).toBeTruthy();
    // The acting principal is flagged.
    expect(screen.getByText("local (you)")).toBeTruthy();
  });

  it("offers no Remove button for the acting user (self-removal would lock them out with no web re-add path)", () => {
    renderMembers();
    // A persona member is removable, and the button's accessible name uses the
    // display name shown on the row ("Ada"), not the raw id — so a screen reader
    // announces the same identity the sighted operator sees.
    expect(screen.getByLabelText("Remove Ada")).toBeTruthy();
    expect(screen.queryByLabelText("Remove ada")).toBeNull();
    // ...but the acting principal is not: they are not in the agent registry,
    // so the add picker could never re-add them, and a non-member sender is
    // rejected (403) on publish. Removing themselves is an unrecoverable
    // lockout from this surface, so the affordance is withheld.
    expect(screen.queryByLabelText("Remove local")).toBeNull();
  });

  it("offers only non-member personas as add candidates (excludes members and task agents)", () => {
    renderMembers();
    const select = screen.getByLabelText("Add persona");
    const optionValues = Array.from(select.options).map((o) => o.value);
    expect(optionValues).toContain("iron-fox"); // persona, not yet a member
    expect(optionValues).not.toContain("ada"); // already a member
    expect(optionValues).not.toContain("builder"); // task agent — not conversational
  });

  it("removes a member and re-lists via onChanged", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(noContent()));
    vi.stubGlobal("fetch", fetchMock);
    const onChanged = vi.fn(() => Promise.resolve());
    renderMembers({ onChanged });

    await fireEvent.click(screen.getByLabelText("Remove Ada"));

    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/v1/channels/group%3Aplanning/members/ada");
    expect(init.method).toBe("DELETE");
  });

  it("adds the selected persona with the chosen disposition and re-lists", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(noContent()));
    vi.stubGlobal("fetch", fetchMock);
    const onChanged = vi.fn(() => Promise.resolve());
    renderMembers({ onChanged });

    await fireEvent.change(screen.getByLabelText("Add persona"), {
      target: { value: "iron-fox" },
    });
    await fireEvent.change(
      screen.getByLabelText("Disposition for the new member"),
      { target: { value: "chair" } },
    );
    await fireEvent.click(screen.getByRole("button", { name: "Add member" }));

    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/v1/channels/group%3Aplanning/members");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ id: "iron-fox", respond: "chair" });
  });

  it("surfaces the server error and does not re-list when an add fails", async () => {
    // A plausible real failure: the watched channel was deleted between the
    // list and the add, so the server reports 404 (the add is idempotent, so
    // there is no "already a member" conflict to assert here).
    const fetchMock = vi.fn(() =>
      Promise.resolve({
        ok: false,
        status: 404,
        json: () => Promise.resolve({ error: "channel not found" }),
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onChanged = vi.fn(() => Promise.resolve());
    renderMembers({ onChanged });

    await fireEvent.change(screen.getByLabelText("Add persona"), {
      target: { value: "iron-fox" },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Add member" }));

    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toContain(
      "channel not found",
    );
    expect(onChanged).not.toHaveBeenCalled();
  });
});
