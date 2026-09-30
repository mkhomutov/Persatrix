import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/svelte";
import DmComposer from "./DmComposer.svelte";

// The DM composer names who the message goes to and, near the chat façade's
// 4 000-character limit (chat_handler.go counts code points), shows how much
// room is left — before the send is refused, not after.
vi.mock("../lib/api.js", () => ({
  ApiError: class ApiError extends Error {},
  listSessions: vi.fn(() => Promise.resolve({ sessions: [] })),
  createSession: vi.fn(),
}));

afterEach(cleanup);

function setup(message = "") {
  render(DmComposer, {
    props: {
      message,
      sending: false,
      canSend: true,
      chattable: true,
      hasPersona: true,
      personaName: "Ember Owl",
      onSubmit: vi.fn(),
      onKeydown: vi.fn(),
    },
  });
  return screen.getByRole("textbox", { name: /message/i });
}

describe("DmComposer", () => {
  it("addresses the persona in the placeholder", () => {
    expect(setup().getAttribute("placeholder")).toMatch(/ember owl/i);
  });

  it("shows no counter for an ordinary message", () => {
    setup("hello");
    expect(screen.queryByText(/\/ 4,?000/)).toBeNull();
  });

  it("counts characters near the limit, in code points", () => {
    // 3 600 emoji are 7 200 UTF-16 units but 3 600 characters to the server.
    setup("😀".repeat(3600));
    expect(screen.getByText(/3,?600 \/ 4,?000/)).toBeTruthy();
  });

  it("flags a message over the limit", () => {
    setup("x".repeat(4100));
    const counter = screen.getByText(/4,?100 \/ 4,?000/);
    expect(counter.classList.contains("over")).toBe(true);
  });
});
