import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/svelte";
import SelectList from "./SelectList.svelte";

// The sidebar's conversation lists are single-select ARIA listboxes: each row
// is an option named by its label, the open conversation is the selected
// option, and the list is driven by keyboard as well as by pointer (APG
// listbox: one tab stop, arrows move, Home/End jump, Enter/Space picks,
// a typed letter jumps to the next matching row).
const ITEMS = [
  { id: "general", label: "General" },
  { id: "ops", label: "Ops" },
  { id: "runner", label: "Runner", disabled: true },
  { id: "random", label: "Random" },
];

function renderList(props = {}) {
  const onselect = vi.fn();
  render(SelectList, {
    props: {
      label: "Channels",
      items: ITEMS,
      selectedKey: "ops",
      isDisabled: (item) => Boolean(item.disabled),
      onselect,
      ...props,
    },
  });
  return { onselect };
}

afterEach(() => {
  cleanup();
});

describe("SelectList", () => {
  it("renders a labelled listbox with one named option per item", () => {
    renderList();
    const list = screen.getByRole("listbox", { name: "Channels" });
    expect(list).toBeTruthy();
    expect(screen.getAllByRole("option").map((o) => o.getAttribute("aria-label"))).toEqual([
      "General",
      "Ops",
      "Runner",
      "Random",
    ]);
  });

  it("marks only the selected item as selected", () => {
    renderList();
    expect(screen.getByRole("option", { name: "Ops", selected: true })).toBeTruthy();
    expect(screen.getAllByRole("option", { selected: true })).toHaveLength(1);
  });

  it("selects an item on click", async () => {
    const { onselect } = renderList();
    await fireEvent.click(screen.getByRole("option", { name: "Random" }));
    expect(onselect).toHaveBeenCalledWith(ITEMS[3]);
  });

  it("marks a disabled item and ignores clicks on it", async () => {
    const { onselect } = renderList();
    const runner = screen.getByRole("option", { name: "Runner" });
    expect(runner.getAttribute("aria-disabled")).toBe("true");
    await fireEvent.click(runner);
    expect(onselect).not.toHaveBeenCalled();
  });

  it("keeps a single tab stop on the selected option", () => {
    renderList();
    const stops = screen
      .getAllByRole("option")
      .filter((o) => o.getAttribute("tabindex") === "0");
    expect(stops.map((o) => o.getAttribute("aria-label"))).toEqual(["Ops"]);
  });

  it("puts the tab stop on the first enabled option when nothing is selected", () => {
    renderList({ selectedKey: "" });
    expect(screen.getByRole("option", { name: "General" }).getAttribute("tabindex")).toBe(
      "0",
    );
  });

  it("moves focus with the arrow keys, skipping disabled items, and wraps", async () => {
    renderList();
    const ops = screen.getByRole("option", { name: "Ops" });
    ops.focus();
    await fireEvent.keyDown(ops, { key: "ArrowDown" });
    // Runner is disabled, so focus lands on Random.
    expect(document.activeElement.getAttribute("aria-label")).toBe("Random");
    await fireEvent.keyDown(document.activeElement, { key: "ArrowDown" });
    expect(document.activeElement.getAttribute("aria-label")).toBe("General");
    await fireEvent.keyDown(document.activeElement, { key: "ArrowUp" });
    expect(document.activeElement.getAttribute("aria-label")).toBe("Random");
  });

  it("jumps to the ends with Home and End", async () => {
    renderList();
    const ops = screen.getByRole("option", { name: "Ops" });
    ops.focus();
    await fireEvent.keyDown(ops, { key: "End" });
    expect(document.activeElement.getAttribute("aria-label")).toBe("Random");
    await fireEvent.keyDown(document.activeElement, { key: "Home" });
    expect(document.activeElement.getAttribute("aria-label")).toBe("General");
  });

  it("selects the focused option with Enter or Space, without selecting on arrow moves", async () => {
    const { onselect } = renderList();
    const ops = screen.getByRole("option", { name: "Ops" });
    ops.focus();
    await fireEvent.keyDown(ops, { key: "ArrowDown" });
    expect(onselect).not.toHaveBeenCalled();
    await fireEvent.keyDown(document.activeElement, { key: "Enter" });
    expect(onselect).toHaveBeenLastCalledWith(ITEMS[3]);
    await fireEvent.keyDown(document.activeElement, { key: "Home" });
    await fireEvent.keyDown(document.activeElement, { key: " " });
    expect(onselect).toHaveBeenLastCalledWith(ITEMS[0]);
  });

  it("jumps to the next option starting with a typed letter", async () => {
    renderList();
    const ops = screen.getByRole("option", { name: "Ops" });
    ops.focus();
    await fireEvent.keyDown(ops, { key: "r" });
    // Runner is disabled, so the letter lands on Random.
    expect(document.activeElement.getAttribute("aria-label")).toBe("Random");
  });

  it("shows the empty text instead of an empty listbox", () => {
    renderList({ items: [], emptyText: "No channels match." });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.getByText("No channels match.")).toBeTruthy();
  });
});
