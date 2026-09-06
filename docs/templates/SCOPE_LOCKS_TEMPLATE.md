# vX.Y.Z — the scope locks (record)

**Companion to**: `docs/vX.Y.Z-plan.md`
**Locked**: YYYY-MM-DD, at plan opening · ratified by `docs/v0.3.x-sequencing.md` §Amendment YYYY-MM-DD
**Status**: 🔄 Binding for the whole cycle — a lock is re-opened by an amendment, not by a PR

Each lock states a decision and its binding consequence; the evidence sits in
the issue it belongs to. <If split from the plan, say when and why: "Split out
of the plan on YYYY-MM-DD (at PR <n>) because the plan had reached the
3 000-word cap with <what> still to record, so every later status flip would
have been paid for by deleting a lock.">

> Guidance: three to five locks named by the amendment, plus whatever the
> plan-opening audit forced. One paragraph each. A lock that rests on an
> unknown names a **plan-opening default** and the PR that confirms it.

---

## The locks

- **<Lock title — a decision, stated as a fact>.** <Why it is settled, with
  the date and evidence.> Consequence: <what PR owns the consequence; what the
  release notes must state>. <If it deviates from the amendment's wording:
  "*A deliberate deviation from the amendment's wording, on <issue>'s own
  analysis; reversible at review.*">

- **<Lock title>.** <…> **Plan-opening default**: <the assumed answer>;
  confirmed or overturned at PR <n>.

- **<Lock title — the cuttable item>.** Rides in shape <n>; the amendment's cut
  clause stands (cuts if <condition>). If cut, <what is re-filed where>.

- **<Lock title — the live arc>.** One live arc, one MT: `<MT ID>`, extended
  with <leg>. MT edits that need owners before the paid run: <list>.

---

## Out of scope (so it does not pressure the cut)

| Item | Goes to | Because |
|------|---------|---------|
| <item> | vX.Y.(Z+1) / RFC NNNN Phase n / new issue | <one line> |

## Related documentation

- `docs/methodology/decisions.md` — what a lock is and how it changes
- `docs/vX.Y.Z-plan.md` — the moving half
