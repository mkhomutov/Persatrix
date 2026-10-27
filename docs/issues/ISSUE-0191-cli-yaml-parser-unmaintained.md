---
id: ISSUE-0191
summary: "The CLI reads and writes channel YAML through serde_yaml_ng, whose parser, unsafe-libyaml, is unmaintained (RUSTSEC-2024-0449) and has no upgrade. deny.toml ignores the advisory so the weekly audit passes; the fix is to move the three `channel config` YAML verbs to a parser written in safe Rust, then drop the ignore."
status: open
severity: low
area: cli
created: 2026-10-27
refs:
  - deny.toml
  - cli/Cargo.toml
  - cli/src/commands/channel_config_yaml.rs
  - cli/src/commands/channel_config_autonomous.rs
  - https://rustsec.org/advisories/RUSTSEC-2024-0449
  - https://github.com/mkhomutov/Persatrix/issues/1043
---

# ISSUE-0191: The CLI's YAML parser is unmaintained

## Summary

`persatrix channel config import`, `diff` and `export` handle YAML through
`serde_yaml_ng` 0.10. Its parser, `unsafe-libyaml`, is a machine
translation of the C libyaml library into unsafe Rust. Its repository was
archived in March 2024, so any bug found in it stays unfixed.
`deny.toml` ignores the advisory so the weekly dependency audit can pass.
Replacing the parser is the real fix.

## Context

The weekly audit (GitHub issue #1043) started failing on RUSTSEC-2024-0449
in October 2026. `serde_yaml_ng` 0.10.0 is still its latest release, and
the advisory says no safe upgrade exists.

The CLI uses YAML in two files:

- `cli/src/commands/channel_config_yaml.rs` parses `config/channels.yaml`
  (`parse_channels_doc`), converts each knob's YAML value to JSON
  (`yaml_to_knob_json`), and writes a channel block back out (`export`).
- `cli/src/commands/channel_config_autonomous.rs` converts a YAML list to
  a JSON array (`coerce_yaml_list`).

Both work on `serde_yaml_ng::Value`, and both rely on how it types an
unquoted scalar: `true` reads as a boolean and `12` as a number. Their test
files, `channel_config_yaml_tests.rs` and `channel_config_autonomous_tests.rs`,
call `serde_yaml_ng` directly too.

## Impact

Low. The parser reads only a file the operator names on the command line,
never YAML from the network. The risk is that a parser bug will never be
fixed, not an attack anyone can mount today.

## Proposed fix / investigation path

Move to a YAML crate written in safe Rust. The advisory suggests
`serde-saphyr` (serde-based) or `noyalib`. The move has to keep:

- how an unquoted scalar is typed: a knob that expects a string rejects a
  value the parser read as a boolean
  (`yaml_to_knob_json_rejects_wrong_type_naming_the_knob`), so an unquoted
  `true` must still read as a boolean;
- the shape `export` writes, so an exported block imports unchanged.

Then delete the `RUSTSEC-2024-0449` entry from `deny.toml` and run
`cd cli && cargo deny check advisories bans sources licenses`.

## Notes

> 2026-10-27 — filed while fixing the weekly audit for GitHub issue #1043;
> that PR adds the `deny.toml` ignore.
