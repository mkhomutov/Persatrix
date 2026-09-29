import { describe, it, expect, vi, afterEach } from "vitest";
import { render, cleanup, screen, fireEvent, waitFor } from "@testing-library/svelte";
import CreateChannelForm from "./CreateChannelForm.svelte";

// The new-channel dialog opens ready to type (focus on the name), and with a
// big persona fleet the member picker narrows as you type — picks already made
// stay picked while they are filtered out of view.
vi.mock("../lib/api.js", () => ({
  ApiError: class ApiError extends Error {},
  createChannel: vi.fn(),
}));

import { createChannel } from "../lib/api.js";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function personas(n) {
  return Array.from({ length: n }, (_, i) => ({
    id: `p${i}`,
    name: `Persona ${i}`,
    role: i === 3 ? "Security reviewer" : "Generalist",
  }));
}

function renderForm(agents) {
  const onCreated = vi.fn();
  render(CreateChannelForm, {
    props: { agents, userId: "local", onCreated, onCancel: vi.fn() },
  });
  return { onCreated };
}

describe("CreateChannelForm", () => {
  it("focuses the channel name when it opens", async () => {
    renderForm(personas(2));
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("textbox", { name: /channel name/i }),
      ),
    );
  });

  it("keeps Tab and Shift+Tab inside the dialog", async () => {
    renderForm(personas(2));
    const dialog = screen.getByRole("dialog");
    const focusables = [
      ...dialog.querySelectorAll("button, input, select, textarea"),
    ].filter((el) => !el.disabled);

    focusables.at(-1).focus();
    await fireEvent.keyDown(document.activeElement, { key: "Tab" });
    expect(document.activeElement).toBe(focusables[0]);

    await fireEvent.keyDown(document.activeElement, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(focusables.at(-1));
  });

  it("gives focus back to where it was when it closes", async () => {
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();
    try {
      const { unmount } = render(CreateChannelForm, {
        props: { agents: personas(2), userId: "local", onCreated: vi.fn(), onCancel: vi.fn() },
      });
      await waitFor(() =>
        expect(document.activeElement).toBe(
          screen.getByRole("textbox", { name: /channel name/i }),
        ),
      );
      unmount();
      expect(document.activeElement).toBe(opener);
    } finally {
      opener.remove();
    }
  });

  it("offers no persona filter for a short list", () => {
    renderForm(personas(4));
    expect(screen.queryByRole("searchbox", { name: /filter personas/i })).toBeNull();
  });

  it("filters a long persona list and keeps hidden picks selected", async () => {
    createChannel.mockResolvedValue({ id: "group:sec" });
    const { onCreated } = renderForm(personas(10));

    await fireEvent.click(screen.getByRole("checkbox", { name: /persona 1\b/i }));
    await fireEvent.input(screen.getByRole("searchbox", { name: /filter personas/i }), {
      target: { value: "security" },
    });

    expect(screen.getAllByRole("checkbox")).toHaveLength(1);
    await fireEvent.click(screen.getByRole("checkbox", { name: /persona 3/i }));

    await fireEvent.input(screen.getByRole("textbox", { name: /channel name/i }), {
      target: { value: "sec" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /create channel/i }));

    await waitFor(() => expect(createChannel).toHaveBeenCalledTimes(1));
    expect(createChannel.mock.calls[0][0].members.map((m) => m.id)).toEqual([
      "p1",
      "p3",
      "local",
    ]);
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
  });
});
