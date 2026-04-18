#!/usr/bin/env python3
"""Generate nickname-style persona IDs and display names.

This utility intentionally avoids human-like names to reduce accidental overlap
with real people. Output is suitable for `config/agents.yaml` snippets.
"""

from __future__ import annotations

import argparse
import random
from dataclasses import dataclass


ADJECTIVES: tuple[str, ...] = (
    "amber",
    "arc",
    "atlas",
    "binary",
    "blaze",
    "cinder",
    "cobalt",
    "comet",
    "crimson",
    "delta",
    "echo",
    "ember",
    "flux",
    "frost",
    "glint",
    "grid",
    "halo",
    "horizon",
    "indigo",
    "ion",
    "ivory",
    "jade",
    "lumen",
    "lunar",
    "matrix",
    "midnight",
    "mint",
    "nebula",
    "neon",
    "nova",
    "obsidian",
    "orbit",
    "pixel",
    "plasma",
    "pulse",
    "quantum",
    "raven",
    "ripple",
    "solar",
    "spectral",
    "spruce",
    "static",
    "storm",
    "swift",
    "terra",
    "ultra",
    "vector",
    "velvet",
    "vertex",
    "violet",
)

NOUNS: tuple[str, ...] = (
    "badger",
    "bear",
    "beetle",
    "bison",
    "crane",
    "crow",
    "drake",
    "falcon",
    "finch",
    "fox",
    "gecko",
    "gull",
    "hawk",
    "heron",
    "ibis",
    "kite",
    "koala",
    "lark",
    "lemur",
    "lynx",
    "manta",
    "marten",
    "moose",
    "newt",
    "ocelot",
    "otter",
    "owl",
    "panther",
    "pika",
    "puma",
    "quail",
    "raven",
    "seal",
    "shark",
    "sparrow",
    "stoat",
    "swift",
    "tern",
    "tiger",
    "viper",
    "walrus",
    "weasel",
    "wombat",
    "yak",
    "zorilla",
)


@dataclass(frozen=True)
class PersonaNickname:
    persona_id: str
    display_name: str


def _to_display_name(persona_id: str) -> str:
    return " ".join(part.capitalize() for part in persona_id.split("-"))


def generate_nicknames(count: int, seed: int | None = None) -> list[PersonaNickname]:
    if count < 1:
        raise ValueError("count must be >= 1")

    all_combinations = [f"{adj}-{noun}" for adj in ADJECTIVES for noun in NOUNS]
    if count > len(all_combinations):
        raise ValueError(
            f"count exceeds available unique nicknames ({len(all_combinations)})"
        )

    rng = random.Random(seed)
    rng.shuffle(all_combinations)

    selected_ids = all_combinations[:count]
    return [PersonaNickname(persona_id=s, display_name=_to_display_name(s)) for s in selected_ids]


def _format_yaml(entries: list[PersonaNickname]) -> str:
    lines: list[str] = []
    for item in entries:
        lines.extend(
            [
                f'- id: "{item.persona_id}"',
                '  type: "persona"',
                f'  name: "{item.display_name}"',
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def _format_ids(entries: list[PersonaNickname]) -> str:
    return "\n".join(item.persona_id for item in entries)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate nickname-style persona IDs and display names "
            "for Persatrix config files."
        )
    )
    parser.add_argument("--count", type=int, default=1, help="Number of nicknames to generate")
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional random seed for reproducible output",
    )
    parser.add_argument(
        "--format",
        choices=("yaml", "ids"),
        default="yaml",
        help="Output format",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    entries = generate_nicknames(count=args.count, seed=args.seed)

    if args.format == "ids":
        print(_format_ids(entries))
    else:
        print(_format_yaml(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
