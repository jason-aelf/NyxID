#!/usr/bin/env python3
"""Normalize a coverage report into a tiny summary JSON for the CI gate.

Used by .github/workflows/ci.yml coverage jobs (issue #785). Reads either a
`cargo llvm-cov report --json --summary-only` document (Rust) or a vitest v8
`coverage-summary.json` (Frontend) and writes:

    {"component": "...", "label": "...", "threshold": <int>, "head_pct": <float>}

The base-branch line % is merged in later by coverage_merge_base.py.

Usage:
    coverage_summary.py <component> <label> <threshold> <input.json> <output.json>
"""
import json
import math
import sys


def normalize_pct(value: object) -> float:
    # bool is a number in Python, but is not a coverage measurement.
    if isinstance(value, bool):
        return 0.0
    try:
        percentage = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(percentage) or not 0 <= percentage <= 100:
        return 0.0
    return percentage


def line_pct(doc: object) -> float:
    """Extract total line coverage % from either supported report shape."""
    # cargo-llvm-cov: {"data": [{"totals": {"lines": {"percent": ...}}}]}
    if not isinstance(doc, dict):
        raise ValueError("coverage report must be an object")
    if "data" in doc:
        data = doc["data"]
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise ValueError("invalid cargo-llvm-cov data")
        totals = data[0].get("totals")
        if not isinstance(totals, dict):
            raise ValueError("invalid cargo-llvm-cov totals")
        lines = totals.get("lines")
        if not isinstance(lines, dict) or "percent" not in lines:
            raise ValueError("missing cargo-llvm-cov line percentage")
        return normalize_pct(lines["percent"])
    # vitest v8 json-summary: {"total": {"lines": {"pct": ...}}}. v8 emits the
    # string "Unknown" when no files match the include glob — treat that (and
    # any non-numeric value) as 0% so the gate fails loudly instead of crashing.
    if "total" in doc:
        total = doc["total"]
        if not isinstance(total, dict):
            raise ValueError("invalid vitest total")
        lines = total.get("lines")
        if not isinstance(lines, dict) or "pct" not in lines:
            raise ValueError("missing vitest line percentage")
        return normalize_pct(lines["pct"])
    raise ValueError(f"unrecognized coverage report shape; keys={list(doc)}")


def main() -> None:
    if len(sys.argv) != 6:
        raise SystemExit(__doc__)
    component, label, threshold, in_path, out_path = sys.argv[1:6]
    with open(in_path, encoding="utf-8") as fh:
        doc = json.load(fh)
    summary = {
        "component": component,
        "label": label,
        "threshold": int(threshold),
        "head_pct": round(line_pct(doc), 2),
        "base_pct": None,
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, allow_nan=False)
    print(f"{label}: head lines {summary['head_pct']}% (threshold {threshold}%)")


if __name__ == "__main__":
    main()
