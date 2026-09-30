import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/svelte";
import Avatar from "./Avatar.svelte";

// The health dot uses the orchestrator's own status words (agentStatusString:
// healthy / degraded / offline / unknown), so a persona that cannot reply is
// marked in the sidebar, not only in its row's accessible name.
afterEach(() => {
  cleanup();
});

function dotFor(status) {
  const { container } = render(Avatar, { props: { id: "ada", label: "Ada", status } });
  return container.querySelector(".dot");
}

describe("Avatar health dot", () => {
  it("draws green for healthy, amber for degraded and red for offline", () => {
    expect(dotFor("healthy").classList.contains("ok")).toBe(true);
    cleanup();
    expect(dotFor("degraded").classList.contains("warn")).toBe(true);
    cleanup();
    expect(dotFor("offline").classList.contains("danger")).toBe(true);
  });

  it("draws no dot for an unknown status", () => {
    expect(dotFor("unknown")).toBeNull();
  });
});
