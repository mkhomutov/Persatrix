"""
Configuration validator.

Validates all YAML configs against JSON schemas.
Usage: python -m orchestr8_agents.validate config/
"""

import json
import sys
from pathlib import Path

import yaml

# TODO: Implement JSON Schema validation using jsonschema library
# TODO: Load schemas from schemas/ directory
# TODO: Validate: agents.yaml, workflows/*.yaml, mcp-servers.yaml,
#       channels.yaml, organizations.yaml, bridges.yaml, optimization.yaml
# TODO: Report errors with file path, line number, and fix suggestion
# TODO: Support --strict mode (warnings become errors)


def validate_config_dir(config_dir: str) -> bool:
    """Validate all YAML files in a config directory. Returns True if all valid."""
    config_path = Path(config_dir)
    if not config_path.exists():
        print(f"ERROR: Config directory not found: {config_dir}")
        return False

    # TODO: Implement validation
    print(f"Validating configs in {config_dir}...")
    print("WARNING: Validation not yet implemented")
    return True


if __name__ == "__main__":
    config_dir = sys.argv[1] if len(sys.argv) > 1 else "config/"
    success = validate_config_dir(config_dir)
    sys.exit(0 if success else 1)
