#!/usr/bin/env python3
"""
atlas_yaml_validator.py
Post-write YAML validation for Atlas macro_backdrop.yaml.
Add this call at the end of weekly_macro.py after writing the backdrop.

Usage (standalone):
    python3 atlas_yaml_validator.py

Usage (in weekly_macro.py after writing backdrop):
    from atlas_yaml_validator import validate_backdrop
    ok, issues = validate_backdrop(backdrop_path)
    if not ok:
        print(f"  ⚠ Atlas YAML validation failed: {issues}")
"""

import yaml
import os
import sys

BASE_DIR = os.environ.get("STOCK_BASE_DIR", "/home/jay/stock_dashboard")

REQUIRED_KEYS = [
    "monetary_policy",
    "inflation",
    "growth",
    "labor_market",
    "credit",
    "ai_guidance",
]

REQUIRED_SUBKEYS = {
    "monetary_policy": ["stance", "key_metrics"],
    "inflation":       ["stance", "key_metrics"],
    "growth":          ["stance", "key_metrics"],
    "labor_market":    ["stance", "key_metrics"],
    "credit":          ["stance", "key_metrics"],
}


def validate_backdrop(path: str) -> tuple[bool, list[str]]:
    """
    Validate Atlas macro_backdrop.yaml for completeness.
    Returns (is_valid, list_of_issues).
    """
    issues = []

    if not os.path.exists(path):
        return False, [f"File not found: {path}"]

    # Size sanity check
    size = os.path.getsize(path)
    if size < 2000:
        issues.append(f"File suspiciously small: {size} bytes")

    # Read raw content
    with open(path) as f:
        content = f.read()

    # Check for mid-word truncation (common Atlas failure mode)
    # Look for lines that end without punctuation or common endings
    lines = content.split('\n')
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if (stripped and
            not stripped.startswith('#') and
            not stripped.startswith('-') and
            ':' in stripped):
            value = stripped.split(':', 1)[-1].strip()
            if (value and
                len(value) > 5 and
                not value[-1] in '."\'%)-_0123456789>|' and
                not value.endswith('...') and
                value[-1].isalpha() and
                value[-1].islower()):
                # Likely truncated mid-word
                issues.append(
                    f"Line {i+1} may be truncated: "
                    f"'{stripped[-50:]}'"
                )

    # Parse YAML
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        return False, [f"YAML parse error: {str(e)[:150]}"]

    if not isinstance(data, dict):
        return False, ["YAML root is not a dict"]

    # Check required top-level keys
    for key in REQUIRED_KEYS:
        if key not in data:
            issues.append(f"Missing required key: {key}")
        elif isinstance(data[key], dict):
            subkeys = REQUIRED_SUBKEYS.get(key, [])
            for sk in subkeys:
                if sk not in data[key]:
                    issues.append(f"Missing {key}.{sk}")
                elif data[key][sk] is None:
                    issues.append(f"Null value at {key}.{sk}")
                elif isinstance(data[key][sk], dict):
                    # Check for null values in key_metrics
                    for mk, mv in data[key][sk].items():
                        if mv is None:
                            issues.append(
                                f"Null metric at {key}.{sk}.{mk}"
                            )

    # Check generated_date
    gen_date = data.get("generated_date")
    if not gen_date:
        issues.append("Missing generated_date field")

    return len(issues) == 0, issues


def main():
    path = f"{BASE_DIR}/macro_backdrop.yaml"
    print(f"Validating: {path}")
    ok, issues = validate_backdrop(path)

    if ok:
        print("✓ Atlas YAML is valid — all required fields present")
    else:
        print(f"⚠ Atlas YAML has {len(issues)} issue(s):")
        for issue in issues:
            print(f"  - {issue}")
        sys.exit(1)


if __name__ == "__main__":
    main()
