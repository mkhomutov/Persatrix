// List filtering shared by the sidebar's "Jump to…" box, the members card and
// the new-channel member picker, so long lists narrow the same way everywhere.

// fold lower-cases and strips accents, so "cafe" finds "Café".
function fold(text) {
  return String(text ?? "")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase();
}

// matchesQuery reports whether every word of `query` appears in at least one
// of `fields`. A blank query matches everything.
export function matchesQuery(fields, query) {
  const words = fold(query).split(/\s+/).filter(Boolean);
  if (words.length === 0) {
    return true;
  }
  const haystack = fields.map(fold).join("\n");
  return words.every((word) => haystack.includes(word));
}

const collator = new Intl.Collator(undefined, {
  numeric: true,
  sensitivity: "base",
});

// byLabel builds a sort comparator over a label getter: case-insensitive and
// number-aware ("team 9" before "team 10").
export function byLabel(getLabel) {
  return (a, b) => collator.compare(getLabel(a), getLabel(b));
}
