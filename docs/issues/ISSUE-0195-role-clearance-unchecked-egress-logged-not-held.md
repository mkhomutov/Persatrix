---
id: ISSUE-0195
summary: "In v0.4.0 an organization role has no clearance and nothing checks one, so a persona can be a member of a channel of any classification (RFC 0012 §G's membership-time clearance check is unslotted). A persona's publish that repeats memory classified above the target channel is logged by RFC 0037's leak tripwire, not held for a person (RFC 0012 §H is unslotted). RFC 0012's 2026-10-20 amendment took both out of v0.4.0; they return only by an amendment that records an outside ask. A Known Gap of the v0.4.0 plan."
status: open
severity: low
area: security
created: 2026-11-04
refs:
  - docs/rfcs/0012-protocols-organizations.md
  - docs/rfcs/0012-pr-plan.md
  - docs/rfcs/0037-memory-confidentiality-channel-classification.md
  - docs/v0.4.0-plan.md
  - schemas/organization.schema.json
  - agents/confidentiality_tripwire.py
---

# ISSUE-0195: Role clearance is unchecked; egress above a channel's class is logged, not held

## Summary

v0.4.0's organizations give each role an authority ceiling but no
clearance, so nothing stops a persona from being a member of a channel
classified above what its role should see. When a persona's message
repeats memory classified above the channel it posts in, the message is
sent and an audit event is written; nothing holds it for a person.
RFC 0037 reads such a hit as a bug, since its injection gate keeps that
memory out of the channel's prompt.

## Context

RFC 0012 planned both checks for v0.4.0:

- **§G, the clearance check.** A role names a `clearance`, the highest
  RFC 0037 classification its holders may see, and adding a persona to a
  channel classified above it is refused — at `make validate` for the
  static config, and at runtime.
- **§H, the egress gate.** RFC 0037 §G's leak tripwire
  (`agents/confidentiality_tripwire.py`) catches a publish that repeats a
  memory entry classified above the target channel. §H turns that from a
  log line into a hold that a person, or the persona's manager, releases
  with an approval token.

The [v0.4.0 plan](../v0.4.0-plan.md#scope-locks)'s ruling (b) audit found
no outside ask for this kind of memory-isolation work, and RFC 0012's
[amendment 2026-10-20](../rfcs/0012-protocols-organizations.md#amendment-2026-10-20--the-v040-plans-ruling-b-audit)
took both out of the v0.4.0 phases, unslotted, not cut. The PR plan's
D-3 goes further for §G: the v0.4.0 schema
(`schemas/organization.schema.json`) refuses a role's `clearance` key, so
an operator is never shown a ceiling that nothing enforces. The v0.4.0
plan lists this as a Known Gap owned by row C1, whose PR 1 files it.

## Impact

- **Membership.** A persona can be added to any channel, whatever its
  classification and the persona's role. This is v0.3.16's behaviour; an
  organization adds no check to it.
- **Egress.** A publish that copies a span of memory classified above
  its channel is sent. The tripwire writes a
  `channel.confidentiality_tripwire` audit event naming the persona, the
  channel and the entry's protection level, so an operator can find it
  afterwards; §H would have held it instead. The tripwire matches verbatim
  spans only, so a paraphrase is neither logged now nor held by §H, which
  keeps the same detection.

Neither weakens a v0.3.16 control. RFC 0037's injection gate (§D) keeps
memory classified above a channel out of that channel's prompt, so RFC 0037
§G reads a true verbatim hit as a bug — an entry stamped at the wrong
level, a projection that copied its source, or a missed injection path —
not a path the design leaves open. What goes unheld is that bug class.
Severity is low for that reason.

## Proposed fix / investigation path

An amendment whose External evidence section records an outside ask
brings either check back. For §G: the role schema accepts `clearance`
again, `agents/org_validate.py` and RFC 0012 PR 2's Go loader check each
static membership against `config/channels.yaml`, and the channel
membership path checks it at runtime with an audit event on refusal.
For §H: the tripwire's hit becomes an RFC 0028 required-HITL hold, as
RFC 0012 §H describes.

## Notes

> 2026-11-04 — filed by RFC 0012 PR 1, as the v0.4.0 plan's Known Gaps
> table assigns to row C1.
